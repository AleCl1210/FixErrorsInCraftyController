"""Esecuzione delle riparazioni: quarantena, spostamenti, aggiornamenti, verifica e rollback."""
from __future__ import annotations

import logging
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .analyzer import Action, Analyzer, load_log_text
from .crafty import Server
from .store import Store

log = logging.getLogger("crafty_medic.repair")

SUCCESS_MARKERS = (
    "Done (",
    "Forge server running",
    "Ran in ",
    "Timings Reset",
    "Listening on",
)


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


@dataclass
class Job:
    id: str
    server_id: str
    server_name: str
    status: str = "running"        # running | done | error | rolledback
    lines: list[dict] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    ended: float | None = None
    result: dict = field(default_factory=dict)

    def emit(self, text: str, level: str = "info") -> None:
        line = {"ts": _ts(), "text": text, "level": level}
        self.lines.append(line)
        log.info("[%s] %s", self.server_name, text)

    def finish(self, status: str, **result) -> None:
        self.status = status
        self.ended = time.time()
        self.result = {**self.result, **result}

    @property
    def elapsed(self) -> float:
        return (self.ended or time.time()) - self.started

    def as_dict(self) -> dict:
        data = asdict(self)
        data["elapsed"] = round(self.elapsed, 1)
        return data


class RepairEngine:
    def __init__(self, cfg, client, store: Store, analyzer: Analyzer, updater=None):
        self.cfg = cfg
        self.client = client
        self.store = store
        self.analyzer = analyzer
        self.updater = updater
        self.jobs: dict[str, Job] = {}

    @staticmethod
    def _short(text: str, limit: int = 120) -> str:
        text = str(text or "").replace("\n", " ")
        return text if len(text) <= limit else text[:limit].rstrip() + "..."

    # ------------------------------------------------------------------ #
    def new_job(self, server: Server) -> Job:
        job = Job(id=uuid.uuid4().hex[:8], server_id=server.server_id, server_name=server.name)
        self.jobs[job.id] = job
        return job

    # ------------------------------------------------------------------ #
    def apply(self, server: Server, actions: list[Action], auto_start: bool = True,
              verify: bool = True, rollback: bool | None = None, job: Job | None = None) -> Job:
        rollback = self.cfg.auto_rollback if rollback is None else rollback
        job = job or self.new_job(server)
        job.emit(f"Avvio riparazione: {len(actions)} azion(e)i su {server.name}")

        ctx = self.analyzer.build_context(server, self.client)
        root = ctx.root
        if not root or not root.is_dir():
            job.emit(f"Cartella del server non raggiungibile: {root}", "error")
            job.finish("error", error="root non disponibile")
            return job

        manifests: list[str] = []
        applied: list[dict] = []
        was_running = self._is_running(server)

        # 1) ferma il server
        if was_running:
            job.emit("Server in esecuzione: lo fermo prima di toccare i file...")
            self._stop(server, job)

        # 2) backup
        if self.cfg.create_backup_before_fix:
            backup = self.store.local_backup(server.server_id, root)
            job.emit(f"Backup di sicurezza creato: {backup.name if backup else 'FALLITO'}" + ("" if backup else " (continuo comunque)"))
            if backup is None:
                job.emit("Attenzione: backup non riuscito, controlla i permessi sulla cartella dati.", "warn")

        # 3) azioni
        for action in actions:
            try:
                if action.kind == "move":
                    self._do_move(root, action, job, applied)
                elif action.kind == "quarantine":
                    manifest = self._do_quarantine(server, root, action, job)
                    if manifest:
                        manifests.append(manifest)
                        applied.append({"kind": "quarantine", "target": action.target, "manifest": manifest})
                elif action.kind == "delete":
                    self._do_delete(root, action, job, applied)
                elif action.kind == "update":
                    manifest = self._do_update(server, ctx, root, action, job)
                    if manifest:
                        manifests.append(manifest)
                        applied.append({"kind": "update", "target": action.target, "manifest": manifest})
            except Exception as exc:  # pragma: no cover
                job.emit(f"Errore su {action.target}: {exc}", "error")

        if not applied:
            job.emit("Nessuna modifica applicata.", "warn")
            if was_running and auto_start:
                self._start(server, job)
            job.finish("error", applied=[], error="nessuna azione applicata")
            return job

        self.store.audit(server.server_id, "apply", f"{len(applied)} azioni applicate", "warn")

        # 4) riavvio
        if auto_start:
            job.emit("Riavvio il server per verificare...")
            self._start(server, job)
        else:
            job.emit("Server lasciato spento su tua richiesta.", "warn")
            job.finish("done", applied=applied, started=False, manifests=manifests)
            return job

        # 5) verifica
        status = "done"
        detail = ""
        if verify:
            ok, detail = self._verify(server, ctx, job)
            if not ok:
                status = "error"
                if rollback and manifests:
                    job.emit("Il server non riparte: annullo le modifiche (rollback).", "warn")
                    restored = 0
                    for manifest_id in manifests:
                        count, errors = self.store.restore(manifest_id, server_root=str(root))
                        restored += count
                        for err in errors:
                            job.emit(f"Rollback: {err}", "error")
                    job.emit(f"Rollback completato: {restored} file ripristinati.")
                    self._start(server, job)
                    status = "rolledback"
        job.finish(status, applied=applied, manifests=manifests, detail=detail)
        return job

    # ------------------------------------------------------------------ #
    def _is_running(self, server: Server) -> bool:
        try:
            self.client.refresh_status(server)
        except Exception:
            pass
        return bool(server.running)

    def _stop(self, server: Server, job: Job, timeout: int = 45) -> None:
        try:
            self.client.action(server.server_id, "stop_server")
        except Exception as exc:
            job.emit(f"Impossibile fermare il server via API: {exc}", "warn")
            return
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(3)
            try:
                self.client.refresh_status(server)
            except Exception:
                return
            if not server.running:
                job.emit("Server fermato.")
                return
        job.emit("Il server non si e' fermato in tempo: continuo lo stesso.", "warn")

    def _start(self, server: Server, job: Job) -> None:
        try:
            self.client.action(server.server_id, "start_server")
            job.emit("Comando di avvio inviato a Crafty.")
        except Exception as exc:
            job.emit(f"Impossibile avviare il server via API: {exc}", "error")

    # ------------------------------------------------------------------ #
    @staticmethod
    def _do_move(root: Path, action: Action, job: Job, applied: list[dict]) -> None:
        source = root / action.target
        if not source.exists():
            job.emit(f"{action.target}: file non trovato", "warn")
            return
        destination_dir = root / (action.param or "plugins")
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / source.name
        if destination.exists():
            job.emit(f"{action.target}: esiste gia' {destination.relative_to(root)}", "warn")
            return
        shutil.move(str(source), str(destination))
        job.emit(f"Spostato {action.target} -> {destination.relative_to(root).as_posix()}")
        applied.append({"kind": "move", "target": action.target, "destination": destination.relative_to(root).as_posix()})

    def _do_quarantine(self, server: Server, root: Path, action: Action, job: Job) -> str | None:
        source = root / action.target
        if not source.exists():
            job.emit(f"{action.target}: file non trovato", "warn")
            return None
        manifest = self.store.quarantine(server.server_id, server.name, [(action.target, str(source))], reason=action.label)
        job.emit(f"In quarantena: {action.target}")
        return manifest.id

    @staticmethod
    def _do_delete(root: Path, action: Action, job: Job, applied: list[dict]) -> None:
        source = root / action.target
        if not source.exists():
            return
        source.unlink()
        job.emit(f"Eliminato: {action.target}", "warn")
        applied.append({"kind": "delete", "target": action.target})

    def _do_update(self, server: Server, ctx, root: Path, action: Action, job: Job) -> str | None:
        if not self.updater:
            job.emit("Updater disattivato: impossibile aggiornare.", "warn")
            return None
        source = root / action.target
        if not source.exists():
            job.emit(f"{action.target}: file non trovato", "warn")
            return None
        from .jarmeta import read_jar_meta
        meta = read_jar_meta(source, action.target)
        job.emit(f"Ricerca aggiornamento per {meta.label}...")
        try:
            result = self.updater.find_update(meta, ctx.loader, ctx.mc_version)
        except Exception as exc:
            job.emit(f"Ricerca fallita: {self._short(exc)}", "error")
            job.emit("Procedo con la quarantena, cosi' il server riparte.", "warn")
            return self._do_quarantine(server, root, action, job)
        if not result.ok:
            job.emit(f"Nessun aggiornamento automatico: {self._short(result.message)}", "warn")
            job.emit("Procedo con la quarantena, cosi' il server riparte.", "warn")
            return self._do_quarantine(server, root, action, job)

        tmp_dir = Path(self.cfg.data_dir) / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        filename = source.name
        tmp_file = tmp_dir / f"{uuid.uuid4().hex[:8]}-{filename}"
        try:
            self.updater.download_update(result, tmp_file)
        except Exception as exc:
            job.emit(f"Download fallito: {self._short(exc)}", "error")
            if tmp_file.exists():
                tmp_file.unlink()
            job.emit("Procedo con la quarantena, cosi' il server riparte.", "warn")
            return self._do_quarantine(server, root, action, job)

        # il vecchio jar va in quarantena, il nuovo prende il suo posto
        manifest = self.store.quarantine(server.server_id, server.name, [(action.target, str(source))], reason=f"aggiornamento: {result.project} {result.version_name}")
        shutil.move(str(tmp_file), str(source))
        job.emit(f"Aggiornato {filename} -> {result.project} {result.version_name}")
        return manifest.id

    # ------------------------------------------------------------------ #
    def _verify(self, server: Server, ctx, job: Job, wait: int | None = None) -> tuple[bool, str]:
        wait = wait if wait is not None else self.cfg.boot_wait_seconds
        deadline = time.time() + max(10, wait)
        job.emit(f"Verifico l'avvio (attendo fino a {max(10, wait)} secondi)...")
        last = ""
        while time.time() < deadline:
            time.sleep(8)
            log_text, _ = load_log_text(server, ctx.root, self.client, self.cfg.log_tail_bytes)
            if not log_text:
                continue
            tail = log_text[-8000:]
            if any(marker in tail for marker in SUCCESS_MARKERS):
                job.emit("Il server e' partito correttamente.", "ok")
                return True, "server avviato"
            fresh = self.analyzer.analyze(self.analyzer.build_context(server, self.client))
            criticals = [f for f in fresh.findings if f.severity == "critical" and f.category in ("mod", "plugin")]
            if criticals:
                last = "; ".join(f"{f.title} ({f.targets[0].name if f.targets else '?'})" for f in criticals[:3])
                job.emit(f"Ancora errori: {last}", "error")
                return False, last
        return False, last or "timeout di verifica"

    # ------------------------------------------------------------------ #
    #  operazioni semplici usate dall'interfaccia
    # ------------------------------------------------------------------ #
    def quarantine_file(self, server: Server, rel: str, reason: str = "quarantena manuale") -> str | None:
        ctx = self.analyzer.build_context(server, self.client)
        if not ctx.root:
            return None
        source = ctx.root / rel
        if not source.exists():
            return None
        manifest = self.store.quarantine(server.server_id, server.name, [(rel, str(source))], reason=reason)
        return manifest.id

    def restore_manifest(self, manifest_id: str, server_root: str | None = None) -> tuple[int, list[str]]:
        return self.store.restore(manifest_id, server_root=server_root)

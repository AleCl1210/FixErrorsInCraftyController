"""Interfaccia web di Crafty Medic (Flask, senza dipendenze frontend)."""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for

from .analyzer import Action, Analyzer
from .config import Config
from .crafty import CraftyClient, CraftyError, Server
from .demo import DemoClient
from .jarmeta import read_jar_meta
from .repair import RepairEngine
from .store import Store
from .updater import Updater

log = logging.getLogger("crafty_medic.web")

SEVERITY_LABEL = {
    "critical": "Bloccante",
    "high": "Importante",
    "medium": "Da controllare",
    "low": "Informazione",
    "info": "Informazione",
}

CATEGORY_LABEL = {
    "mod": "Mod",
    "plugin": "Plugin",
    "java": "Java",
    "config": "Configurazione",
    "world": "Mondo",
    "generic": "Generale",
}

REMEDY_LABEL = {
    "quarantine": "Quarantena",
    "update": "Aggiornamento",
    "move": "Spostamento",
    "config": "Configurazione",
    "manual": "Manuale",
}


def human_size(value: int) -> str:
    value = float(value or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


class Medic:
    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or Config.from_env()
        self.store = Store(self.cfg.data_dir)
        if self.cfg.demo:
            self.client = DemoClient(self.cfg.demo_dir)
        else:
            self.client = CraftyClient(
                self.cfg.crafty_url, self.cfg.crafty_token,
                self.cfg.crafty_verify_ssl, self.cfg.crafty_timeout,
            )
        self.analyzer = Analyzer(self.cfg)
        self.updater = Updater(
            self.cfg.modrinth_base, self.cfg.user_agent,
            min_score=self.cfg.update_min_score,
            mapping=self.store.get_mapping(),
        ) if self.cfg.updater_enabled else None
        self.engine = RepairEngine(self.cfg, self.client, self.store, self.analyzer, self.updater)
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, object]] = {}

    # ------------------------------------------------------------------ #
    def servers(self) -> tuple[list[Server], str | None]:
        """Lista dei server + eventuale errore."""
        try:
            servers = self.client.list_servers()
        except CraftyError as exc:
            return [], str(exc)
        except Exception as exc:  # pragma: no cover
            return [], f"Errore imprevisto: {exc}"
        for server in servers:
            try:
                self.client.refresh_status(server)
            except Exception:
                pass
        return servers, None

    def get_server(self, server_id: str) -> Server | None:
        servers, _ = self.servers()
        for item in servers:
            if item.server_id == server_id:
                return item
        return None

    def analyze(self, server: Server, force: bool = False):
        with self._lock:
            cached = self._cache.get(server.server_id)
            if cached and not force and time.time() - cached[0] < 60:
                return cached[1]
        analysis = self.analyzer.analyze(self.analyzer.build_context(server, self.client))
        with self._lock:
            self._cache[server.server_id] = (time.time(), analysis)
        return analysis

    def server_root(self, server: Server) -> Path | None:
        from .analyzer import resolve_server_root
        return resolve_server_root(server, self.cfg.servers_dir, self.cfg.path_map)


def create_app(cfg: Config | None = None) -> Flask:
    medic = Medic(cfg)
    cfg = medic.cfg
    app = Flask(__name__, template_folder="templates", static_folder=None)
    app.secret_key = cfg.secret_key

    @app.template_filter("sizeformat")
    def _size(value):
        return human_size(value)

    @app.context_processor
    def _globals():
        online = False
        error = None
        try:
            online = medic.client.check()
        except Exception as exc:
            error = str(exc)
        try:
            quarantine_count = len([m for m in medic.store.manifests() if not m.restored])
        except Exception:  # pragma: no cover
            quarantine_count = 0
        return {
            "cfg": cfg,
            "demo": cfg.demo,
            "crafty_online": online,
            "crafty_error": error,
            "quarantine_count": quarantine_count,
            "sev_label": SEVERITY_LABEL,
            "cat_label": CATEGORY_LABEL,
            "remedy_label": REMEDY_LABEL,
        }

    # ------------------------------------------------------------------ #
    @app.get("/")
    def index():
        servers, error = medic.servers()
        cards = []
        for server in servers:
            summary = {"problems": 0, "critical": 0}
            try:
                analysis = medic.analyze(server)
                summary["problems"] = len(analysis.findings)
                summary["critical"] = len(analysis.critical)
                summary["fixable"] = len(analysis.fixable)
            except Exception as exc:  # pragma: no cover
                log.warning("analisi rapida fallita per %s: %s", server.name, exc)
                summary["error"] = str(exc)
            cards.append((server, summary))
        quarantine = medic.store.manifests()
        return render_template("index.html", cards=cards, error=error,
                               quarantine_count=len([m for m in quarantine if not m.restored]))

    # ------------------------------------------------------------------ #
    @app.get("/server/<server_id>")
    def server_page(server_id: str):
        server = medic.get_server(server_id)
        if not server:
            return render_template("error.html", message=f"Server {server_id} non trovato"), 404
        force = request.args.get("refresh") == "1"
        analysis = medic.analyze(server, force=force)
        job_id = request.args.get("job")
        job = medic.engine.jobs.get(job_id) if job_id else None
        root = medic.server_root(server)
        return render_template(
            "server.html",
            server=server,
            analysis=analysis,
            job=job,
            root=str(root) if root else "",
            manifests=[m for m in medic.store.manifests(server_id)],
            audit=medic.store.read_audit(server_id, limit=15),
        )

    # ------------------------------------------------------------------ #
    @app.post("/server/<server_id>/apply")
    def apply(server_id: str):
        server = medic.get_server(server_id)
        if not server:
            return redirect(url_for("index"))
        selected = request.form.getlist("action")
        actions: list[Action] = []
        for raw in selected:
            parts = raw.split("|", 2)
            if len(parts) < 2:
                continue
            kind, target = parts[0], parts[1]
            param = parts[2] if len(parts) > 2 else ""
            if kind == "update":
                slug = request.form.get(f"slug|{target}", "").strip()
                if slug and medic.updater:
                    key = (Path(target).name).lower()
                    medic.store.set_mapping(key, slug)
                    medic.updater.mapping = medic.store.get_mapping()
            actions.append(Action(id=uuid.uuid4().hex[:8], kind=kind, target=target, param=param))
        if not actions:
            return redirect(url_for("server_page", server_id=server_id))

        auto_start = request.form.get("auto_start", "1") == "1"
        rollback = request.form.get("rollback", "1") == "1"
        job = medic.engine.new_job(server)
        job.emit(f"{len(actions)} azioni selezionate")

        def run():
            try:
                medic.engine.apply(server, actions, auto_start=auto_start,
                                   verify=auto_start, rollback=rollback, job=job)
            except Exception as exc:  # pragma: no cover
                job.emit(f"Errore imprevisto: {exc}", "error")
                job.finish("error", error=str(exc))
            finally:
                medic.analyze(server, force=True)

        threading.Thread(target=run, daemon=True).start()
        return redirect(url_for("server_page", server_id=server_id, job=job.id))

    # ------------------------------------------------------------------ #
    @app.post("/server/<server_id>/auto")
    def auto_fix(server_id: str):
        """Riparazione automatica: aggiorna se puo', altrimenti toglie il file."""
        server = medic.get_server(server_id)
        if not server:
            return redirect(url_for("index"))
        analysis = medic.analyze(server, force=True)

        actions: list[Action] = []
        seen: set[str] = set()
        for finding in analysis.sorted_findings():
            if finding.severity not in ("critical", "high"):
                continue
            for ref in finding.targets:
                if ref.rel in seen:
                    continue
                seen.add(ref.rel)
                if finding.remedy == "update" and medic.updater:
                    actions.append(Action(
                        id=uuid.uuid4().hex[:8], kind="update", target=ref.rel,
                        label=f"Aggiorna {ref.label} (se non si puo': quarantena)",
                        confidence=finding.confidence))
                elif finding.remedy == "move":
                    param = finding.actions[0].param if finding.actions else ""
                    if param:
                        actions.append(Action(
                            id=uuid.uuid4().hex[:8], kind="move", target=ref.rel,
                            param=param, label=f"Sposta {ref.name} in {param}/",
                            confidence=finding.confidence))
                elif finding.remedy == "quarantine":
                    actions.append(Action(
                        id=uuid.uuid4().hex[:8], kind="quarantine", target=ref.rel,
                        label=f"Metti in quarantena {ref.name}",
                        confidence=finding.confidence))

        if not actions:
            return redirect(url_for("server_page", server_id=server_id, refresh="1"))

        rollback = request.form.get("rollback", "1") == "1"
        job = medic.engine.new_job(server)
        job.emit(f"Riparazione automatica: {len(actions)} file da sistemare")

        def run():
            try:
                medic.engine.apply(server, actions, auto_start=True, verify=True,
                                   rollback=rollback, job=job)
            except Exception as exc:  # pragma: no cover
                job.emit(f"Errore imprevisto: {exc}", "error")
                job.finish("error", error=str(exc))
            finally:
                medic.analyze(server, force=True)

        threading.Thread(target=run, daemon=True).start()
        return redirect(url_for("server_page", server_id=server_id, job=job.id))

    # ------------------------------------------------------------------ #
    @app.get("/api/job/<job_id>")
    def job_status(job_id: str):
        job = medic.engine.jobs.get(job_id)
        if not job:
            return jsonify({"status": "not_found"}), 404
        return jsonify(job.as_dict())

    # ------------------------------------------------------------------ #
    @app.post("/server/<server_id>/quarantine")
    def quarantine_one(server_id: str):
        server = medic.get_server(server_id)
        rel = request.form.get("rel", "")
        if server and rel:
            medic.engine.quarantine_file(server, rel, reason="quarantena manuale dall'interfaccia")
            medic.analyze(server, force=True)
        return redirect(url_for("server_page", server_id=server_id, refresh="1"))

    # ------------------------------------------------------------------ #
    @app.get("/quarantine")
    def quarantine_page():
        manifests = medic.store.manifests()
        return render_template("quarantine.html", manifests=manifests)

    @app.post("/quarantine/<manifest_id>/restore")
    def restore_manifest(manifest_id: str):
        manifest = medic.store.get_manifest(manifest_id)
        if manifest:
            server = medic.get_server(manifest.server_id)
            root = medic.server_root(server) if server else None
            count, errors = medic.store.restore(manifest_id, server_root=str(root) if root else None)
            if errors:
                log.warning("ripristino con errori: %s", errors)
            if server:
                medic.analyze(server, force=True)
        return redirect(url_for("quarantine_page"))

    @app.post("/quarantine/<manifest_id>/delete")
    def delete_manifest(manifest_id: str):
        medic.store.delete_manifest(manifest_id)
        return redirect(url_for("quarantine_page"))

    # ------------------------------------------------------------------ #
    @app.post("/server/<server_id>/control")
    def control(server_id: str):
        server = medic.get_server(server_id)
        action = request.form.get("action", "")
        if server and action:
            try:
                medic.client.action(server_id, action)
                medic.store.audit(server_id, action, "comando inviato dall'interfaccia")
            except Exception as exc:
                medic.store.audit(server_id, action, f"errore: {exc}", "error")
            time.sleep(1)
        return redirect(url_for("server_page", server_id=server_id, refresh="1"))

    # ------------------------------------------------------------------ #
    @app.get("/api/update-preview")
    def update_preview():
        server_id = request.args.get("server", "")
        rel = request.args.get("file", "")
        server = medic.get_server(server_id)
        if not server or not rel:
            return jsonify({"ok": False, "message": "parametri mancanti"})
        root = medic.server_root(server)
        if not root:
            return jsonify({"ok": False, "message": "cartella del server non raggiungibile"})
        meta = read_jar_meta(root / rel, rel)
        analysis = medic.analyze(server)
        try:
            result = medic.updater.find_update(meta, analysis.loader, analysis.mc_version)
        except Exception as exc:
            return jsonify({"ok": False, "message": str(exc)})
        return jsonify({
            "ok": result.ok,
            "message": result.message,
            "project": result.project,
            "version": result.version_name,
            "url": result.url,
            "candidates": [
                {"slug": c.slug, "title": c.title, "type": c.project_type, "score": round(c.score, 1)}
                for c in (result.candidates or [])
            ],
        })

    # ------------------------------------------------------------------ #
    @app.get("/settings")
    def settings():
        return render_template("settings.html", mapping=medic.store.get_mapping(),
                               audit=medic.store.read_audit(limit=40))

    @app.post("/settings/mapping")
    def save_mapping():
        key = request.form.get("key", "").strip()
        slug = request.form.get("slug", "").strip()
        if key:
            if slug:
                medic.store.set_mapping(key, slug)
            else:
                data = medic.store.get_mapping()
                data.pop(key.lower(), None)
                medic.store.mapping_file.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        if medic.updater:
            medic.updater.mapping = medic.store.get_mapping()
        return redirect(url_for("settings"))

    # ------------------------------------------------------------------ #
    @app.get("/health")
    def health():
        return jsonify({
            "status": "ok",
            "demo": cfg.demo,
            "crafty": medic.client.check() if hasattr(medic.client, "check") else False,
        })

    return app


def main() -> None:  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(description="Crafty Medic")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.from_env()
    app = create_app(cfg)
    app.run(host=args.host or cfg.host, port=args.port or cfg.port, debug=args.debug, threaded=True)


if __name__ == "__main__":  # pragma: no cover
    main()

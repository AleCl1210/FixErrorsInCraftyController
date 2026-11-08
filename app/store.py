"""Quarantena, backup locali e log di audit."""
from __future__ import annotations

import json
import logging
import shutil
import tarfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger("crafty_medic.store")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class QuarantineEntry:
    rel: str            # path originale relativo alla root del server
    stored: str         # path dentro la cartella di quarantena
    size: int = 0
    sha1: str = ""


@dataclass
class Manifest:
    id: str
    server_id: str
    server_name: str
    created: str
    reason: str
    files: list[QuarantineEntry] = field(default_factory=list)
    restored: bool = False

    @property
    def size(self) -> int:
        return sum(f.size for f in self.files)


class Store:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.quarantine_dir = self.data_dir / "quarantine"
        self.backups_dir = self.data_dir / "backups"
        self.audit_file = self.data_dir / "audit.jsonl"
        self.mapping_file = self.data_dir / "modrinth_map.json"
        for folder in (self.data_dir, self.quarantine_dir, self.backups_dir):
            folder.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    #  audit
    # ------------------------------------------------------------------ #
    def audit(self, server_id: str, action: str, detail: str = "", level: str = "info") -> None:
        record = {
            "ts": _now(),
            "server_id": server_id,
            "action": action,
            "detail": detail,
            "level": level,
        }
        try:
            with open(self.audit_file, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:  # pragma: no cover
            log.warning("audit non scritto: %s", exc)

    def read_audit(self, server_id: str | None = None, limit: int = 100) -> list[dict]:
        if not self.audit_file.is_file():
            return []
        try:
            lines = self.audit_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        records = []
        for line in lines[-2000:]:
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
        if server_id:
            records = [r for r in records if r.get("server_id") == server_id]
        records.reverse()
        return records[:limit]

    # ------------------------------------------------------------------ #
    #  quarantena
    # ------------------------------------------------------------------ #
    def quarantine(self, server_id: str, server_name: str,
                   files: list[tuple[str, str]], reason: str = "") -> Manifest:
        """Sposta i file nella quarantena. `files` = [(rel, abs_path)]."""
        manifest_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        target_dir = self.quarantine_dir / server_id / manifest_id
        target_dir.mkdir(parents=True, exist_ok=True)

        manifest = Manifest(
            id=manifest_id,
            server_id=server_id,
            server_name=server_name,
            created=_now(),
            reason=reason,
        )
        for rel, abs_path in files:
            source = Path(abs_path)
            if not source.exists():
                log.warning("file non trovato, salto: %s", abs_path)
                continue
            destination = target_dir / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(source), str(destination))
            except OSError as exc:
                log.error("spostamento fallito %s: %s", source, exc)
                continue
            manifest.files.append(QuarantineEntry(rel=rel, stored=str(destination.relative_to(target_dir)), size=source.stat().st_size if source.exists() else 0))

        (target_dir / "manifest.json").write_text(
            json.dumps(asdict(manifest), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.audit(server_id, "quarantine", f"{len(manifest.files)} file in quarantena ({manifest_id}): {reason}", "warn")
        return manifest

    def manifests(self, server_id: str | None = None) -> list[Manifest]:
        base = self.quarantine_dir if server_id is None else self.quarantine_dir / server_id
        if not base.is_dir():
            return []
        out: list[Manifest] = []
        for server_folder in ([base] if server_id else sorted(p for p in base.iterdir() if p.is_dir())):
            for folder in sorted(server_folder.iterdir()):
                manifest_file = folder / "manifest.json"
                if not manifest_file.is_file():
                    continue
                try:
                    data = json.loads(manifest_file.read_text(encoding="utf-8"))
                except ValueError:
                    continue
                manifest = Manifest(
                    id=data.get("id", folder.name),
                    server_id=data.get("server_id", server_folder.name),
                    server_name=data.get("server_name", ""),
                    created=data.get("created", ""),
                    reason=data.get("reason", ""),
                    restored=bool(data.get("restored", False)),
                    files=[QuarantineEntry(**entry) for entry in data.get("files", [])],
                )
                manifest.files = [f for f in manifest.files if f.rel]
                out.append(manifest)
        out.sort(key=lambda m: m.created, reverse=True)
        return out

    def get_manifest(self, manifest_id: str) -> Manifest | None:
        for manifest in self.manifests():
            if manifest.id == manifest_id:
                return manifest
        return None

    def _manifest_dir(self, manifest: Manifest) -> Path:
        return self.quarantine_dir / manifest.server_id / manifest.id

    def restore(self, manifest_id: str, servers_dir_fn=None, server_root: str | None = None) -> tuple[int, list[str]]:
        """Riporta i file in quarantena nella loro posizione originale."""
        manifest = self.get_manifest(manifest_id)
        if not manifest:
            return 0, ["manifest non trovato"]
        root = Path(server_root) if server_root else None
        if root is None and servers_dir_fn:
            root = Path(servers_dir_fn(manifest.server_id))
        if root is None or not root.is_dir():
            return 0, [f"cartella del server non disponibile: {root}"]

        source_dir = self._manifest_dir(manifest)
        restored, errors = 0, []
        for entry in manifest.files:
            source = source_dir / entry.stored
            if not source.exists():
                errors.append(f"{entry.rel}: file di quarantena mancante")
                continue
            destination = root / entry.rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(source), str(destination))
                restored += 1
            except OSError as exc:
                errors.append(f"{entry.rel}: {exc}")
        self._write_manifest(manifest, restored=restored > 0)
        self.audit(manifest.server_id, "restore", f"ripristinati {restored} file da {manifest_id}", "info")
        return restored, errors

    def delete_manifest(self, manifest_id: str) -> bool:
        manifest = self.get_manifest(manifest_id)
        if not manifest:
            return False
        shutil.rmtree(self._manifest_dir(manifest), ignore_errors=True)
        self.audit(manifest.server_id, "delete", f"quarantena {manifest_id} eliminata definitivamente", "warn")
        return True

    def _write_manifest(self, manifest: Manifest, restored: bool = False) -> None:
        if restored:
            manifest.restored = True
        path = self._manifest_dir(manifest) / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(manifest), ensure_ascii=False, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------ #
    #  backup locale (leggero: solo i file toccati + configurazioni)
    # ------------------------------------------------------------------ #
    def local_backup(self, server_id: str, root: Path, extra_paths: list[Path] | None = None) -> Path | None:
        """Crea un tar.gz con mods/, plugins/, config/ e i file .properties/.yml/.txt/.json nella root."""
        folder = self.backups_dir / server_id
        folder.mkdir(parents=True, exist_ok=True)
        name = datetime.now().strftime("%Y%m%d-%H%M%S") + ".tar.gz"
        destination = folder / name
        try:
            with tarfile.open(destination, "w:gz") as tar:
                for subfolder in ("mods", "plugins", "config"):
                    path = root / subfolder
                    if path.is_dir():
                        tar.add(path, arcname=subfolder)
                for path in sorted(root.glob("*")):
                    if path.is_file() and path.suffix.lower() in {".properties", ".yml", ".yaml", ".txt", ".json", ".toml"}:
                        tar.add(path, arcname=path.name)
                for path in extra_paths or []:
                    if path.exists():
                        tar.add(path, arcname="extra/" + path.name)
        except OSError as exc:
            log.error("backup locale fallito: %s", exc)
            return None
        self.audit(server_id, "backup", f"backup locale {destination.name}", "info")
        self._prune_backups(folder)
        return destination

    @staticmethod
    def _prune_backups(folder: Path, keep: int = 10) -> None:
        files = sorted(folder.glob("*.tar.gz"))
        for old in files[:-keep]:
            try:
                old.unlink()
            except OSError:
                pass

    def backups(self, server_id: str) -> list[Path]:
        folder = self.backups_dir / server_id
        if not folder.is_dir():
            return []
        return sorted(folder.glob("*.tar.gz"), reverse=True)

    # ------------------------------------------------------------------ #
    #  mappatura manuale mod -> progetto Modrinth
    # ------------------------------------------------------------------ #
    def get_mapping(self) -> dict:
        if not self.mapping_file.is_file():
            return {}
        try:
            return json.loads(self.mapping_file.read_text(encoding="utf-8"))
        except ValueError:
            return {}

    def set_mapping(self, key: str, project: str) -> None:
        data = self.get_mapping()
        data[key.strip().lower()] = project.strip()
        self.mapping_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

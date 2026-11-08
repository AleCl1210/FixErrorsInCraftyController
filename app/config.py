"""Configurazione di Crafty Medic.

Tutte le opzioni arrivano da variabili d'ambiente (o da un file .env
nella directory dati / nella root del progetto).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "si"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def load_dotenv(path: str | Path | None = None) -> None:
    """Carica un file .env minimale senza dipendenze esterne."""
    candidates = []
    if path:
        candidates.append(Path(path))
    else:
        candidates.append(ROOT / ".env")
        candidates.append(Path(os.environ.get("CRAFTY_MEDIC_DATA_DIR", "/data")) / ".env")
    for candidate in candidates:
        try:
            if not candidate.is_file():
                continue
            for raw_line in candidate.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)
        except OSError:
            continue


def _parse_path_map(raw: str) -> list[tuple[str, str]]:
    """"/crafty/servers=/mnt/crafty/servers,/crafty=/mnt/crafty" -> lista di coppie."""
    pairs: list[tuple[str, str]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        src, dst = chunk.split("=", 1)
        src, dst = src.strip().rstrip("/"), dst.strip().rstrip("/")
        if src:
            pairs.append((src, dst))
    # i prefissi piu' lunghi devono essere valutati per primi
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


@dataclass
class Config:
    # --- Crafty Controller ---
    crafty_url: str = ""
    crafty_token: str = ""
    crafty_verify_ssl: bool = False
    crafty_timeout: int = 20

    # --- filesystem ---
    servers_dir: str = ""
    path_map: list[tuple[str, str]] = field(default_factory=list)

    # --- dati interni (quarantena, audit, cache) ---
    data_dir: str = str(ROOT / "data")

    # --- comportamento ---
    demo: bool = False
    demo_dir: str = str(ROOT / "demo")
    create_backup_before_fix: bool = True
    auto_rollback: bool = True
    boot_wait_seconds: int = 60
    log_tail_bytes: int = 4_000_000
    quarantine_keep: int = 20
    user_agent: str = "CraftyMedic/1.0 (self-hosted Minecraft repair tool)"

    # --- update online ---
    updater_enabled: bool = True
    modrinth_base: str = "https://api.modrinth.com/v2"
    curseforge_api_key: str = ""
    update_min_score: int = 55

    # --- web ---
    host: str = "0.0.0.0"
    port: int = 8765
    secret_key: str = "crafty-medic-change-me"

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        return cls(
            crafty_url=os.environ.get("CRAFTY_URL", "").rstrip("/"),
            crafty_token=os.environ.get("CRAFTY_TOKEN", "").strip(),
            crafty_verify_ssl=_env_bool("CRAFTY_VERIFY_SSL", False),
            crafty_timeout=_env_int("CRAFTY_TIMEOUT", 20),
            servers_dir=os.environ.get("CRAFTY_SERVERS_DIR", "").rstrip("/"),
            path_map=_parse_path_map(os.environ.get("CRAFTY_MEDIC_PATH_MAP", "")),
            data_dir=os.environ.get("CRAFTY_MEDIC_DATA_DIR", str(ROOT / "data")),
            demo=_env_bool("CRAFTY_MEDIC_DEMO", False),
            demo_dir=os.environ.get("CRAFTY_MEDIC_DEMO_DIR", str(ROOT / "demo")),
            create_backup_before_fix=_env_bool("CRAFTY_MEDIC_BACKUP", True),
            auto_rollback=_env_bool("CRAFTY_MEDIC_AUTO_ROLLBACK", True),
            boot_wait_seconds=_env_int("CRAFTY_MEDIC_BOOT_WAIT", 60),
            log_tail_bytes=_env_int("CRAFTY_MEDIC_LOG_TAIL", 4_000_000),
            quarantine_keep=_env_int("CRAFTY_MEDIC_QUARANTINE_KEEP", 20),
            updater_enabled=_env_bool("CRAFTY_MEDIC_UPDATER", True),
            curseforge_api_key=os.environ.get("CURSEFORGE_API_KEY", "").strip(),
            host=os.environ.get("CRAFTY_MEDIC_HOST", "0.0.0.0"),
            port=_env_int("CRAFTY_MEDIC_PORT", 8765),
            secret_key=os.environ.get("CRAFTY_MEDIC_SECRET", "crafty-medic-change-me"),
        )

    # ------------------------------------------------------------------ #
    def map_path(self, path: str) -> str:
        """Traduce un path visto da Crafty (dentro il container) in path locale."""
        if not path:
            return path
        for src, dst in self.path_map:
            if path == src or path.startswith(src + "/"):
                return dst + path[len(src):]
        return path

    @property
    def quarantine_dir(self) -> Path:
        return Path(self.data_dir) / "quarantine"

    @property
    def backups_dir(self) -> Path:
        return Path(self.data_dir) / "backups"

    @property
    def audit_file(self) -> Path:
        return Path(self.data_dir) / "audit.jsonl"

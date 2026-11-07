"""Provider dimostrativo: simula le API di Crafty leggendo i file in demo/.

Serve a provare Crafty Medic senza un'istanza Crafty vera
(modalita' CRAFTY_MEDIC_DEMO=1).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .crafty import Server

log = logging.getLogger("crafty_medic.demo")

DEMO_SERVERS = [
    {
        "server_id": "demo-fabric",
        "name": "Moddato Fabric 1.20.1",
        "type": "minecraft-java",
        "version": "1.20.1",
        "path": "demo-fabric",
        "status": "stopped",
    },
    {
        "server_id": "demo-paper",
        "name": "Paper Plugin 1.20.6",
        "type": "minecraft-java",
        "version": "1.20.6",
        "path": "demo-paper",
        "status": "stopped",
    },
    {
        "server_id": "demo-forge",
        "name": "Forge 1.20.1 Survival",
        "type": "minecraft-java",
        "version": "1.20.1",
        "path": "demo-forge",
        "status": "stopped",
    },
]


class DemoClient:
    """Interfaccia compatibile con CraftyClient, ma legge da file locali."""

    def __init__(self, demo_dir: str | Path):
        self.demo_dir = Path(demo_dir)
        self.running: dict[str, bool] = {}
        self.configured = True
        self.base_url = "demo"
        self.actions: list[str] = []

    # ------------------------------------------------------------------ #
    def _server_dir(self, server_id: str) -> Path:
        return self.demo_dir / server_id

    def _servers(self) -> list[dict]:
        config = self.demo_dir / "servers.json"
        if config.is_file():
            try:
                data = json.loads(config.read_text(encoding="utf-8"))
                if isinstance(data, list) and data:
                    return data
            except ValueError:
                pass
        return DEMO_SERVERS

    def check(self) -> bool:
        return True

    def list_servers(self) -> list[Server]:
        servers = []
        for item in self._servers():
            server = Server(
                server_id=str(item.get("server_id") or item.get("id")),
                name=str(item.get("name", "")),
                path=str(self._server_dir(item.get("server_id", ""))),
                server_type=str(item.get("type", "minecraft-java")),
                mc_version=str(item.get("version", "")),
                status=str(item.get("status", "stopped")),
                raw=dict(item),
            )
            server.running = self.running.get(server.server_id, False)
            servers.append(server)
        return servers

    def get_server(self, server_id: str) -> Server | None:
        for server in self.list_servers():
            if server.server_id == server_id:
                return server
        return None

    def refresh_status(self, server: Server) -> Server:
        server.running = self.running.get(server.server_id, False)
        server.status = "running" if server.running else "stopped"
        return server

    def all_status(self) -> dict[str, str]:
        return {s.server_id: ("running" if self.running.get(s.server_id) else "stopped") for s in self.list_servers()}

    def get_log(self, server_id: str, file: bool = True) -> str:
        path = self._server_dir(server_id) / "logs" / "latest.log"
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    # ------------------------------------------------------------------ #
    def _bad_files(self, server_id: str) -> list[str]:
        path = self._server_dir(server_id) / "bad_files.txt"
        if not path.is_file():
            return []
        return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _simulate_start(self, server_id: str) -> None:
        """Riscrive latest.log: crash se resta un file 'cattivo', altrimenti avvio ok."""
        base = self._server_dir(server_id)
        logs = base / "logs"
        boot = (logs / "boot.log").read_text(encoding="utf-8") if (logs / "boot.log").is_file() else ""
        remaining = [rel for rel in self._bad_files(server_id) if (base / rel).exists()]
        block_name = "crash.log" if remaining else "clean.log"
        block = (logs / block_name).read_text(encoding="utf-8") if (logs / block_name).is_file() else ""
        (logs / "latest.log").write_text(boot + block, encoding="utf-8")

    def action(self, server_id: str, action: str) -> dict[str, Any]:
        self.actions.append(f"{server_id}:{action}")
        log.info("[demo] azione %s su %s", action, server_id)
        if action == "start_server":
            self.running[server_id] = True
            self._simulate_start(server_id)
        elif action in ("stop_server", "kill_server"):
            self.running[server_id] = False
        elif action == "restart_server":
            self.running[server_id] = True
            self._simulate_start(server_id)
        return {"status": "ok", "action": action}

    def send_command(self, server_id: str, command: str) -> dict[str, Any]:
        self.actions.append(f"{server_id}:cmd:{command}")
        return {"status": "ok"}

    def backups(self, server_id: str) -> list[dict]:
        return []

    def crafty_stats(self) -> dict:
        return {"cpu_usage": 12, "mem_percent": 41}

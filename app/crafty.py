"""Client minimale per le API v2 di Crafty Controller.

Endpoint usati (Crafty Controller 4.x):
    GET  /api/v2/crafty/check
    GET  /api/v2/crafty/stats
    GET  /api/v2/servers
    GET  /api/v2/servers/status
    GET  /api/v2/servers/<id>
    GET  /api/v2/servers/<id>/stats
    GET  /api/v2/servers/<id>/logs?file=true
    POST /api/v2/servers/<id>/action/<action>
    POST /api/v2/servers/<id>/stdin
    GET  /api/v2/servers/<id>/backups
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger("crafty_medic.crafty")


class CraftyError(RuntimeError):
    """Errore di comunicazione con Crafty."""


@dataclass
class Server:
    server_id: str
    name: str = ""
    path: str = ""
    log_path: str = ""
    backup_path: str = ""
    server_type: str = ""
    mc_version: str = ""
    status: str = "unknown"
    running: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_online(self) -> bool:
        return bool(self.running) or self.status in {"running", "online", "started"}


_STATUS_KEYS = ("running", "online", "started", "is_running", "server_running")


def _first_str(data: dict, *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


class CraftyClient:
    def __init__(self, base_url: str, token: str, verify_ssl: bool = False, timeout: int = 20):
        self.base_url = base_url.rstrip("/") if base_url else ""
        self.token = token
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {self.token}"})

    # ------------------------------------------------------------------ #
    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    def _request(self, method: str, path: str, **kwargs) -> Any:
        if not self.base_url:
            raise CraftyError("CRAFTY_URL non configurato")
        url = f"{self.base_url}{path}"
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("verify", self.verify_ssl)
        try:
            response = self._session.request(method, url, **kwargs)
        except requests.RequestException as exc:  # pragma: no cover - rete
            raise CraftyError(f"Impossibile contattare Crafty: {exc}") from exc

        if response.status_code == 401:
            raise CraftyError("Token API Crafty non valido (401)")
        if response.status_code == 403:
            raise CraftyError("Permessi insufficienti sul token API Crafty (403)")
        if response.status_code >= 400:
            detail = ""
            try:
                payload = response.json()
                detail = payload.get("info", payload.get("error", ""))
            except ValueError:
                detail = response.text[:200]
            raise CraftyError(f"Crafty ha risposto {response.status_code}: {detail or response.reason}")

        if not response.content:
            return {}
        content_type = response.headers.get("Content-Type", "")
        if "json" in content_type:
            try:
                return response.json()
            except ValueError:
                return response.text
        # Crafty a volte risponde text/plain pur essendo JSON
        text = response.text
        try:
            return json.loads(text)
        except ValueError:
            return text

    @staticmethod
    def _unwrap(payload: Any) -> Any:
        if isinstance(payload, dict) and "data" in payload and set(payload) <= {"data", "status", "error"}:
            return payload["data"]
        return payload

    # ------------------------------------------------------------------ #
    def check(self) -> bool:
        """True se Crafty risponde."""
        try:
            self._request("GET", "/api/v2/crafty/check")
            return True
        except CraftyError as exc:
            log.warning("Crafty non raggiungibile: %s", exc)
            return False

    def list_servers(self) -> list[Server]:
        payload = self._unwrap(self._request("GET", "/api/v2/servers"))
        if isinstance(payload, dict):
            payload = payload.get("servers", [])
        servers: list[Server] = []
        for item in payload or []:
            if not isinstance(item, dict):
                continue
            servers.append(self._server_from_dict(item))
        return servers

    def _server_from_dict(self, item: dict) -> Server:
        server_id = str(item.get("server_id") or item.get("id") or item.get("uuid") or "")
        server = Server(
            server_id=server_id,
            name=str(item.get("server_name") or item.get("name") or server_id),
            path=str(item.get("path") or ""),
            log_path=str(item.get("log_path") or ""),
            backup_path=str(item.get("backup_path") or ""),
            server_type=str(item.get("type") or item.get("server_type") or ""),
            mc_version=str(item.get("version") or item.get("server_version") or ""),
            status=str(item.get("status") or "unknown"),
            raw=item,
        )
        for key in _STATUS_KEYS:
            if key in item:
                value = item[key]
                server.running = bool(value) if isinstance(value, bool) else str(value).lower() in {
                    "true", "running", "online", "started", "1",
                }
                break
        return server

    def get_server(self, server_id: str) -> Server | None:
        try:
            payload = self._unwrap(self._request("GET", f"/api/v2/servers/{server_id}"))
        except CraftyError:
            return None
        if isinstance(payload, dict):
            return self._server_from_dict(payload)
        return None

    def refresh_status(self, server: Server) -> Server:
        """Aggiorna running/status/versione usando /stats (e /servers/status)."""
        try:
            payload = self._unwrap(self._request("GET", f"/api/v2/servers/{server.server_id}/stats"))
        except CraftyError as exc:
            log.debug("stats non disponibili per %s: %s", server.server_id, exc)
            return server
        if isinstance(payload, dict):
            data = payload.get("data", payload) if isinstance(payload.get("data"), dict) else payload
            for key in _STATUS_KEYS:
                if key in data:
                    value = data[key]
                    server.running = bool(value) if isinstance(value, bool) else str(value).lower() in {
                        "true", "running", "online", "started", "1",
                    }
                    server.status = "running" if server.running else "stopped"
                    break
            server.mc_version = server.mc_version or _first_str(data, "version", "server_version", "mc_version")
        return server

    def all_status(self) -> dict[str, str]:
        try:
            payload = self._unwrap(self._request("GET", "/api/v2/servers/status"))
        except CraftyError:
            return {}
        out: dict[str, str] = {}
        if isinstance(payload, dict):
            for key, value in payload.items():
                if isinstance(value, dict):
                    out[str(key)] = str(value.get("status", ""))
                else:
                    out[str(key)] = str(value)
        return out

    def get_log(self, server_id: str, file: bool = True) -> str:
        path = f"/api/v2/servers/{server_id}/logs"
        if file:
            path += "?file=true"
        payload = self._unwrap(self._request("GET", path))
        if isinstance(payload, str):
            return payload
        if isinstance(payload, dict):
            for key in ("data", "content", "log", "logs"):
                if isinstance(payload.get(key), str):
                    return payload[key]
            return json.dumps(payload, ensure_ascii=False)
        return ""

    def action(self, server_id: str, action: str) -> Any:
        """action: start_server | stop_server | restart_server | kill_server | backup_server"""
        return self._request("POST", f"/api/v2/servers/{server_id}/action/{action}")

    def send_command(self, server_id: str, command: str) -> Any:
        return self._request("POST", f"/api/v2/servers/{server_id}/stdin", json={"command": command})

    def backups(self, server_id: str) -> list[dict]:
        payload = self._unwrap(self._request("GET", f"/api/v2/servers/{server_id}/backups"))
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

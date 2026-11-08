"""Test dell'interfaccia web, compresa la modalita' senza token API."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402
from app.web import create_app  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def env(tmp_path):
    servers = tmp_path / "servers"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "make_demo.py"),
                    "--out", str(servers), "--force"], check=True)
    (servers / "servers.json").unlink(missing_ok=True)
    cfg = Config()
    cfg.demo = False
    cfg.crafty_url = ""            # nessun token: modalita' "solo file"
    cfg.crafty_token = ""
    cfg.servers_dir = str(servers)
    cfg.data_dir = str(tmp_path / "data")
    cfg.create_backup_before_fix = False
    return cfg, servers


def test_index_lists_servers_without_token(env):
    cfg, servers = env
    client = create_app(cfg).test_client()
    response = client.get("/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "demo-paper" in body
    assert "problemi bloccanti" in body
    assert "solo file (senza token API)" in body


def test_server_page_shows_repair_options_without_token(env):
    cfg, servers = env
    client = create_app(cfg).test_client()
    response = client.get("/server/demo-paper")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Sistema tutto da solo" in body
    assert "Metti in quarantena ShopGUI.jar" in body
    assert "Sposta sodium-fabric-1.20.1-0.5.3.jar in mods/" in body
    # senza token i comandi Crafty sono sostituiti dalla spiegazione
    assert "CRAFTY_TOKEN" in body


def test_auto_fix_without_token_quarantines_and_asks_manual_restart(env):
    cfg, servers = env
    app = create_app(cfg)
    client = app.test_client()

    response = client.post("/server/demo-paper/auto", data={"rollback": "1"})
    assert response.status_code == 302
    job_id = response.headers["Location"].split("job=")[-1]

    # il job gira in un thread: aspettiamo che finisca
    for _ in range(40):
        payload = client.get(f"/api/job/{job_id}").get_json()
        if payload.get("status") in ("done", "error", "rolledback"):
            break
        import time
        time.sleep(0.3)

    assert payload["status"] == "done"
    assert "riavvio manuale" in (payload.get("result", {}).get("detail") or "")
    root = servers / "demo-paper"
    assert not (root / "plugins" / "ShopGUI.jar").exists()
    assert (root / "mods" / "sodium-fabric-1.20.1-0.5.3.jar").exists()


def test_quarantine_page_after_repair(env):
    cfg, servers = env
    client = create_app(cfg).test_client()
    client.post("/server/demo-paper/auto", data={"rollback": "1"})
    import time
    time.sleep(2)
    body = client.get("/quarantine").get_data(as_text=True)
    assert "ShopGUI.jar" in body

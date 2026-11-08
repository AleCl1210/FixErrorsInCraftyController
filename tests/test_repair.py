"""Test della quarantena, del backup e del rollback."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.analyzer import Action, Analyzer
from app.config import Config
from app.crafty import Server
from app.repair import RepairEngine
from app.store import Store


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "server"
    (root / "mods").mkdir(parents=True)
    (root / "plugins").mkdir()
    (root / "logs").mkdir()
    for name in ("jei.jar", "sodium.jar"):
        with zipfile.ZipFile(root / "mods" / name, "w") as zf:
            zf.writestr("fabric.mod.json", '{"id":"%s","version":"1","name":"%s","depends":{"minecraft":"1.20.x"}}'
                        % (name[:-4], name[:-4]))
    (root / "logs" / "latest.log").write_text("avvio\n", encoding="utf-8")

    cfg = Config()
    cfg.data_dir = str(tmp_path / "data")
    cfg.boot_wait_seconds = 1
    cfg.create_backup_before_fix = True
    cfg.auto_rollback = True

    class FakeClient:
        configured = True
        calls: list[str] = []

        def refresh_status(self, server):
            server.running = False
            return server

        def action(self, server_id, action):
            FakeClient.calls.append(action)
            return {"ok": True}

        def get_log(self, server_id, file=True):
            return (root / "logs" / "latest.log").read_text(encoding="utf-8") if (root / "logs" / "latest.log").exists() else ""

    store = Store(cfg.data_dir)
    analyzer = Analyzer(cfg)
    engine = RepairEngine(cfg, FakeClient(), store, analyzer, updater=None)
    server = Server(server_id="srv", name="Server di prova", path=str(root))
    return cfg, root, server, engine, store, FakeClient


def test_quarantine_and_restore(env):
    cfg, root, server, engine, store, client = env
    action = Action(id="a1", kind="quarantine", target="mods/jei.jar")
    job = engine.apply(server, [action], auto_start=False, verify=False)
    assert job.status == "done"
    assert not (root / "mods" / "jei.jar").exists()

    manifests = store.manifests("srv")
    assert len(manifests) == 1
    restored, errors = store.restore(manifests[0].id, server_root=str(root))
    assert restored == 1 and not errors
    assert (root / "mods" / "jei.jar").exists()


def test_backup_created(env):
    cfg, root, server, engine, store, client = env
    engine.apply(server, [Action(id="a1", kind="quarantine", target="mods/sodium.jar")],
                 auto_start=False, verify=False)
    backups = store.backups("srv")
    assert backups, "nessun backup creato"


def test_move_action(env):
    cfg, root, server, engine, store, client = env
    action = Action(id="a1", kind="move", target="mods/jei.jar", param="plugins")
    engine.apply(server, [action], auto_start=False, verify=False)
    assert (root / "plugins" / "jei.jar").exists()
    assert not (root / "mods" / "jei.jar").exists()


def test_rollback_when_still_broken(env):
    cfg, root, server, engine, store, client = env
    # il log contiene un errore critico che resta anche dopo la quarantena
    (root / "logs" / "latest.log").write_text(
        "[main/ERROR] Could not load 'plugins/ShopGUI.jar' in folder 'plugins'\n", encoding="utf-8")
    action = Action(id="a1", kind="quarantine", target="mods/jei.jar")
    job = engine.apply(server, [action], auto_start=True, verify=True, rollback=True)
    assert job.status in ("rolledback", "error")
    if job.status == "rolledback":
        assert (root / "mods" / "jei.jar").exists(), "il rollback deve rimettere il file"


def test_success_when_log_is_clean(env):
    cfg, root, server, engine, store, client = env
    (root / "logs" / "latest.log").write_text("[Server thread/INFO] Done (4.2s)!\n", encoding="utf-8")
    action = Action(id="a1", kind="quarantine", target="mods/jei.jar")
    job = engine.apply(server, [action], auto_start=True, verify=True, rollback=True)
    assert job.status == "done"
    assert not (root / "mods" / "jei.jar").exists()


def test_audit_log(env):
    cfg, root, server, engine, store, client = env
    engine.apply(server, [Action(id="a1", kind="quarantine", target="mods/jei.jar")],
                 auto_start=False, verify=False)
    entries = store.read_audit("srv")
    assert any(e["action"] == "quarantine" for e in entries)

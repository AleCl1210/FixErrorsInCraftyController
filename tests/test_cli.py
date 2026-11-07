"""Test dello script singolo crafty-medic.py (quello che si usa dentro il container di Crafty)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "crafty-medic.py"


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    base = tmp_path_factory.mktemp("cli")
    servers = base / "servers"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "make_demo.py"),
                    "--out", str(servers), "--force"], check=True)
    (servers / "servers.json").unlink(missing_ok=True)  # la demo non serve allo script
    return base, servers, base / "home"


def run(*args, home=None, servers=None):
    command = [sys.executable, str(SCRIPT)]
    if servers:
        command += ["--dir", str(servers)]
    if home:
        command += ["--home", str(home)]
    command += list(args)
    result = subprocess.run(command, capture_output=True, text=True)
    return result


def test_scan_json(sandbox):
    base, servers, home = sandbox
    result = run("scan", "--json", home=home, servers=servers)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    by_id = {item["id"]: item for item in data}
    assert set(by_id) == {"demo-fabric", "demo-paper", "demo-forge"}

    rules = {f["rule"] for f in by_id["demo-fabric"]["findings"]}
    assert "fabric_mc_version" in rules
    assert "fabric_missing_dep" in rules
    assert by_id["demo-fabric"]["mc_version"] == "1.20.1"
    assert by_id["demo-fabric"]["loader"] == "fabric"

    rules = {f["rule"] for f in by_id["demo-paper"]["findings"]}
    assert "plugin_unsupported_api" in rules
    assert "plugin_unknown_dep" in rules
    assert "scan_corrupt" in rules
    assert "scan_nested" in rules
    assert by_id["demo-paper"]["loader"] == "paper"

    rules = {f["rule"] for f in by_id["demo-forge"]["findings"]}
    assert "forge_missing_dep" in rules
    assert by_id["demo-forge"]["loader"] == "forge"


def test_fix_quarantines_and_moves(sandbox):
    base, servers, home = sandbox
    result = run("fix", "demo-paper", "--auto", home=home, servers=servers)
    assert result.returncode == 0, result.stderr
    assert "in quarantena: plugins/ShopGUI.jar" in result.stdout
    assert "spostato plugins/sodium-fabric-1.20.1-0.5.3.jar -> mods/" in result.stdout

    root = servers / "demo-paper"
    assert not (root / "plugins" / "ShopGUI.jar").exists()
    assert (root / "mods" / "sodium-fabric-1.20.1-0.5.3.jar").exists()
    assert (root / "plugins" / "Vault.jar").exists()  # tolto dalla sottocartella

    # i file non sono persi: stanno nella quarantena
    batches = [d for d in (home / "quarantine").iterdir() if d.is_dir()]
    assert batches
    quarantined = {p.name for b in batches for p in b.rglob("*.jar")}
    assert "ShopGUI.jar" in quarantined


def test_undo_restores_everything(sandbox):
    base, servers, home = sandbox
    result = run("undo", "--yes", home=home, servers=servers)
    assert result.returncode == 0, result.stderr
    root = servers / "demo-paper"
    assert (root / "plugins" / "ShopGUI.jar").exists()
    assert (root / "plugins" / "sodium-fabric-1.20.1-0.5.3.jar").exists()
    assert not (root / "mods" / "sodium-fabric-1.20.1-0.5.3.jar").exists()
    assert (root / "plugins" / "old" / "Vault.jar").exists()


def test_history_and_quarantine_commands(sandbox):
    base, servers, home = sandbox
    history = run("history", home=home)
    assert history.returncode == 0
    assert "ripristino del lotto" in history.stdout

    quarantine = run("quarantine", home=home)
    assert quarantine.returncode == 0
    assert "Quarantena" in quarantine.stdout


def test_interactive_selection_accepts_ranges(sandbox):
    """'1-3' deve selezionare i primi tre problemi, come '1,2,3'."""
    base, servers, home = sandbox
    # rimette la demo nello stato iniziale
    subprocess.run([sys.executable, str(ROOT / "scripts" / "make_demo.py"),
                    "--out", str(servers), "--force"], check=True)
    (servers / "servers.json").unlink(missing_ok=True)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--dir", str(servers), "--home", str(home),
         "fix", "demo-paper"],
        input="1-3\n", capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "riparo: 1, 2, 3" in result.stdout

    root = servers / "demo-paper"
    assert not (root / "plugins" / "EssentialsX-2.19.0.jar").exists()
    assert not (root / "plugins" / "ShopGUI.jar").exists()
    assert not (root / "plugins" / "broken-plugin.jar").exists()
    # il 4 e il 5 non sono stati toccati
    assert (root / "plugins" / "sodium-fabric-1.20.1-0.5.3.jar").exists()
    assert (root / "plugins" / "old" / "Vault.jar").exists()

    # e con "tutti" parte anche il resto
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--dir", str(servers), "--home", str(home),
         "fix", "demo-paper"],
        input="tutti\n", capture_output=True, text=True,
    )
    assert (root / "mods" / "sodium-fabric-1.20.1-0.5.3.jar").exists()
    assert (root / "plugins" / "Vault.jar").exists()


def test_parse_selection():
    import importlib.util
    spec = importlib.util.spec_from_file_location("cm_cli", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    parse = module.parse_selection
    assert parse("1-3", 5) == [1, 2, 3]
    assert parse("2-", 5) == [2, 3, 4, 5]
    assert parse("-3", 5) == [1, 2, 3]
    assert parse("1,3", 5) == [1, 3]
    assert parse("tutti", 5) == [1, 2, 3, 4, 5]
    assert parse("1,3-5,8", 10) == [1, 3, 4, 5, 8]
    assert parse("7", 5) == []
    assert parse("abc", 5) == []


def test_scan_without_servers(tmp_path):
    result = run("scan", home=tmp_path / "home", servers=tmp_path / "vuoto")
    assert result.returncode == 1
    assert "nessun server" in result.stdout or "nessun server" in result.stderr

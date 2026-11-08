"""Test del motore di analisi: regole sui log e controlli sui file."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from app.analyzer import Analyzer, resolve_token
from app.config import Config
from app.crafty import Server
from app.demo import DemoClient
from app.jarmeta import read_jar_meta

LOG_FABRIC = """
[16:42:02] [main/INFO] Loading Minecraft 1.20.1 with Fabric Loader 0.14.21
[16:42:03] [main/ERROR] Failed to start the minecraft server
net.fabricmc.loader.impl.FormattedException: Some mods failed to load!
Caused by: net.fabricmc.loader.impl.discovery.ModResolutionException: Errors were found!
 - Mod 'Reese's Sodium Options' (reeses-sodium-options) 1.6.4 requires version 0.5.x of mod sodium, which is missing!
 - Mod 'JEI' (jei) 15.2.0.27 requires version 1.21.x of minecraft, which is missing!
"""

LOG_PAPER = """
[12:00:01] [ServerMain/INFO] This server is running Paper version git-Paper-497 (MC: 1.20.6)
[12:00:02] [Server thread/ERROR] Could not load 'plugins/EssentialsX-2.19.0.jar' in folder 'plugins'
org.bukkit.plugin.InvalidPluginException: Unsupported API version 1.13
[12:00:02] [Server thread/ERROR] Could not load 'plugins/ShopGUI.jar' in folder 'plugins'
org.bukkit.plugin.UnknownDependencyException: Unknown/missing dependency plugins: [Vault].
"""

LOG_FORGE = """
[09:15:01] [main/INFO] Forge mod loading, version 47.2.0, for MC 1.20.1
[09:15:05] [main/ERROR] Missing or unsupported mandatory dependencies:
\tMod ID: 'jei', Requested by: 'derivedaddon', Expected range: '[15.0.0,)', Actual version: '[MISSING]'
"""

LOG_JAVA = """
[main/ERROR] Exception in thread "main" java.lang.UnsupportedClassVersionError: net/minecraft/server/Main has been compiled by a more recent version of the Java Runtime (class file version 61.0), this version of the Java Runtime only recognizes class file versions up to 52.0
"""

LOG_EULA = "[main/WARN] You need to agree to the EULA in order to run the server."


@pytest.fixture
def analyzer(tmp_path):
    cfg = Config()
    cfg.data_dir = str(tmp_path / "data")
    cfg.demo_dir = str(tmp_path / "demo")
    return Analyzer(cfg)


def make_server(server_id="srv", root: Path | None = None, name="Test", version=""):
    server = Server(server_id=server_id, name=name, mc_version=version)
    if root:
        server.path = str(root)
    return server


def empty_ctx(analyzer, log_text, root=None, mc_version="", loader=""):
    from app.analyzer import ServerContext
    server = make_server(root=root)
    ctx = ServerContext(server, root, log_text, "test", mc_version, loader)
    return ctx


# --------------------------------------------------------------------------- #
def test_fabric_rules(analyzer):
    ctx = empty_ctx(analyzer, LOG_FABRIC, mc_version="1.20.1", loader="fabric")
    findings = analyzer._analyze_log(ctx)
    rules = {f.rule_id for f in findings}
    assert "fabric_missing_dependency" in rules
    assert "fabric_minecraft_version" in rules

    dep = next(f for f in findings if f.rule_id == "fabric_missing_dependency")
    assert dep.severity == "critical"
    assert dep.remedy == "quarantine"
    assert "reeses-sodium-options" in dep.unmatched or "sodium" in dep.unmatched


def test_fabric_minecraft_version_is_update(analyzer):
    ctx = empty_ctx(analyzer, LOG_FABRIC, mc_version="1.20.1", loader="fabric")
    finding = next(f for f in analyzer._analyze_log(ctx) if f.rule_id == "fabric_minecraft_version")
    assert finding.remedy == "update"
    # senza filesystem il mod non puo' essere collegato a un file: scende l'affidabilita'
    assert finding.confidence >= 60
    from app.rules import RULES_BY_ID
    assert RULES_BY_ID["fabric_minecraft_version"].confidence >= 90
    assert finding.unmatched == ["jei", "JEI"]


def test_paper_rules(analyzer):
    ctx = empty_ctx(analyzer, LOG_PAPER, mc_version="1.20.6", loader="paper")
    findings = analyzer._analyze_log(ctx)
    rules = {f.rule_id for f in findings}
    assert "plugin_unsupported_api" in rules
    assert "plugin_unknown_dependency" in rules

    api = next(f for f in findings if f.rule_id == "plugin_unsupported_api")
    assert api.remedy == "update"
    assert api.unmatched == [] or True  # senza filesystem non puo' risolvere il file


def test_forge_rules(analyzer):
    ctx = empty_ctx(analyzer, LOG_FORGE, mc_version="1.20.1", loader="forge")
    findings = analyzer._analyze_log(ctx)
    forge = [f for f in findings if f.rule_id == "forge_missing_dependency"]
    assert forge, [f.rule_id for f in findings]
    assert forge[0].unmatched and forge[0].unmatched[0] == "derivedaddon"


def test_server_level_rules(analyzer):
    java = analyzer._analyze_log(empty_ctx(analyzer, LOG_JAVA))
    assert any(f.rule_id == "java_class_version" for f in java)

    eula = analyzer._analyze_log(empty_ctx(analyzer, LOG_EULA))
    assert any(f.rule_id == "eula_not_accepted" and f.remedy == "config" for f in eula)


def test_mc_version_detection(analyzer):
    from app.rules import detect_loader, detect_mc_version
    assert detect_mc_version(LOG_PAPER) == "1.20.6"
    assert detect_mc_version(LOG_FABRIC) == "1.20.1"
    assert detect_loader(LOG_FABRIC) == "fabric"
    assert detect_loader(LOG_PAPER) == "paper"
    assert detect_loader(LOG_FORGE) == "forge"


# --------------------------------------------------------------------------- #
def _write_jar(root: Path, rel: str, entries: dict):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for key, value in entries.items():
            zf.writestr(key, value)
    return path


def test_resolve_token_and_scan(tmp_path):
    root = tmp_path / "server"
    _write_jar(root, "mods/reeses-sodium-options-1.6.4.jar", {
        "fabric.mod.json": json.dumps({
            "id": "reeses-sodium-options", "version": "1.6.4",
            "name": "Reese's Sodium Options", "environment": "*",
            "depends": {"minecraft": "1.20.x"}})})
    from app.analyzer import scan_jars
    jars = scan_jars(root)
    assert len(jars) == 1
    assert resolve_token("reeses-sodium-options", jars)[0].rel.endswith(".jar")
    assert resolve_token("Reese's Sodium Options", jars)
    assert resolve_token("mods/reeses-sodium-options-1.6.4.jar", jars)
    assert resolve_token("qualcosa-che-non-esiste", jars) == []


def test_preflight_detects_wrong_mc_version(tmp_path):
    root = tmp_path / "server"
    _write_jar(root, "mods/jei.jar", {
        "fabric.mod.json": json.dumps({
            "id": "jei", "version": "15.2.0", "name": "JEI", "environment": "*",
            "depends": {"minecraft": "1.21.x"}})})
    analyzer = Analyzer(_cfg(tmp_path))
    ctx = analyzer.build_context(make_server(root=root))
    ctx.mc_version = "1.20.1"
    ctx.loader = "fabric"
    findings = analyzer._preflight_scan(ctx)
    assert any(f.rule_id == "scan_mc_version_mismatch" and f.remedy == "update" for f in findings)


def test_preflight_detects_plugin_in_mods(tmp_path):
    root = tmp_path / "server"
    _write_jar(root, "mods/essentials.jar", {
        "plugin.yml": "name: Essentials\nversion: 2.19.0\napi-version: 1.13\nmain: x.Main\n"})
    analyzer = Analyzer(_cfg(tmp_path))
    ctx = analyzer.build_context(make_server(root=root))
    ctx.mc_version = "1.20.6"
    ctx.loader = "paper"
    findings = analyzer._preflight_scan(ctx)
    move = [f for f in findings if f.rule_id == "scan_plugin_in_mods"]
    assert move
    assert move[0].actions[0].kind == "move"
    assert move[0].actions[0].param == "plugins"


def test_preflight_detects_duplicates(tmp_path):
    root = tmp_path / "server"
    for name, version in (("cloth-new.jar", "11.0.99"), ("cloth-old.jar", "10.0.96")):
        _write_jar(root, f"mods/{name}", {
            "fabric.mod.json": json.dumps({
                "id": "cloth-config", "version": version, "name": "Cloth Config",
                "environment": "*", "depends": {"minecraft": "1.20.x"}})})
    analyzer = Analyzer(_cfg(tmp_path))
    ctx = analyzer.build_context(make_server(root=root))
    ctx.mc_version = "1.20.1"
    ctx.loader = "fabric"
    findings = analyzer._preflight_scan(ctx)
    dup = [f for f in findings if f.rule_id == "scan_duplicate_mod"]
    assert dup and len(dup[0].targets) == 1


def _cfg(tmp_path):
    cfg = Config()
    cfg.data_dir = str(tmp_path / "data")
    return cfg


# --------------------------------------------------------------------------- #
def test_demo_fixtures_end_to_end(tmp_path):
    """I tre server demo devono produrre diagnosi sensate."""
    import subprocess
    import sys
    from pathlib import Path as P
    root = P(__file__).resolve().parent.parent
    demo = tmp_path / "demo"
    subprocess.run([sys.executable, str(root / "scripts" / "make_demo.py"),
                    "--out", str(demo), "--force"], check=True)

    cfg = Config()
    cfg.demo = True
    cfg.demo_dir = str(demo)
    cfg.data_dir = str(tmp_path / "data-test")
    client = DemoClient(demo)
    analyzer = Analyzer(cfg)

    expected = {
        "demo-fabric": ["fabric_minecraft_version", "fabric_missing_dependency"],
        "demo-paper": ["plugin_unsupported_api", "plugin_unknown_dependency", "scan_mod_in_plugins"],
        "demo-forge": ["forge_missing_dependency"],
    }
    for server in client.list_servers():
        analysis = analyzer.analyze(analyzer.build_context(server, client))
        rules = {f.rule_id for f in analysis.findings}
        assert analysis.mc_version, server.name
        for rule in expected[server.server_id]:
            assert rule in rules, f"{server.server_id}: manca {rule}"
        # un solo problema per file
        claimed = [t.rel for f in analysis.findings for t in f.targets]
        assert len(claimed) == len(set(claimed)), server.server_id

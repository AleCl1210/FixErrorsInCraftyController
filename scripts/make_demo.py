#!/usr/bin/env python3
"""Genera i dati dimostrativi in demo/ (server finti, log di crash, jar finti).

Uso:  python3 scripts/make_demo.py [--force]
"""
from __future__ import annotations

import json
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "demo"


def write_jar(path: Path, entries: dict[str, str], raw: bytes | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        path.write_bytes(raw)
        return
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)


def fabric_mod(path: Path, mod_id: str, name: str, version: str, depends: dict, environment: str = "*") -> None:
    data = {
        "schemaVersion": 1,
        "id": mod_id,
        "version": version,
        "name": name,
        "description": f"{name} (fixture dimostrativa)",
        "environment": environment,
        "depends": depends,
        "entrypoints": {"main": [f"{mod_id.replace('-', '')}.Main"]},
    }
    write_jar(path, {"fabric.mod.json": json.dumps(data, indent=2)})


def forge_mod(path: Path, mod_id: str, name: str, version: str, mc_range: str, deps: list[tuple[str, str]] | None = None) -> None:
    lines = [
        'modLoader = "javafml"',
        'loaderVersion = "[47,)"',
        'license = "MIT"',
        "",
        "[[mods]]",
        f'modId = "{mod_id}"',
        f'version = "{version}"',
        f'displayName = "{name}"',
        "",
        "[[dependencies." + mod_id + "]]",
        '    modId = "minecraft"',
        "    mandatory = true",
        f'    versionRange = "{mc_range}"',
        '    ordering = "NONE"',
        '    side = "BOTH"',
    ]
    for dep_id, dep_range in deps or []:
        lines += [
            "",
            "[[dependencies." + mod_id + "]]",
            f'    modId = "{dep_id}"',
            "    mandatory = true",
            f'    versionRange = "{dep_range}"',
            '    ordering = "AFTER"',
            '    side = "BOTH"',
        ]
    write_jar(path, {"META-INF/mods.toml": "\n".join(lines) + "\n"})


def plugin_jar(path: Path, name: str, version: str, api_version: str, depend: list[str] | None = None, main: str = "") -> None:
    lines = [
        f"name: {name}",
        f"version: {version}",
        f"api-version: {api_version}",
        f"main: {main or ('me.demo.' + name.lower() + '.Main')}",
        "authors: [Demo]",
    ]
    if depend:
        lines.append("depend: [" + ", ".join(depend) + "]")
    write_jar(path, {"plugin.yml": "\n".join(lines) + "\n"})


# --------------------------------------------------------------------------- #
BOOT_FABRIC = """[16:42:01] [main/INFO] Loading Minecraft 1.20.1 with Fabric Loader 0.14.21
[16:42:01] [main/INFO] Fabric is preparing JARs on first launch, this may take a few seconds...
[16:42:02] [main/INFO] Loading 5 mods:
\t- fabricloader 0.14.21
\t- minecraft 1.20.1
\t- jei 15.2.0.27
\t- reeses-sodium-options 1.6.4
\t- cloth-config 11.0.99
[16:42:02] [main/INFO] Environment: Environment[sessionHost=https://sessionserver.mojang.com, servicesHost=https://api.minecraftservices.com, name=PROD]
"""

CRASH_FABRIC = """
[16:42:03] [main/ERROR] Failed to start the minecraft server
net.fabricmc.loader.impl.FormattedException: Some mods failed to load!
\tat net.fabricmc.loader.impl.FabricLoaderImpl.load(FabricLoaderImpl.java:195) ~[fabric-loader-0.14.21.jar:?]
\tat net.fabricmc.loader.impl.launch.knot.Knot.init(Knot.java:148) ~[fabric-loader-0.14.21.jar:?]
Caused by: net.fabricmc.loader.impl.discovery.ModResolutionException: Errors were found!
 - Mod 'Reese's Sodium Options' (reeses-sodium-options) 1.6.4 requires version 0.5.x of mod sodium, which is missing!
\t- You must install version 0.5.x of sodium.
 - Mod 'JEI' (jei) 15.2.0.27 requires version 1.21.x of minecraft, which is missing!
\t- You are running Minecraft 1.20.1, so you need a version of JEI built for 1.20.1.
\tat net.fabricmc.loader.impl.discovery.ModResolver.findCompatibleSet(ModResolver.java:180) ~[fabric-loader-0.14.21.jar:?]
\tat net.fabricmc.loader.impl.discovery.ModResolver.resolve(ModResolver.java:805) ~[fabric-loader-0.14.21.jar:?]
"""

CLEAN_FABRIC = """
[16:42:03] [main/INFO] Starting minecraft server version 1.20.1
[16:42:03] [main/INFO] Loading properties
[16:42:03] [main/INFO] Default game type: SURVIVAL
[16:42:04] [main/INFO] Preparing level "world"
[16:42:09] [Server thread/INFO] Done (6.1s)! For help, type "help"
"""

BOOT_PAPER = """[12:00:01] [ServerMain/INFO] This server is running Paper version git-Paper-497 (MC: 1.20.6) (Implementing API version 1.20.6-R0.1-SNAPSHOT)
[12:00:01] [ServerMain/INFO] Environment: Environment[sessionHost=https://sessionserver.mojang.com, servicesHost=https://api.minecraftservices.com, name=PROD]
[12:00:02] [Server thread/INFO] Loading 4 plugins
"""

CRASH_PAPER = """
[12:00:02] [Server thread/ERROR] Could not load 'plugins/EssentialsX-2.19.0.jar' in folder 'plugins'
org.bukkit.plugin.InvalidPluginException: Unsupported API version 1.13
\tat org.bukkit.plugin.java.JavaPluginLoader.loadPlugin(JavaPluginLoader.java:146) ~[paper-api-1.20.6-R0.1-SNAPSHOT.jar:?]
\tat org.bukkit.plugin.SimplePluginManager.loadPlugin(SimplePluginManager.java:394) ~[paper-api-1.20.6-R0.1-SNAPSHOT.jar:?]
[12:00:02] [Server thread/ERROR] Could not load 'plugins/ShopGUI.jar' in folder 'plugins'
org.bukkit.plugin.UnknownDependencyException: Unknown/missing dependency plugins: [Vault]. Please download and install Vault to run this plugin.
\tat org.bukkit.plugin.java.JavaPluginLoader.loadPlugin(JavaPluginLoader.java:170) ~[paper-api-1.20.6-R0.1-SNAPSHOT.jar:?]
\tat org.bukkit.plugin.SimplePluginManager.loadPlugin(SimplePluginManager.java:394) ~[paper-api-1.20.6-R0.1-SNAPSHOT.jar:?]
[12:00:02] [Server thread/ERROR] Could not load 'plugins/sodium-fabric-1.20.1-0.5.3.jar' in folder 'plugins'
org.bukkit.plugin.InvalidDescriptionException: Invalid plugin.yml
\tat org.bukkit.plugin.java.JavaPluginLoader.getPluginDescription(JavaPluginLoader.java:240) ~[paper-api-1.20.6-R0.1-SNAPSHOT.jar:?]
[12:00:03] [Server thread/WARN] Failed to load plugin broken-plugin.jar (non e' un jar valido)
"""

CLEAN_PAPER = """
[12:00:03] [Server thread/INFO] [Vault] Enabling Vault v1.7.3
[12:00:03] [Server thread/INFO] Preparing level "world"
[12:00:05] [Server thread/INFO] Done (3.8s)! For help, type "help"
"""

BOOT_FORGE = """[09:15:01] [main/INFO] Forge mod loading, version 47.2.0, for MC 1.20.1
[09:15:02] [main/INFO] Loading 41 mods
[09:15:02] [main/INFO] Environment: Environment[sessionHost=https://sessionserver.mojang.com, servicesHost=https://api.minecraftservices.com, name=PROD]
"""

CRASH_FORGE = """
[09:15:05] [main/ERROR] Missing or unsupported mandatory dependencies:
\tMod ID: 'jei', Requested by: 'derivedaddon', Expected range: '[15.0.0,)', Actual version: '[MISSING]'
\tMod ID: 'minecraft', Requested by: 'oldmod', Expected range: '[1.19.4]', Actual version: '1.20.1'
\tat net.minecraftforge.fml.ModList.lambda$verifyDependencyVersions$8(ModList.java:...)
[09:15:05] [main/FATAL] Failed to start the minecraft server
net.minecraftforge.fml.ModLoadingException: Loading errors encountered
"""

CLEAN_FORGE = """
[09:15:06] [main/INFO] Starting Minecraft server on 1.20.1
[09:15:06] [Server thread/INFO] Preparing level "world"
[09:15:14] [Server thread/INFO] Done (8.4s)! For help, type "help"
"""

SERVERS = [
    {"server_id": "demo-fabric", "name": "Moddato Fabric 1.20.1", "type": "minecraft-java",
     "version": "1.20.1", "path": "demo-fabric", "status": "stopped"},
    {"server_id": "demo-paper", "name": "Paper Plugin 1.20.6", "type": "minecraft-java",
     "version": "1.20.6", "path": "demo-paper", "status": "stopped"},
    {"server_id": "demo-forge", "name": "Forge 1.20.1 Survival", "type": "minecraft-java",
     "version": "1.20.1", "path": "demo-forge", "status": "stopped"},
]

BAD_FILES = {
    "demo-fabric": ["mods/reeses-sodium-options-1.6.4.jar", "mods/jei-1.21.1.jar"],
    "demo-paper": ["plugins/EssentialsX-2.19.0.jar", "plugins/ShopGUI.jar", "plugins/sodium-fabric-1.20.1-0.5.3.jar"],
    "demo-forge": ["mods/derivedaddon-1.0.0.jar", "mods/oldmod-1.19.4.jar"],
}

BOOT = {"demo-fabric": BOOT_FABRIC, "demo-paper": BOOT_PAPER, "demo-forge": BOOT_FORGE}
CRASH = {"demo-fabric": CRASH_FABRIC, "demo-paper": CRASH_PAPER, "demo-forge": CRASH_FORGE}
CLEAN = {"demo-fabric": CLEAN_FABRIC, "demo-paper": CLEAN_PAPER, "demo-forge": CLEAN_FORGE}


def build(target: Path) -> None:
    DEMO = target
    if DEMO.exists():
        shutil.rmtree(DEMO)
    DEMO.mkdir(parents=True)

    (DEMO / "servers.json").write_text(json.dumps(SERVERS, indent=2), encoding="utf-8")

    for server in SERVERS:
        sid = server["server_id"]
        base = DEMO / sid
        (base / "logs").mkdir(parents=True, exist_ok=True)
        (base / "logs" / "boot.log").write_text(BOOT[sid], encoding="utf-8")
        (base / "logs" / "crash.log").write_text(CRASH[sid], encoding="utf-8")
        (base / "logs" / "clean.log").write_text(CLEAN[sid], encoding="utf-8")
        (base / "logs" / "latest.log").write_text(BOOT[sid] + CRASH[sid], encoding="utf-8")
        (base / "bad_files.txt").write_text("\n".join(BAD_FILES[sid]) + "\n", encoding="utf-8")
        (base / "server.properties").write_text(
            f"# demo\nlevel-name=world\nserver-port={25565 + SERVERS.index(server)}\n", encoding="utf-8")
        (base / "eula.txt").write_text("eula=true\n", encoding="utf-8")
        (base / "server.jar").write_bytes(b"fake-server-jar\n")

    # ---------------- Fabric ----------------
    mods = DEMO / "demo-fabric" / "mods"
    fabric_mod(mods / "reeses-sodium-options-1.6.4.jar", "reeses-sodium-options", "Reese's Sodium Options",
               "1.6.4", {"minecraft": "1.20.x", "sodium": "0.5.x", "fabricloader": ">=0.14"})
    fabric_mod(mods / "jei-1.21.1.jar", "jei", "JEI", "15.2.0.27", {"minecraft": "1.21.x", "fabricloader": ">=0.15"})
    fabric_mod(mods / "cloth-config-11.0.99.jar", "cloth-config", "Cloth Config", "11.0.99",
               {"minecraft": "1.20.x", "fabricloader": ">=0.14"})
    fabric_mod(mods / "cloth-config-10.0.96.jar", "cloth-config", "Cloth Config", "10.0.96",
               {"minecraft": "1.19.4", "fabricloader": ">=0.14"})
    fabric_mod(mods / "xaeros-minimap-23.9.jar", "xaerominimap", "Xaero's Minimap", "23.9.1",
               {"minecraft": "1.20.x", "fabricloader": ">=0.14"}, environment="client")

    # ---------------- Paper ----------------
    plugins = DEMO / "demo-paper" / "plugins"
    plugin_jar(plugins / "EssentialsX-2.19.0.jar", "Essentials", "2.19.0", "1.13",
               main="com.earth2me.essentials.Essentials")
    plugin_jar(plugins / "ShopGUI.jar", "ShopGUIPlus", "1.90.0", "1.20", depend=["Vault"])
    write_jar(plugins / "broken-plugin.jar", {}, raw=b"questo non e' uno zip\n")
    fabric_mod(plugins / "sodium-fabric-1.20.1-0.5.3.jar", "sodium", "Sodium", "0.5.3",
               {"minecraft": "1.20.x", "fabricloader": ">=0.14"})
    plugin_jar(plugins / "old" / "Vault.jar", "Vault", "1.7.3", "1.20")

    # ---------------- Forge ----------------
    fmods = DEMO / "demo-forge" / "mods"
    forge_mod(fmods / "derivedaddon-1.0.0.jar", "derivedaddon", "Derived Addon", "1.0.0", "[1.20.1,1.21)",
              deps=[("jei", "[15.0.0,)")])
    forge_mod(fmods / "oldmod-1.19.4.jar", "oldmod", "Old Mod", "3.2.1", "[1.19.4]")
    forge_mod(fmods / "twilightforest-1.20.1.jar", "twilightforest", "The Twilight Forest", "4.3.1897",
              "[1.20.1,1.21)", deps=[("forge", "[47,)")])

    print(f"Demo creata in {DEMO}")
    for server in SERVERS:
        print(f"  - {server['name']} ({server['server_id']})")


if __name__ == "__main__":
    out = DEMO
    if "--out" in sys.argv:
        out = Path(sys.argv[sys.argv.index("--out") + 1])
    if out.exists() and "--force" not in sys.argv:
        print(f"{out} esiste gia': uso --force per rigenerarla")
        sys.exit(0)
    build(out)

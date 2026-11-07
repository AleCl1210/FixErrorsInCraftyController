import zipfile

import pytest

from app.jarmeta import read_jar_meta


@pytest.fixture
def jar_factory(tmp_path):
    def _factory(name, entries=None, raw=None):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if raw is not None:
            path.write_bytes(raw)
            return path
        with zipfile.ZipFile(path, "w") as zf:
            for key, value in (entries or {}).items():
                zf.writestr(key, value)
        return path

    return _factory


def test_fabric_metadata(jar_factory):
    import json
    jar = jar_factory("mods/reeses.jar", {
        "fabric.mod.json": json.dumps({
            "schemaVersion": 1,
            "id": "reeses-sodium-options",
            "version": "1.6.4",
            "name": "Reese's Sodium Options",
            "environment": "*",
            "depends": {"minecraft": "1.20.x", "sodium": "0.5.x", "fabricloader": ">=0.14"},
        })
    })
    meta = read_jar_meta(jar, "mods/reeses.jar")
    assert meta.loader == "fabric"
    assert meta.mod_id == "reeses-sodium-options"
    assert meta.mc_spec == "1.20.x"
    assert meta.depends["sodium"] == "0.5.x"
    assert meta.valid


def test_forge_metadata(jar_factory):
    toml = """
modLoader = "javafml"
loaderVersion = "[47,)"
[[mods]]
modId = "derivedaddon"
version = "1.0.0"
displayName = "Derived Addon"
[[dependencies.derivedaddon]]
    modId = "minecraft"
    mandatory = true
    versionRange = "[1.20.1,1.21)"
    side = "BOTH"
[[dependencies.derivedaddon]]
    modId = "jei"
    mandatory = true
    versionRange = "[15.0.0,)"
    side = "BOTH"
"""
    jar = jar_factory("mods/derived.jar", {"META-INF/mods.toml": toml})
    meta = read_jar_meta(jar, "mods/derived.jar")
    assert meta.loader == "forge"
    assert meta.mod_id == "derivedaddon"
    assert meta.name == "Derived Addon"
    assert meta.mc_spec == "[1.20.1,1.21)"
    assert meta.depends["jei"] == "[15.0.0,)"


def test_neoforge_detected(jar_factory):
    jar = jar_factory("mods/neo.jar", {"META-INF/neoforge.mods.toml": '[[mods]]\nmodId="neotest"\n'})
    meta = read_jar_meta(jar, "mods/neo.jar")
    assert meta.loader == "neoforge"


def test_plugin_yml(jar_factory):
    yml = """
name: Essentials
version: 2.19.0
api-version: 1.13
main: com.earth2me.essentials.Essentials
depend: [Vault]
softdepend: [LuckPerms]
"""
    jar = jar_factory("plugins/EssentialsX-2.19.0.jar", {"plugin.yml": yml})
    meta = read_jar_meta(jar, "plugins/EssentialsX-2.19.0.jar")
    assert meta.loader == "bukkit"
    assert meta.name == "Essentials"
    assert meta.api_version == "1.13"
    assert "Vault" in meta.depends
    assert "LuckPerms" in meta.soft_depends


def test_corrupt_jar(jar_factory):
    jar = jar_factory("plugins/broken.jar", raw=b"not a zip at all")
    meta = read_jar_meta(jar, "plugins/broken.jar")
    assert meta.valid is False
    assert "corrotto" in meta.error or "zip" in meta.error


def test_unknown_jar(jar_factory):
    jar = jar_factory("mods/mystery.jar", {"readme.txt": "niente metadati"})
    meta = read_jar_meta(jar, "mods/mystery.jar")
    assert meta.loader == "unknown"
    assert meta.valid is True

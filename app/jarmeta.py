"""Lettura dei metadati dentro i .jar di mod e plugin.

Riconosce:
  * fabric.mod.json                          -> loader fabric
  * META-INF/mods.toml, neoforge.mods.toml   -> loader forge / neoforge
  * plugin.yml                               -> loader bukkit (spigot/paper)
  * paper-plugin.yml                         -> loader paper (1.20.5+)
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import tomllib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("crafty_medic.jarmeta")

try:  # PyYAML e' preferibile, ma non obbligatorio
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None

MAX_META_BYTES = 512 * 1024


@dataclass
class JarMeta:
    path: Path
    rel: str
    size: int = 0
    sha1: str = ""
    loader: str = "unknown"        # fabric | forge | neoforge | bukkit | paper | unknown
    mod_id: str = ""
    name: str = ""
    version: str = ""
    mc_spec: str = ""              # es. "1.20.x", "[1.20.1,1.21)"
    depends: dict[str, str] = field(default_factory=dict)
    soft_depends: list[str] = field(default_factory=list)
    environment: str = ""          # client | server | *
    api_version: str = ""          # plugin.yml
    main_class: str = ""
    valid: bool = True
    error: str = ""

    @property
    def label(self) -> str:
        return self.name or self.mod_id or self.path.name

    def as_dict(self) -> dict:
        data = dict(self.__dict__)
        data["path"] = str(self.path)
        return data


# --------------------------------------------------------------------------- #
# YAML minimale di riserva (solo per plugin.yml, che e' sempre molto semplice)
# --------------------------------------------------------------------------- #
def _mini_yaml(text: str) -> dict:
    """Parser YAML ultra-ridotto: mappe annidate per indentazione, liste '- x'."""
    root: dict = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    last_list_key: tuple[dict, str] | None = None

    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.strip().startswith("#"):
            continue
        if "\t" in raw_line[:8]:
            raw_line = raw_line.replace("\t", "    ")
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()

        if line.startswith("- ") or line == "-":
            if last_list_key:
                parent, key = last_list_key
                parent.setdefault(key, [])
                if isinstance(parent[key], list):
                    parent[key].append(_scalar(line[2:].strip()))
            continue

        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            stack = [(-1, root)]
        parent = stack[-1][1]

        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value:
            parent[key] = _scalar(value)
            last_list_key = None
        else:
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
            last_list_key = (parent, key)
    return root


def _scalar(value: str):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        return [item.strip().strip("\"'") for item in value[1:-1].split(",") if item.strip()]
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return value


def _load_yaml(text: str) -> dict:
    if yaml is not None:
        try:
            data = yaml.safe_load(text)
            return data if isinstance(data, dict) else {}
        except Exception:  # yaml non standard
            pass
    return _mini_yaml(text)


# --------------------------------------------------------------------------- #
def _read_entry(jar: zipfile.ZipFile, name: str) -> str:
    with jar.open(name) as handle:
        data = handle.read(MAX_META_BYTES)
    return data.decode("utf-8", errors="replace")


def _strip_colors(text: str) -> str:
    return re.sub(r"§[0-9a-fk-orA-FK-OR]", "", text)


def _find_name(zf: zipfile.ZipFile, candidates: tuple[str, ...]) -> str:
    names = set(zf.namelist())
    lowered = {name.lower(): name for name in names}
    for candidate in candidates:
        if candidate in names:
            return candidate
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
        # cerca per suffisso (es. "META-INF/mods.toml" dentro cartelle)
        for name in names:
            if name.lower().endswith("/" + candidate.lower()):
                return name
    return ""


def _parse_fabric(meta: JarMeta, data: dict) -> None:
    meta.loader = "fabric"
    meta.mod_id = str(data.get("id", "")).strip()
    meta.name = _strip_colors(str(data.get("name", meta.mod_id)))
    meta.version = str(data.get("version", "")).strip()
    meta.environment = str(data.get("environment", "*"))
    depends = data.get("depends") or {}
    if isinstance(depends, dict):
        meta.depends = {str(k): str(v) for k, v in depends.items()}
    recommends = data.get("recommends") or {}
    if isinstance(recommends, dict):
        meta.soft_depends = [str(k) for k in recommends]
    meta.mc_spec = meta.depends.get("minecraft", "")
    entrypoints = data.get("entrypoints") or {}
    if isinstance(entrypoints, dict) and entrypoints.get("main"):
        try:
            meta.main_class = str(entrypoints["main"][0])
        except (TypeError, IndexError, KeyError):
            pass


def _flatten_dependencies(raw) -> list[dict]:
    """mods.toml: `dependencies` e' una tabella {modId: [ {...}, {...} ]}."""
    out: list[dict] = []
    if isinstance(raw, dict):
        for value in raw.values():
            if isinstance(value, list):
                out.extend(item for item in value if isinstance(item, dict))
            elif isinstance(value, dict):
                out.append(value)
    elif isinstance(raw, list):
        out.extend(item for item in raw if isinstance(item, dict))
    return out


def _parse_modstoml(meta: JarMeta, text: str, loader: str) -> None:
    meta.loader = loader
    try:
        data = tomllib.loads(text)
    except Exception as exc:
        meta.valid = False
        meta.error = f"mods.toml non valido: {exc}"
        return
    mods = data.get("mods") or []
    if isinstance(mods, list) and mods:
        first = mods[0] if isinstance(mods[0], dict) else {}
        meta.mod_id = str(first.get("modId", "")).strip()
        meta.name = _strip_colors(str(first.get("displayName", meta.mod_id)))
        meta.version = str(first.get("version", "")).strip()
    for dependency in _flatten_dependencies(data.get("dependencies")):
        dep_id = str(dependency.get("modId", "")).strip()
        dep_range = str(dependency.get("versionRange", "*")).strip()
        if not dep_id:
            continue
        if dep_id in {"minecraft"}:
            meta.mc_spec = dep_range
        elif dep_id in {"forge", "neoforge", "fml", "fabricloader"}:
            continue
        else:
            if str(dependency.get("mandatory", "true")).lower() == "true":
                meta.depends[dep_id] = dep_range
            else:
                meta.soft_depends.append(dep_id)


def _parse_plugin_yml(meta: JarMeta, data: dict, paper: bool = False) -> None:
    meta.loader = "paper" if paper else "bukkit"
    meta.mod_id = str(data.get("name", "")).strip()
    meta.name = str(data.get("name", "")).strip()
    meta.version = str(data.get("version", "")).strip()
    meta.api_version = str(data.get("api-version", "")).strip()
    meta.main_class = str(data.get("main", "")).strip()
    meta.environment = "server"
    for key in ("depend", "dependencies"):
        value = data.get(key)
        if isinstance(value, list):
            meta.depends.update({str(item): "*" for item in value})
        elif isinstance(value, dict):  # paper-plugin.yml
            server = value.get("server") or {}
            for dep_name, dep_conf in server.items():
                if isinstance(dep_conf, dict) and not dep_conf.get("required", True):
                    meta.soft_depends.append(str(dep_name))
                else:
                    meta.depends[str(dep_name)] = "*"
    for key in ("softdepend", "soft-depend"):
        value = data.get(key)
        if isinstance(value, list):
            meta.soft_depends.extend(str(item) for item in value)


def read_jar_meta(path: Path, rel: str | None = None) -> JarMeta:
    rel = rel or path.name
    meta = JarMeta(path=path, rel=rel)
    try:
        meta.size = path.stat().st_size
    except OSError:
        meta.valid = False
        meta.error = "file non leggibile"
        return meta

    if meta.size == 0:
        meta.valid = False
        meta.error = "file vuoto (0 byte): download interrotto?"
        return meta

    try:
        with open(path, "rb") as handle:
            meta.sha1 = hashlib.sha1(handle.read()).hexdigest()
    except OSError:
        pass

    try:
        with zipfile.ZipFile(path) as zf:
            fabric = _find_name(zf, ("fabric.mod.json",))
            mods_toml = _find_name(zf, ("META-INF/neoforge.mods.toml", "META-INF/mods.toml"))
            paper_yml = _find_name(zf, ("paper-plugin.yml",))
            plugin_yml = _find_name(zf, ("plugin.yml",))

            if fabric:
                try:
                    _parse_fabric(meta, json.loads(_read_entry(zf, fabric)))
                except Exception as exc:  # json rotto
                    meta.valid = False
                    meta.error = f"fabric.mod.json non leggibile: {exc}"
            elif mods_toml:
                loader = "neoforge" if "neoforge" in mods_toml.lower() else "forge"
                _parse_modstoml(meta, _read_entry(zf, mods_toml), loader)
            elif paper_yml:
                _parse_plugin_yml(meta, _load_yaml(_read_entry(zf, paper_yml)), paper=True)
            elif plugin_yml:
                _parse_plugin_yml(meta, _load_yaml(_read_entry(zf, plugin_yml)))
            else:
                meta.loader = "unknown"
                meta.error = "nessun metadato riconosciuto (fabric.mod.json / mods.toml / plugin.yml)"
    except zipfile.BadZipFile:
        meta.valid = False
        meta.error = "jar corrotto o non valido (non e' uno zip)"
    except OSError as exc:
        meta.valid = False
        meta.error = f"errore di lettura: {exc}"

    if not meta.name:
        meta.name = path.name
    if not meta.mod_id:
        meta.mod_id = _guess_id_from_filename(path.name)
    return meta


def _guess_id_from_filename(filename: str) -> str:
    stem = re.sub(r"\.jar$", "", filename, flags=re.IGNORECASE)
    stem = re.sub(r"[-_+]\d[\w.\-+]*$", "", stem)  # toglie la versione finale
    return re.sub(r"[^a-z0-9]", "", stem.lower())


def normalize_token(value: str) -> str:
    """Normalizza un nome/id per confronti approssimativi."""
    value = _strip_colors(value or "").lower()
    value = re.sub(r"[-_+.]", " ", value)
    value = re.sub(r"\bv?\d+(\.\d+)+\b", " ", value)  # toglie le versioni
    value = re.sub(r"[^a-z0-9 ]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value.replace(" ", "")

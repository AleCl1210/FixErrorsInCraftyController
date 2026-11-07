#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Crafty Medic - script singolo da eseguire DENTRO il container di Crafty Controller.

Nessuna dipendenza: usa solo la libreria standard di Python (quindi niente pip install).
Si salva in una cartella persistente del container (es. /crafty/servers) e si lancia:

    python3 /crafty/servers/crafty-medic.py            # menu interattivo
    python3 /crafty/servers/crafty-medic.py scan       # analizza tutti i server
    python3 /crafty/servers/crafty-medic.py fix        # ripara (interattivo)
    python3 /crafty/servers/crafty-medic.py fix --auto # ripara senza chiedere
    python3 /crafty/servers/crafty-medic.py undo       # annulla l'ultima riparazione
    python3 /crafty/servers/crafty-medic.py quarantine # file in quarantena

Non cancella mai nulla: i file tolti vanno in quarantena e si ripristinano con `undo`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

VERSION = "1.0.0"
USER_AGENT = "CraftyMedic/%s (in-container repair script)" % VERSION

# --------------------------------------------------------------------------- #
#  output
# --------------------------------------------------------------------------- #
USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(text: str, code: str) -> str:
    if not USE_COLOR:
        return text
    return "\033[%sm%s\033[0m" % (code, text)


def ok(msg): print(_c("  ✓ ", "32") + msg)
def warn(msg): print(_c("  ! ", "33") + msg)
def err(msg): print(_c("  ✗ ", "31") + msg)
def info(msg): print("    " + msg)
def head(msg): print("\n" + _c(msg, "1;36"))
def rule(char="-"): print(char * 68)


# --------------------------------------------------------------------------- #
#  versioni / range
# --------------------------------------------------------------------------- #
_RANGE_RE = re.compile(r"^([\[\(])\s*([^,]*)\s*,\s*([^\]\)]*)\s*([\]\)])$")
_WILDCARD_RE = re.compile(r"^(\d+(?:\.\d+)*)\.[xX*]$")


def parse_version(value: str) -> tuple:
    if not value:
        return ()
    cleaned = re.split(r"[-+]", value.strip())[0]
    parts = []
    for chunk in cleaned.split("."):
        if chunk.isdigit():
            parts.append(int(chunk))
        else:
            break
    return tuple(parts) if parts else ()


def compare(a: str, b: str) -> int:
    left, right = parse_version(a), parse_version(b)
    if not left or not right:
        return 0
    size = max(len(left), len(right))
    left += (0,) * (size - len(left))
    right += (0,) * (size - len(right))
    return (left > right) - (left < right)


def _split_constraints(spec: str) -> list:
    chunks, depth, current = [], 0, ""
    for char in spec:
        if char in "[((":
            depth += 1
        elif char in "])":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            chunks.append(current)
            current = ""
        else:
            current += char
    chunks.append(current)
    return [c.strip() for c in chunks]


def parse_spec(spec: str) -> list:
    if not spec:
        return []
    spec = spec.strip()
    if not spec or spec in ("*", "any"):
        return []
    out = []
    for chunk in _split_constraints(spec):
        if not chunk:
            continue
        m = _RANGE_RE.match(chunk)
        if m:
            open_br, low, high, close_br = m.groups()
            if low.strip():
                out.append((">=" if open_br == "[" else ">", low.strip()))
            if high.strip():
                out.append(("<=" if close_br == "]" else "<", high.strip()))
            continue
        if chunk[0] in "[(" and chunk[-1] in "])":
            if chunk[1:-1].strip():
                out.append(("==", chunk[1:-1].strip()))
            continue
        for prefix in (">=", "<=", "==", "!=", "~", "^", ">", "<", "="):
            if chunk.startswith(prefix):
                rest = chunk[len(prefix):].strip()
                if prefix == "~":
                    digits = parse_version(rest)
                    out.append((">=", rest))
                    if digits:
                        out.append(("<", _fmt(_bump(digits))))
                elif prefix == "^":
                    out.append((">=", rest))
                    out.append(("<", str(parse_version(rest)[0] + 1) + ".0.0" if parse_version(rest) else ""))
                else:
                    out.append(("==" if prefix == "=" else prefix, rest))
                break
        else:
            w = _WILDCARD_RE.match(chunk)
            if w:
                out.append((">=", _fmt(parse_version(w.group(1)))))
                out.append(("<", _fmt(_bump(parse_version(w.group(1))))))
            else:
                out.append(("==", chunk))
    return [c for c in out if c[1]]


def _bump(digits: tuple) -> tuple:
    if not digits:
        return ()
    d = list(digits[:2]) + [0] * (2 - len(digits[:2]))
    d[-1] += 1
    while len(d) < 3:
        d.append(0)
    return tuple(d)


def _fmt(digits: tuple) -> str:
    return ".".join(str(x) for x in digits)


def satisfies(version: str, spec: str):
    """True/False, None se non valutabile."""
    if not version:
        return None
    constraints = parse_spec(spec or "")
    if not constraints:
        return True
    for op, expected in constraints:
        diff = compare(version, expected)
        if op == ">=" and not diff >= 0:
            return False
        if op == ">" and not diff > 0:
            return False
        if op == "<=" and not diff <= 0:
            return False
        if op == "<" and not diff < 0:
            return False
        if op == "==" and diff != 0:
            return False
        if op == "!=" and diff == 0:
            return False
    return True


# --------------------------------------------------------------------------- #
#  metadati dei jar
# --------------------------------------------------------------------------- #
class Jar:
    def __init__(self, path: Path, rel: str):
        self.path = path
        self.rel = rel
        self.name = path.name
        self.size = path.stat().st_size if path.exists() else 0
        self.loader = "unknown"
        self.mod_id = ""
        self.label = ""
        self.version = ""
        self.mc_spec = ""
        self.depends = {}
        self.soft = []
        self.environment = ""
        self.api_version = ""
        self.main = ""
        self.valid = True
        self.error = ""

    @property
    def kind(self) -> str:
        if self.loader in ("fabric", "forge", "neoforge", "quilt"):
            return "mod"
        if self.loader in ("bukkit", "paper"):
            return "plugin"
        return "sconosciuto"

    def as_dict(self):
        return dict(rel=self.rel, loader=self.loader, label=self.label, version=self.version,
                    mc_spec=self.mc_spec, api_version=self.api_version, valid=self.valid)


def _mini_yaml(text: str) -> dict:
    root, stack = {}, [(-1, {})]
    stack = [(-1, root)]
    last_list = None
    for raw in text.splitlines():
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        if "\t" in raw[:8]:
            raw = raw.replace("\t", "    ")
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if line.startswith("- ") or line == "-":
            if last_list:
                parent, key = last_list
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
        key, value = key.strip(), value.strip()
        if value:
            parent[key] = _scalar(value)
            last_list = None
        else:
            child = {}
            parent[key] = child
            stack.append((indent, child))
            last_list = (parent, key)
    return root


def _scalar(value: str):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        return [i.strip().strip("\"'") for i in value[1:-1].split(",") if i.strip()]
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    return value


def _strip_colors(text: str) -> str:
    return re.sub(r"§[0-9a-fk-orA-FK-OR]", "", text or "")


def read_jar(path: Path, rel: str) -> Jar:
    jar = Jar(path, rel)
    if jar.size == 0:
        jar.valid = False
        jar.error = "file vuoto (0 byte)"
        return jar
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            lower = {n.lower(): n for n in names}

            def find(*candidates):
                for c in candidates:
                    if c in names:
                        return c
                    if c.lower() in lower:
                        return lower[c.lower()]
                    for n in names:
                        if n.lower().endswith("/" + c.lower()):
                            return n
                return ""

            def read(name):
                with zf.open(name) as fh:
                    return fh.read(512 * 1024).decode("utf-8", errors="replace")

            fabric = find("fabric.mod.json")
            mods_toml = find("META-INF/neoforge.mods.toml", "META-INF/mods.toml")
            paper_yml = find("paper-plugin.yml")
            plugin_yml = find("plugin.yml")

            if fabric:
                try:
                    data = json.loads(read(fabric))
                except Exception as exc:
                    jar.valid = False
                    jar.error = "fabric.mod.json illeggibile: %s" % exc
                    return jar
                jar.loader = "fabric"
                jar.mod_id = str(data.get("id", "")).strip()
                jar.label = _strip_colors(str(data.get("name", jar.mod_id)))
                jar.version = str(data.get("version", "")).strip()
                jar.environment = str(data.get("environment", "*"))
                deps = data.get("depends") or {}
                if isinstance(deps, dict):
                    jar.depends = {str(k): str(v) for k, v in deps.items()}
                jar.mc_spec = jar.depends.get("minecraft", "")
            elif mods_toml:
                jar.loader = "neoforge" if "neoforge" in mods_toml.lower() else "forge"
                _parse_toml(jar, read(mods_toml))
            elif paper_yml or plugin_yml:
                name = paper_yml or plugin_yml
                jar.loader = "paper" if paper_yml else "bukkit"
                _parse_plugin(jar, _mini_yaml(read(name)))
            else:
                jar.error = "nessun metadato riconosciuto"
    except zipfile.BadZipFile:
        jar.valid = False
        jar.error = "jar corrotto (non e' uno zip valido)"
    except OSError as exc:
        jar.valid = False
        jar.error = "errore di lettura: %s" % exc
    if not jar.label:
        jar.label = path.name
    if not jar.mod_id:
        stem = re.sub(r"\.jar$", "", path.name, flags=re.I)
        stem = re.sub(r"[-_+]\d[\w.\-+]*$", "", stem)
        jar.mod_id = re.sub(r"[^a-z0-9]", "", stem.lower())
    return jar


def _parse_toml(jar: Jar, text: str) -> None:
    """Parser minimale per mods.toml (solo quello che serve)."""
    current_table = ""
    dep_mod, dep_range, dep_mandatory, dep_id = "", "*", True, ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[["):
            current_table = line.strip("[]")
            dep_mod, dep_range, dep_mandatory, dep_id = "", "*", True, ""
            if current_table.startswith("dependencies."):
                dep_mod = current_table.split(".", 1)[1]
            continue
        if line.startswith("["):
            current_table = line.strip("[]")
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"')
        if current_table == "mods":
            if key == "modId":
                jar.mod_id = value
            elif key == "displayName":
                jar.label = _strip_colors(value)
            elif key == "version":
                jar.version = value
        elif current_table.startswith("dependencies."):
            if key == "modId":
                dep_id = value
            elif key == "versionRange":
                dep_range = value
            elif key == "mandatory":
                dep_mandatory = value.lower() != "false"
            elif key == "side" or line.endswith("BOTH") or line.endswith("SERVER"):
                if dep_id:
                    if dep_id == "minecraft":
                        jar.mc_spec = dep_range
                    elif dep_id in ("forge", "neoforge", "fml"):
                        pass
                    elif dep_mandatory:
                        jar.depends[dep_id] = dep_range
                    else:
                        jar.soft.append(dep_id)
                    dep_id, dep_range, dep_mandatory = "", "*", True
    # se l'ultima dipendenza non era chiusa da "side"
    if dep_id and dep_id not in ("forge", "neoforge", "fml"):
        if dep_id == "minecraft":
            jar.mc_spec = dep_range
        elif dep_mandatory:
            jar.depends[dep_id] = dep_range


def _parse_plugin(jar: Jar, data: dict) -> None:
    jar.mod_id = str(data.get("name", "")).strip()
    jar.label = str(data.get("name", "")).strip() or jar.name
    jar.version = str(data.get("version", "")).strip()
    jar.api_version = str(data.get("api-version", "")).strip()
    jar.main = str(data.get("main", "")).strip()
    jar.environment = "server"
    for key in ("depend", "dependencies"):
        value = data.get(key)
        if isinstance(value, list):
            jar.depends.update({str(i): "*" for i in value})
        elif isinstance(value, dict):
            server = value.get("server") or {}
            for dep_name, conf in server.items():
                if isinstance(conf, dict) and not conf.get("required", True):
                    jar.soft.append(str(dep_name))
                else:
                    jar.depends[str(dep_name)] = "*"
    for key in ("softdepend", "soft-depend"):
        value = data.get(key)
        if isinstance(value, list):
            jar.soft.extend(str(i) for i in value)


# --------------------------------------------------------------------------- #
#  regole sui log
# --------------------------------------------------------------------------- #
MOD_SUBJECT = re.compile(r"Mod '(?P<name>.+?)' \((?P<id>[^)]+)\)")
PLUGIN_FILE = re.compile(r"'(?P<file>plugins/[^']+\.jar)'")
ENABLING = re.compile(r"enabling (?P<name>[^\s]+) v(?P<ver>[^\s(]+)", re.I)


def _lookback(lines, idx, pattern, window=8):
    for i in range(idx, max(0, idx - window) - 1, -1):
        m = pattern.search(lines[i])
        if m:
            return m
    return None


def ex_none(m, lines, idx): return []
def ex_mod(m, lines, idx):
    g = m.groupdict()
    return [t for t in (g.get("id"), g.get("subj_id"), g.get("name"), g.get("subj_name")) if t]
def ex_mod_dep(m, lines, idx):
    g = m.groupdict()
    out = [t for t in (g.get("subj_id"), g.get("subj_name"), g.get("id")) if t]
    dep = (g.get("dep") or "").strip().strip("'\"")
    if dep and dep.lower() != "minecraft":
        out.append(dep)
    return out
def ex_plugin_file(m, lines, idx):
    hit = _lookback(lines, idx, PLUGIN_FILE)
    if hit:
        return [hit.group("file")]
    hit = _lookback(lines, idx, ENABLING)
    if hit:
        return [hit.group("name")]
    return []
def ex_plugin_name(m, lines, idx):
    g = m.groupdict()
    return [t for t in (g.get("name"), g.get("file")) if t]
def ex_forge_dep(m, lines, idx):
    g = m.groupdict()
    return [t for t in (g.get("by"),) if t]
def ex_generic_mods(m, lines, idx):
    out, seen = [], set()
    for i in range(idx, min(len(lines), idx + 15)):
        for hit in MOD_SUBJECT.finditer(lines[i]):
            for token in (hit.group("id"), hit.group("name")):
                if token and token.lower() not in seen:
                    seen.add(token.lower())
                    out.append(token)
        provided = re.search(r"provided by '(?P<id>[^']+)'", lines[i])
        if provided and provided.group("id") not in seen:
            seen.add(provided.group("id").lower())
            out.append(provided.group("id"))
    return out[:6]


RULES = [
    # ---- Fabric ----
    dict(id="fabric_mc_version", title="Mod per un'altra versione di Minecraft", cat="mod", sev="critico",
         remedy="update", conf=95, ctx=6, extract=ex_mod,
         pattern=r"Mod '(?P<subj_name>.+?)' \((?P<subj_id>[^)]+)\)[^!]*?requires (?:version (?P<spec>[^\s]+) of |any version of )minecraft, which is missing",
         advice="Questo mod non supporta la versione di Minecraft del server: aggiornalo o toglilo."),
    dict(id="fabric_missing_dep", title="Dipendenza mancante", cat="mod", sev="critico", remedy="quarantine",
         conf=90, ctx=6, extract=ex_mod_dep,
         pattern=r"Mod '(?P<subj_name>.+?)' \((?P<subj_id>[^)]+)\)[^!]*?requires (?:version (?P<spec>[^\s]+) of |any version of )(?:mod )?(?P<dep>'[^']+'|[^\s,!]+), which is missing",
         advice="Il mod richiede un altro mod che non e' installato: toglilo o installa la dipendenza."),
    dict(id="fabric_incompatible", title="Due mod incompatibili", cat="mod", sev="critico", remedy="quarantine",
         conf=85, ctx=6, extract=ex_mod_dep,
         pattern=r"Mod '(?P<subj_name>.+?)' \((?P<subj_id>[^)]+)\)[^!]*?is incompatible with (?:mod )?'(?P<bname>.+?)' \((?P<bid>[^)]+)\)",
         advice="Questi due mod non possono convivere: togline uno."),
    dict(id="fabric_duplicate", title="Mod duplicato", cat="mod", sev="alto", remedy="quarantine", conf=85,
         ctx=6, extract=ex_mod,
         pattern=r"(?:duplicate (?:mod|mod ?id|modid)|non-unique mod ?id)[^'\"]*['\"]?(?P<id>[\w.\-]+)['\"]?",
         advice="Due copie dello stesso mod: tienine una sola."),
    dict(id="fabric_entrypoint", title="Errore all'avvio di un mod", cat="mod", sev="critico", remedy="quarantine",
         conf=80, ctx=6, extract=ex_mod,
         pattern=r"Could not execute entrypoint stage '(?P<stage>\w+)' due to errors, provided by '(?P<id>[^']+)'",
         advice="Il mod si rompe durante l'inizializzazione: aggiornalo o toglilo."),
    dict(id="fabric_client_mod", title="Mod solo client", cat="mod", sev="alto", remedy="quarantine", conf=70,
         ctx=6, extract=ex_mod,
         pattern=r"Mod '(?P<subj_name>.+?)' \((?P<subj_id>[^)]+)\)[^!]*?(?:is a client(?:-|\s)?(?:side|only) mod|requires environment CLIENT|environment CLIENT)",
         advice="Questo mod gira solo sul client: va tolto dal server."),
    dict(id="fabric_generic", title="Fabric non riesce a caricare i mod", cat="mod", sev="alto", remedy="manual",
         conf=45, ctx=15, extract=ex_generic_mods, pattern=r"ModResolutionException",
         advice="Controlla le righe qui sopra: Fabric elenca i mod coinvolti."),
    # ---- Forge / NeoForge ----
    dict(id="forge_missing_dep", title="Dipendenza obbligatoria mancante", cat="mod", sev="critico",
         remedy="quarantine", conf=92, ctx=6, extract=ex_forge_dep,
         pattern=r"Mod ID: '(?P<dep>[^']+)', Requested by: '(?P<by>[^']+)'",
         advice="Il mod 'Requested by' ha bisogno di un mod non installato."),
    dict(id="forge_missing_mods", title="Mod richiesto non presente", cat="mod", sev="critico", remedy="quarantine",
         conf=88, ctx=6, extract=ex_mod_dep,
         pattern=r"Mod (?P<name>[^\s]+) \((?P<id>[^)]+)\) requires mod (?P<bname>[^\s]+) \((?P<bid>[^)]+)\)",
         advice="Manca un mod richiesto da quello indicato."),
    dict(id="forge_mc_version", title="Mod per un'altra versione di Minecraft", cat="mod", sev="critico",
         remedy="update", conf=90, ctx=6, extract=ex_mod,
         pattern=r"(?:Mod )?(?P<name>[^\s]+) \((?P<id>[^)]+)\) requires minecraft (?:version )?(?P<spec>[^\s]+)",
         advice="Il mod non e' compatibile con questa versione di Minecraft."),
    dict(id="forge_load_error", title="Errore nel caricamento di un mod", cat="mod", sev="critico",
         remedy="quarantine", conf=75, ctx=6, extract=ex_mod,
         pattern=r"Mod (?P<name>.+?) \((?P<id>[^)]+)\) encountered an error during the (?P<phase>[\w ]+?) (?:event )?phase",
         advice="Il mod si rompe durante il caricamento: aggiornalo o toglilo."),
    dict(id="forge_no_modstoml", title="Jar non valido come mod", cat="mod", sev="alto", remedy="move", conf=80,
         ctx=6, extract=ex_plugin_name,
         pattern=r"(?:File|Mod File) (?P<file>[^\s]+\.jar) is missing mods\.toml",
         advice="Non e' un mod Forge: forse e' un plugin, o un download corrotto."),
    dict(id="forge_mixin_fail", title="Mixin fallito", cat="mod", sev="critico", remedy="quarantine", conf=75,
         ctx=6, extract=ex_mod, pattern=r"Mixin apply for mod (?P<id>[\w.\-]+) failed",
         advice="Sintomo classico di mod non aggiornato."),
    # ---- Bukkit / Paper / Spigot ----
    dict(id="plugin_unsupported_api", title="Plugin troppo vecchio (API non supportata)", cat="plugin",
         sev="critico", remedy="update", conf=95, ctx=8, extract=ex_plugin_file,
         pattern=r"Unsupported API version (?P<api>[\d.]+)",
         advice="Il plugin dichiara un'API piu' vecchia di quella del server: aggiornalo o toglilo."),
    dict(id="plugin_unknown_dep", title="Plugin: dipendenza mancante", cat="plugin", sev="critico",
         remedy="quarantine", conf=92, ctx=8, extract=ex_plugin_file,
         pattern=r"Unknown(?:-| |/missing )?[Dd]ependency[^\n]*?\[?(?P<deps>[^\]\n]+)\]?",
         advice="Il plugin richiede un altro plugin non installato (es. Vault, ProtocolLib)."),
    dict(id="plugin_invalid_yml", title="plugin.yml non valido", cat="plugin", sev="critico", remedy="quarantine",
         conf=90, ctx=8, extract=ex_plugin_file,
         pattern=r"(?:InvalidDescriptionException|Invalid plugin\.yml|plugin\.yml (?:is )?(?:invalid|missing|not found)|Could not find (?:required )?'?main'? (?:class|attribute))",
         advice="Il plugin.yml dentro il jar manca o e' sbagliato: riscarica il plugin."),
    dict(id="plugin_enable_error", title="Errore durante l'attivazione", cat="plugin", sev="alto", remedy="update",
         conf=70, ctx=8, extract=ex_plugin_name,
         pattern=r"Error occurred (?:while|when) enabling (?P<name>[^\s]+) v(?P<ver>[^\s(]+)",
         advice="Il plugin si attiva ma genera un errore: aggiornalo o toglilo."),
    dict(id="plugin_noclassdef", title="Classi mancanti (non compatibile)", cat="plugin", sev="alto",
         remedy="update", conf=60, ctx=8, extract=ex_plugin_file,
         pattern=r"java\.lang\.(?:NoClassDefFoundError|NoSuchMethodError|NoSuchFieldError): (?P<cls>[^\s]+)",
         advice="Il plugin cerca qualcosa che non esiste in questa versione del server."),
    dict(id="plugin_duplicate", title="Plugin duplicato", cat="plugin", sev="alto", remedy="quarantine", conf=85,
         ctx=8, extract=ex_plugin_name,
         pattern=r"Ambiguous plugin name '(?P<name>[^']+)' for files '(?P<a>[^']+)' and '(?P<b>[^']+)'",
         advice="Due jar dichiarano lo stesso plugin: tienine uno."),
    dict(id="plugin_load_failed", title="Impossibile caricare un plugin", cat="plugin", sev="critico",
         remedy="quarantine", conf=55, ctx=8, extract=ex_plugin_name,
         pattern=r"Could not load '(?P<file>plugins/[^']+\.jar)' in folder '(?P<folder>[^']+)'",
         advice="La causa e' nelle righe successive."),
    # ---- server / java / config ----
    dict(id="java_class_version", title="Versione di Java sbagliata", cat="java", sev="critico", remedy="config",
         conf=85, ctx=6, extract=ex_none,
         pattern=r"(?:UnsupportedClassVersionError|class file has wrong version|Unsupported class file (?:major )?version \d+|has been compiled by a more recent version of the Java Runtime)",
         advice="Cambia la versione di Java nelle impostazioni del server in Crafty (Java 17 fino a MC 1.20.4, Java 21 da MC 1.20.5)."),
    dict(id="java_too_old", title="Java troppo vecchio", cat="java", sev="critico", remedy="config", conf=80,
         ctx=6, extract=ex_none,
         pattern=r"(?:requires?|need(?:s)?|minimum|update to|upgrade to) Java ?\d+|(?:unsupported|outdated|invalid) Java version",
         advice="Aggiorna Java nelle impostazioni del server in Crafty."),
    dict(id="eula", title="EULA non accettata", cat="config", sev="critico", remedy="config", conf=99, ctx=4,
         extract=ex_none, pattern=r"You need to agree to the EULA",
         advice="Metti eula=true nel file eula.txt del server."),
    dict(id="port_in_use", title="Porta gia' occupata", cat="config", sev="critico", remedy="config", conf=95,
         ctx=4, extract=ex_none, pattern=r"(?:FAILED TO BIND TO PORT|Failed to bind to port|Address already in use)",
         advice="Cambia la porta del server o ferma chi la occupa."),
    dict(id="missing_jar", title="Jar del server non trovato", cat="config", sev="critico", remedy="config",
         conf=95, ctx=4, extract=ex_none, pattern=r"Unable to access jarfile (?P<file>.+)",
         advice="Controlla il comando di avvio del server in Crafty."),
    dict(id="out_of_memory", title="Memoria esaurita", cat="config", sev="alto", remedy="config", conf=90, ctx=4,
         extract=ex_none, pattern=r"java\.lang\.OutOfMemoryError|There is insufficient memory",
         advice="Aumenta la RAM nelle impostazioni del server."),
    dict(id="world_corrupt", title="Mondo danneggiato", cat="mondo", sev="critico", remedy="manual", conf=70,
         ctx=4, extract=ex_none,
         pattern=r"(?:Failed to load world|Exception (?:occurred )?(?:while |in )?loading the world|world .* is corrupt)",
         advice="Ripristina un backup del mondo da Crafty."),
    dict(id="startup_failed", title="Avvio fallito", cat="generale", sev="medio", remedy="manual", conf=30, ctx=10,
         extract=ex_generic_mods, pattern=r"Failed to start the minecraft server|Encountered an unexpected exception",
         advice="Scorri il log per la causa esatta."),
]

for _r in RULES:
    _r["compiled"] = re.compile(_r["pattern"], re.IGNORECASE)

MC_PATTERNS = [
    re.compile(r"Starting minecraft server version (?P<v>[\d.]+)"),
    re.compile(r"Loading Minecraft (?P<v>[\d.]+) with Fabric Loader"),
    re.compile(r"Forge mod loading, version [\d.\-]+, for MC (?P<v>[\d.]+)"),
    re.compile(r"Starting Minecraft server on (?P<v>[\d.]+)"),
    re.compile(r"\(MC: (?P<v>[\d.]+)\)"),
]
LOADER_PATTERNS = [
    (re.compile(r"neoforge", re.I), "neoforge"),
    (re.compile(r"Forge mod loading|MinecraftForge|fml\.", re.I), "forge"),
    (re.compile(r"Fabric Loader|fabric-loader|net\.fabricmc", re.I), "fabric"),
    (re.compile(r"This server is running (?:Paper|Purpur) version", re.I), "paper"),
    (re.compile(r"This server is running CraftBukkit|Spigot", re.I), "bukkit"),
]

GENERIC_RULES = {"fabric_generic", "startup_failed", "plugin_load_failed"}
SEVERITY_RANK = {"critico": 0, "alto": 1, "medio": 2, "basso": 3}
# a parita' di affidabilita' preferiamo l'azione piu' utile (spostare > aggiornare > togliere)
REMEDY_ORDER = {"move": 0, "update": 1, "quarantine": 2, "config": 3, "manual": 4}


# --------------------------------------------------------------------------- #
#  modello
# --------------------------------------------------------------------------- #
class Finding:
    def __init__(self, rule, evidence, line_no, targets, unmatched, source, note=""):
        self.rule_id = rule["id"]
        self.title = rule["title"]
        self.category = rule["cat"]
        self.severity = rule["sev"]
        self.remedy = rule["remedy"]
        self.advice = rule["advice"]
        self.confidence = rule["conf"]
        self.evidence = evidence
        self.line_no = line_no
        self.targets = targets           # lista di Jar
        self.unmatched = unmatched
        self.source = source
        self.note = note

    @property
    def target_names(self):
        return ", ".join(j.rel for j in self.targets) or "-"


class Server:
    def __init__(self, path: Path, name: str):
        self.path = path
        self.name = name
        self.mc_version = ""
        self.loader = ""
        self.log_text = ""
        self.log_path = ""
        self.jars = []
        self.running = False

    @property
    def id(self):
        return self.path.name


# --------------------------------------------------------------------------- #
#  scoperta server e log
# --------------------------------------------------------------------------- #
DEFAULT_SERVERS_DIRS = ["/crafty/servers", "/crafty/app/servers", "/minecraft/servers", "/server", "/data/servers"]


def find_servers_dir(explicit: str = "") -> Path:
    candidates = [explicit] if explicit else []
    candidates += [os.environ.get("CRAFTY_SERVERS_DIR", "")] + DEFAULT_SERVERS_DIRS
    for candidate in candidates:
        if candidate and Path(candidate).is_dir():
            return Path(candidate)
    return Path("/crafty/servers")


def crafty_server_names(db_dirs=("/crafty/app/config/db", "/crafty/db", "/crafty/config/db")) -> dict:
    """Prova a leggere i nomi dei server dal database di Crafty (solo lettura)."""
    names = {}
    for folder in db_dirs:
        folder = Path(folder)
        if not folder.is_dir():
            continue
        for db_file in sorted(folder.glob("*.db")):
            try:
                conn = sqlite3.connect("file:%s?mode=ro" % db_file, uri=True)
                cur = conn.cursor()
                cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = [row[0] for row in cur.fetchall()]
                for table in tables:
                    cur.execute('PRAGMA table_info("%s")' % table)
                    columns = [row[1] for row in cur.fetchall()]
                    if "server_id" not in columns:
                        continue
                    name_col = next((c for c in columns if "name" in c.lower()), None)
                    if not name_col:
                        continue
                    cur.execute('SELECT server_id, "%s" FROM "%s"' % (name_col, table))
                    for server_id, server_name in cur.fetchall():
                        if server_id and server_name:
                            names[str(server_id)] = str(server_name)
                conn.close()
            except sqlite3.Error:
                continue
    return names


def discover_servers(servers_dir: Path) -> list:
    names = crafty_server_names()
    servers = []
    if not servers_dir.is_dir():
        return servers
    for child in sorted(servers_dir.iterdir()):
        if not child.is_dir() or child.name.startswith(("_", ".")):
            continue
        if not list(child.glob("*")):
            continue
        servers.append(Server(child, names.get(child.name, child.name)))
    return servers


def read_log(server: Server) -> None:
    for relative in ("logs/latest.log", "logs/latest.log.gz", "logs/server.log", "latest.log"):
        path = server.path / relative
        if not path.is_file():
            continue
        try:
            if path.suffix == ".gz":
                import gzip
                server.log_text = gzip.open(path, "rt", errors="replace").read()
            else:
                data = path.read_bytes()
                if len(data) > 4_000_000:
                    data = data[-4_000_000:]
                server.log_text = data.decode("utf-8", errors="replace")
            server.log_path = str(path)
            return
        except OSError:
            continue


def scan_jars(server: Server) -> list:
    jars = []
    for folder in ("mods", "plugins"):
        base = server.path / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.name.lower().endswith((".jar", ".jar.disabled")):
                rel = path.relative_to(server.path).as_posix()
                jars.append(read_jar(path, rel) if path.suffix.lower() == ".jar" else _plain_jar(path, rel))
    return jars


def _plain_jar(path: Path, rel: str) -> Jar:
    jar = Jar(path, rel)
    jar.error = "estensione .jar.disabled: non viene caricato"
    return jar


def detect_mc_version(text: str) -> str:
    for pattern in MC_PATTERNS:
        m = pattern.search(text or "")
        if m:
            return m.group("v")
    return ""


def detect_loader(text: str) -> str:
    for pattern, loader in LOADER_PATTERNS:
        if pattern.search(text or ""):
            return loader
    return ""


def guess_loader_from_files(server: Server) -> str:
    counts = {}
    for jar in server.jars:
        if jar.loader != "unknown":
            counts[jar.loader] = counts.get(jar.loader, 0) + 1
    if counts:
        return max(counts.items(), key=lambda kv: kv[1])[0]
    for name in os.listdir(server.path):
        low = name.lower()
        if "fabric" in low:
            return "fabric"
        if "forge" in low or "neoforge" in low:
            return "forge"
        if "paper" in low:
            return "paper"
    return ""


def is_process_running(server: Server) -> bool:
    """Controlla /proc per capire se c'e' un java che sta usando questa cartella."""
    needle = str(server.path)
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % entry, "rb") as fh:
                    cmdline = fh.read().decode("utf-8", errors="replace")
            except OSError:
                continue
            if needle in cmdline and ("java" in cmdline or "bedrock" in cmdline):
                return True
    except OSError:
        return False
    return False


# --------------------------------------------------------------------------- #
#  analisi
# --------------------------------------------------------------------------- #
def normalize(value: str) -> str:
    value = _strip_colors(value or "").lower()
    value = re.sub(r"[-_+.]", " ", value)
    value = re.sub(r"\bv?\d+(\.\d+)+\b", " ", value)
    value = re.sub(r"[^a-z0-9 ]", " ", value)
    return re.sub(r"\s+", " ", value).strip().replace(" ", "")


def resolve_token(token: str, jars: list) -> list:
    if not token or not jars:
        return []
    needle = normalize(token)
    raw = _strip_colors(token).strip().strip("\"'").lower()
    if not needle and not raw:
        return []
    scored = []
    for jar in jars:
        candidates_norm = [normalize(jar.name), normalize(jar.label), normalize(jar.mod_id)]
        candidates_raw = [jar.name.lower(), (jar.label or "").lower(), (jar.mod_id or "").lower()]
        if "/" in token:
            candidates_norm.append(normalize(jar.rel))
            candidates_raw.append(jar.rel.lower())
        score = 0
        for value in candidates_norm:
            if needle and value == needle:
                score = max(score, 100)
        for value in candidates_raw:
            if raw and value == raw:
                score = max(score, 100)
        if score < 100:
            for value in candidates_norm:
                if needle and value and len(value) >= 4 and (value.startswith(needle) or needle.startswith(value)):
                    score = max(score, 85)
            for value in candidates_raw:
                if raw and value and len(value) >= 4 and (value.startswith(raw) or raw.startswith(value)):
                    score = max(score, 85)
        if score < 85:
            for value in candidates_norm:
                if needle and value and min(len(value), len(needle)) >= 5 and (needle in value or value in needle):
                    score = max(score, 65)
        if score:
            scored.append((score, jar))
    scored.sort(key=lambda item: -item[0])
    if not scored:
        return []
    best = scored[0][0]
    return [jar for score, jar in scored if score == best][:2]


BUILTIN_DEPS = {"minecraft", "forge", "neoforge", "fml", "fabricloader", "fabric", "java", "jarjar"}


def analyze(server: Server) -> list:
    findings = []
    lines = [_strip_colors(line) for line in (server.log_text or "").splitlines()]

    # --- dal log ---
    for rule in RULES:
        for idx, line in enumerate(lines):
            m = rule["compiled"].search(line)
            if not m:
                continue
            groups = m.groupdict()
            if rule["id"] == "fabric_missing_dep" and (groups.get("dep") or "").strip().lower() in ("minecraft",):
                continue
            tokens = [t for t in rule["extract"](m, lines, idx) if t]
            targets, unmatched, seen = [], [], set()
            for token in tokens:
                resolved = resolve_token(token, server.jars)
                if not resolved:
                    unmatched.append(token)
                    continue
                for jar in resolved:
                    if jar.rel in seen:
                        continue
                    seen.add(jar.rel)
                    targets.append(jar)
            start = max(0, idx - rule["ctx"])
            end = min(len(lines), idx + rule["ctx"] + 1)
            finding = Finding(rule, lines[start:end], idx + 1, targets, unmatched, "log")
            if not targets and tokens:
                finding.confidence = max(20, rule["conf"] - 25)
            if rule["id"] == "forge_missing_dep" and any(u.lower() == "minecraft" for u in unmatched):
                finding.title = "Mod per un'altra versione di Minecraft"
                finding.remedy = "update"
            findings.append(finding)

    # --- dalla scansione dei file ---
    findings += scan_files(server)

    # --- pulizia ---
    specific = [f for f in findings if f.rule_id not in GENERIC_RULES and f.severity in ("critico", "alto")]
    findings = [f for f in findings if not (f.rule_id in GENERIC_RULES and specific)]

    order = sorted(findings, key=lambda f: (-f.confidence,
                                            REMEDY_ORDER.get(f.remedy, 9),
                                            SEVERITY_RANK.get(f.severity, 9)))
    claimed, result = set(), []
    for finding in order:
        if not finding.targets:
            key = ("", finding.rule_id)
            if key in claimed:
                continue
            claimed.add(key)
            result.append(finding)
            continue
        free = [j for j in finding.targets if j.rel not in claimed]
        if not free:
            continue
        finding.targets = free
        for jar in free:
            claimed.add(jar.rel)
        result.append(finding)
    return sorted(result, key=lambda f: (SEVERITY_RANK.get(f.severity, 9), -f.confidence))


def scan_files(server: Server) -> list:
    findings = []
    jars = [j for j in server.jars if j.valid is not False]
    by_id = {}
    provided = set()
    for jar in server.jars:
        if jar.mod_id:
            by_id.setdefault(jar.mod_id.lower(), []).append(jar)
            provided.add(jar.mod_id.lower())
        if jar.label:
            provided.add(normalize(jar.label))

    for jar in server.jars:
        if not jar.valid:
            findings.append(Finding(
                dict(id="scan_corrupt", title="Jar corrotto o non valido", cat="mod", sev="alto",
                     remedy="quarantine", conf=95, advice="Il file non e' un jar valido (%s): riscaricalo o toglilo." % jar.error),
                [jar.rel, jar.error], 0, [jar], [], "scan"))
            continue
        if jar.loader == "unknown":
            continue

        folder = jar.rel.split("/")[0]
        # cartella sbagliata
        if folder == "mods" and jar.loader in ("bukkit", "paper"):
            findings.append(Finding(
                dict(id="scan_plugin_in_mods", title="Plugin nella cartella dei mod", cat="plugin", sev="alto",
                     remedy="move", conf=90, advice="E' un plugin Bukkit/Paper: va in plugins/, non in mods/."),
                [jar.rel + " -> " + jar.loader], 0, [jar], [], "scan", note="plugins"))
            continue
        if folder == "plugins" and jar.loader in ("fabric", "forge", "neoforge"):
            findings.append(Finding(
                dict(id="scan_mod_in_plugins", title="Mod nella cartella dei plugin", cat="mod", sev="alto",
                     remedy="move", conf=90, advice="E' un mod Forge/Fabric: va in mods/, non in plugins/."),
                [jar.rel + " -> " + jar.loader], 0, [jar], [], "scan", note="mods"))
            continue

        # loader diverso
        compatible = {("forge", "neoforge"), ("neoforge", "forge"), ("bukkit", "paper"), ("paper", "bukkit")}
        if server.loader and jar.loader != server.loader and (jar.loader, server.loader) not in compatible:
            if not (folder == "plugins" and server.loader in ("fabric", "forge", "neoforge")):
                findings.append(Finding(
                    dict(id="scan_loader_mismatch", title="Jar per un loader diverso", cat=jar.kind, sev="alto",
                         remedy="quarantine", conf=70,
                         advice="'%s' e' per %s ma il server usa %s." % (jar.label, jar.loader, server.loader)),
                    [jar.rel + " -> " + jar.loader], 0, [jar], [], "scan"))
                continue

        # versione di Minecraft
        if jar.mc_spec and server.mc_version and jar.loader not in ("bukkit", "paper"):
            if satisfies(server.mc_version, jar.mc_spec) is False:
                findings.append(Finding(
                    dict(id="scan_mc_mismatch", title="Versione di Minecraft non compatibile", cat=jar.kind,
                         sev="critico", remedy="update", conf=90,
                         advice="'%s' vuole Minecraft %s ma il server e' %s: aggiornalo o toglilo."
                                % (jar.label, jar.mc_spec, server.mc_version)),
                    ["%s -> %s %s | minecraft %s" % (jar.rel, jar.label, jar.version, jar.mc_spec)], 0, [jar], [],
                    "scan"))
                continue

        # api-version piu' nuova
        if jar.api_version and server.mc_version and jar.loader in ("bukkit", "paper"):
            if parse_version(jar.api_version) > parse_version(server.mc_version):
                findings.append(Finding(
                    dict(id="scan_api_newer", title="Plugin piu' nuovo del server", cat="plugin", sev="medio",
                         remedy="update", conf=65,
                         advice="Il plugin dichiara api-version %s ma il server e' %s." % (jar.api_version, server.mc_version)),
                    ["%s -> api-version %s" % (jar.rel, jar.api_version)], 0, [jar], [], "scan"))
                continue

        # mod solo client
        if jar.environment == "client" and server.loader in ("fabric", "forge", "neoforge"):
            findings.append(Finding(
                dict(id="scan_client_only", title="Mod solo client", cat="mod", sev="alto", remedy="quarantine",
                     conf=80, advice="'%s' gira solo sul client: va tolto dal server." % jar.label),
                [jar.rel + " -> environment: client"], 0, [jar], [], "scan"))
            continue

        # dipendenze mancanti
        missing = []
        for dep in jar.depends:
            key = dep.lower()
            if key in BUILTIN_DEPS:
                continue
            if key in provided or normalize(dep) in provided:
                continue
            missing.append(dep)
        if missing:
            findings.append(Finding(
                dict(id="scan_missing_dep", title="Dipendenza non installata", cat="mod", sev="critico",
                     remedy="quarantine", conf=80,
                     advice="'%s' richiede %s che non risulta installato: togli il mod o installa la dipendenza."
                            % (jar.label, ", ".join(missing))),
                ["%s -> dipende da: %s" % (jar.rel, ", ".join(missing))], 0, [jar], [], "scan"))
            continue

        # jar in sottocartella
        parts = jar.rel.split("/")
        if len(parts) > 2 and parts[0] in ("mods", "plugins"):
            findings.append(Finding(
                dict(id="scan_nested", title="Jar in una sottocartella", cat="mod" if parts[0] == "mods" else "plugin",
                     sev="medio", remedy="move", conf=85,
                     advice="Minecraft carica solo i jar direttamente in %s/, non nelle sottocartelle." % parts[0]),
                [jar.rel], 0, [jar], [], "scan", note=parts[0]))
            continue

    # duplicati
    for mod_id, items in by_id.items():
        if len(items) > 1:
            keep = max(items, key=lambda j: (j.size, j.rel))
            others = [j for j in items if j is not keep]
            findings.append(Finding(
                dict(id="scan_duplicate", title="Stesso mod installato due volte", cat="mod", sev="alto",
                     remedy="quarantine", conf=85,
                     advice="Ci sono %d copie di '%s': ne basta una." % (len(items), items[0].label)),
                [j.rel for j in items], 0, others, [], "scan"))
    return findings


# --------------------------------------------------------------------------- #
#  quarantena / backup
# --------------------------------------------------------------------------- #
class MedicHome:
    def __init__(self, path: Path):
        self.path = path
        self.quarantine = path / "quarantine"
        self.backups = path / "backups"
        self.log = path / "storico.txt"
        for folder in (self.path, self.quarantine, self.backups):
            folder.mkdir(parents=True, exist_ok=True)

    def record(self, message: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(self.log, "a", encoding="utf-8") as fh:
                fh.write("[%s] %s\n" % (stamp, message))
        except OSError:
            pass

    def history(self, limit: int = 20) -> list:
        if not self.log.is_file():
            return []
        try:
            lines = self.log.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return lines[-limit:]

    def new_batch(self, server, reason: str) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        folder = self.quarantine / ("%s-%s" % (stamp, server.id))
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "info.txt").write_text(
            "server: %s\nserver_id: %s\nquando: %s\nmotivo: %s\n"
            % (server.name, server.id, time.strftime("%Y-%m-%d %H:%M:%S"), reason),
            encoding="utf-8")
        return folder

    def batches(self) -> list:
        if not self.quarantine.is_dir():
            return []
        return sorted((d for d in self.quarantine.iterdir() if d.is_dir()), reverse=True)


def backup_server(home: MedicHome, server: Server) -> Path | None:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    destination = home.backups / ("%s-%s.tar.gz" % (server.id, stamp))
    try:
        with tarfile.open(destination, "w:gz") as tar:
            for folder in ("mods", "plugins", "config"):
                path = server.path / folder
                if path.is_dir():
                    tar.add(path, arcname=folder)
            for path in sorted(server.path.glob("*")):
                if path.is_file() and path.suffix.lower() in (".properties", ".yml", ".yaml", ".txt", ".json", ".toml"):
                    tar.add(path, arcname=path.name)
    except OSError as exc:
        warn("backup non riuscito: %s" % exc)
        return None
    return destination


def do_quarantine(home: MedicHome, server: Server, jars: list, reason: str, batch: Path | None = None) -> Path:
    batch = batch or home.new_batch(server, reason)
    for jar in jars:
        destination = batch / jar.rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(jar.path), str(destination))
        ok("in quarantena: %s" % jar.rel)
        home.record("quarantena %s: %s (%s)" % (server.name, jar.rel, reason))
    return batch


def do_move(server: Server, jar: Jar, destination_folder: str, batch: Path | None = None) -> bool:
    destination_dir = server.path / destination_folder
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / jar.path.name
    if destination.exists():
        warn("esiste gia' %s: salto" % destination.relative_to(server.path))
        return False
    original_rel = jar.rel
    shutil.move(str(jar.path), str(destination))
    new_rel = destination.relative_to(server.path).as_posix()
    if batch is not None:
        try:
            with open(batch / "spostamenti.txt", "a", encoding="utf-8") as fh:
                fh.write("%s|%s\n" % (original_rel, new_rel))
        except OSError:
            pass
    ok("spostato %s -> %s/" % (original_rel, destination_folder))
    return True


# --------------------------------------------------------------------------- #
#  aggiornamenti da Modrinth
# --------------------------------------------------------------------------- #
MODRINTH = "https://api.modrinth.com/v2"
PROJECT_TYPE = {"fabric": "mod", "forge": "mod", "neoforge": "mod", "quilt": "mod",
                "bukkit": "plugin", "paper": "plugin", "spigot": "plugin"}
LOADERS = {"fabric": ["fabric", "quilt"], "forge": ["forge"], "neoforge": ["neoforge", "forge"],
           "bukkit": ["bukkit", "spigot", "paper", "purpur"], "paper": ["paper", "purpur", "bukkit", "spigot"]}


def http_json(url: str, timeout: int = 25):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def modrinth_candidates(jar: Jar, loader: str, mc_version: str) -> list:
    facets = [["project_type:" + PROJECT_TYPE.get(loader, "mod")]]
    if loader:
        facets.append(["categories:" + loader])
    if mc_version:
        facets.append(["versions:" + mc_version])
    import urllib.parse
    query = urllib.parse.urlencode({"query": jar.label or jar.name, "limit": 8, "index": "relevance",
                                    "facets": json.dumps(facets)})
    payload = http_json(MODRINTH + "/search?" + query)
    hits = payload.get("hits", []) if isinstance(payload, dict) else []
    out = []
    for hit in hits:
        title = hit.get("title", "")
        score = _similarity(jar.label or jar.name, title, hit.get("slug", ""))
        out.append({"id": hit.get("project_id") or hit.get("slug"), "slug": hit.get("slug", ""),
                    "title": title, "type": hit.get("project_type", ""), "score": score})
    out.sort(key=lambda c: -c["score"])
    return out


def _similarity(query: str, title: str, slug: str) -> float:
    left, right, slug_norm = normalize(query), normalize(title), normalize(slug)
    if not left:
        return 0.0
    if left == right or (slug_norm and left == slug_norm):
        return 100.0
    score = 0.0
    if right and (left in right or right in left):
        score = 88.0
    if slug_norm and (left in slug_norm or slug_norm in left):
        score = max(score, 88.0)
    if score < 88 and right:
        common = len(set(left) & set(right))
        score = max(score, common / max(len(set(left) | set(right)), 1) * 100)
    return round(score, 1)


def modrinth_best(project_id: str, loader: str, mc_version: str):
    import urllib.parse
    params = {}
    if loader:
        params["loaders"] = json.dumps(LOADERS.get(loader, [loader]))
    if mc_version:
        params["game_versions"] = json.dumps([mc_version])
    url = MODRINTH + "/project/%s/version" % project_id
    if params:
        url += "?" + urllib.parse.urlencode(params)
    versions = http_json(url)
    if not isinstance(versions, list) or not versions:
        return None
    wanted = set(LOADERS.get(loader, [loader]))
    usable = [v for v in versions
              if (not wanted or (set(str(x).lower() for x in v.get("loaders", [])) & wanted))
              and (not mc_version or mc_version in [str(x) for x in v.get("game_versions", [])])
              and v.get("files")]
    if not usable:
        usable = [v for v in versions if v.get("files") and
                  (not mc_version or mc_version in [str(x) for x in v.get("game_versions", [])])]
    if not usable:
        return None
    usable.sort(key=lambda v: v.get("date_published", ""), reverse=True)
    return usable[0]


def download_file(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        with open(destination, "wb") as fh:
            shutil.copyfileobj(response, fh)


def do_update(home: MedicHome, server: Server, jar: Jar, loader: str, mc_version: str, forced_slug: str = "") -> bool:
    head("Aggiornamento di %s" % jar.rel)
    try:
        if forced_slug:
            candidates = [{"id": forced_slug, "slug": forced_slug, "title": forced_slug, "score": 100.0}]
        else:
            candidates = modrinth_candidates(jar, loader, mc_version)
    except Exception as exc:
        warn("non riesco a contattare Modrinth: %s" % exc)
        return False
    if not candidates:
        warn("nessun progetto trovato su Modrinth per '%s'" % jar.label)
        return False
    best = candidates[0]
    if best["score"] < 55:
        warn("non sono sicuro del progetto giusto. Candidati:")
        for candidate in candidates[:5]:
            info("%s (%s) - %.0f%%" % (candidate["title"], candidate["slug"], candidate["score"]))
        info("Riprova con: python3 %s update %s --file %s --project <slug>" % (sys.argv[0], server.id, jar.rel))
        return False
    try:
        version = modrinth_best(best["id"], loader, mc_version)
    except Exception as exc:
        warn("ricerca versioni fallita: %s" % exc)
        return False
    if not version:
        warn("nessuna versione compatibile con Minecraft %s" % mc_version)
        return False
    files = version.get("files") or []
    file_info = next((f for f in files if f.get("primary")), files[0] if files else None)
    if not file_info:
        warn("versione senza file scaricabile")
        return False
    info("trovato: %s %s" % (best["title"], version.get("version_number", version.get("name", ""))))
    if not _confirm("Scaricare e sostituire %s?" % jar.rel):
        return False
    tmp = jar.path.with_suffix(".nuovo")
    try:
        download_file(file_info["url"], tmp)
    except Exception as exc:
        warn("download fallito: %s" % exc)
        if tmp.exists():
            tmp.unlink()
        return False
    do_quarantine(home, server, [jar], "sostituito dall'aggiornamento %s" % version.get("version_number", ""))
    shutil.move(str(tmp), str(jar.path))
    ok("aggiornato %s -> %s %s" % (jar.rel, best["title"], version.get("version_number", "")))
    home.record("aggiornamento %s: %s -> %s %s" % (server.name, jar.rel, best["slug"], version.get("version_number", "")))
    return True


# --------------------------------------------------------------------------- #
#  interfaccia a riga di comando
# --------------------------------------------------------------------------- #
def _confirm(question: str, default_yes: bool = True) -> bool:
    suffix = " [S/n] " if default_yes else " [s/N] "
    try:
        answer = input(_c("  ? ", "36") + question + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not answer:
        return default_yes
    return answer in ("s", "si", "y", "yes")


def _pick(prompt: str, options: list | None = None, allow_empty: bool = True) -> str:
    try:
        answer = input(_c("  > ", "36") + prompt + " ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    if allow_empty and not answer:
        return ""
    return answer


def print_findings(server: Server, findings: list) -> None:
    if not findings:
        ok("nessun problema rilevato")
        return
    for index, finding in enumerate(findings, 1):
        color = {"critico": "31", "alto": "33"}.get(finding.severity, "36")
        print()
        print(_c("[%d] %s" % (index, finding.title), "1;%s" % color))
        print("    %s" % finding.advice)
        print("    severita': %s | affidabilita': %d%% | da: %s" %
              (finding.severity, finding.confidence, "log" if finding.source == "log" else "scansione file"))
        print("    file: %s" % finding.target_names)
        if finding.unmatched:
            print("    non collegato a un file: %s" % ", ".join(finding.unmatched))
        if finding.evidence:
            snippet = "\n".join(finding.evidence[-8:])
            print(_c("    ---", "90"))
            for line in snippet.splitlines():
                print(_c("    " + line[:150], "90"))
            print(_c("    ---", "90"))


def action_for(finding: Finding, server: Server) -> str:
    if finding.remedy == "move":
        destination = finding.note or ("plugins" if finding.category == "plugin" else "mods")
        return "sposta in %s/" % destination
    if finding.remedy == "update":
        return "aggiorna (Modrinth) o metti in quarantena"
    if finding.remedy == "quarantine":
        return "metti in quarantena"
    if finding.remedy == "config":
        return "da sistemare nella configurazione di Crafty"
    return "intervento manuale"


def apply_finding(home: MedicHome, server: Server, finding: Finding, use_update: bool = False,
                  batch: Path | None = None) -> bool:
    if not finding.targets:
        return False
    if finding.remedy == "config" or finding.remedy == "manual":
        return False
    if finding.remedy == "move":
        destination = finding.note or ("plugins" if finding.category == "plugin" else "mods")
        moved = False
        for jar in finding.targets:
            if do_move(server, jar, destination, batch=batch):
                home.record("spostamento %s: %s -> %s/" % (server.name, jar.rel, destination))
                moved = True
        return moved
    if finding.remedy == "update" and use_update:
        done = False
        for jar in finding.targets:
            if do_update(home, server, jar, server.loader, server.mc_version):
                done = True
        if done:
            return True
    for jar in finding.targets:
        do_quarantine(home, server, [jar], finding.title, batch=batch)
    return True


def cmd_scan(args, home: MedicHome, servers_dir: Path) -> int:
    servers = discover_servers(servers_dir)
    if not servers:
        err("nessun server trovato in %s" % servers_dir)
        info("se i server stanno altrove: python3 %s scan --dir /percorso/corretto" % sys.argv[0])
        return 1
    if args.server:
        servers = [s for s in servers if s.id == args.server or s.name == args.server or args.server in s.id]
        if not servers:
            err("server '%s' non trovato" % args.server)
            return 1
    total = 0
    json_out = []
    for server in servers:
        if not args.json:
            head("Server: %s  (%s)" % (server.name, server.path))
        server.jars = scan_jars(server)
        read_log(server)
        server.mc_version = detect_mc_version(server.log_text)
        server.loader = detect_loader(server.log_text) or guess_loader_from_files(server)
        server.running = is_process_running(server)
        if not args.json:
            print("    Minecraft: %s | loader: %s | jar installati: %d | %s" % (
                server.mc_version or "?", server.loader or "?", len(server.jars),
                _c("IN ESECUZIONE", "31") if server.running else "fermo"))
            if server.log_path:
                print("    log: %s" % server.log_path)
            else:
                warn("nessun log trovato (logs/latest.log): prova ad avviare il server una volta")
        findings = analyze(server)
        total += len([f for f in findings if f.severity in ("critico", "alto")])
        if args.json:
            json_out.append({
                "id": server.id, "name": server.name, "path": str(server.path),
                "mc_version": server.mc_version, "loader": server.loader,
                "running": server.running,
                "findings": [{"rule": f.rule_id, "title": f.title, "severity": f.severity,
                              "confidence": f.confidence, "remedy": f.remedy, "advice": f.advice,
                              "files": [j.rel for j in f.targets],
                              "unmatched": f.unmatched} for f in findings],
            })
            continue
        print_findings(server, findings)
        if findings and any(f.remedy in ("quarantine", "move", "update") for f in findings):
            print()
            info("per riparare: python3 %s fix %s" % (sys.argv[0], server.id))

    if args.json:
        print(json.dumps(json_out, ensure_ascii=False, indent=2))
        return 0
    rule("=")
    if total:
        warn("trovati %d problemi importanti in tutto" % total)
    else:
        ok("nessun problema importante rilevato")
    info("quarantena e backup: %s" % home.path)
    return 0


def cmd_fix(args, home: MedicHome, servers_dir: Path) -> int:
    servers = discover_servers(servers_dir)
    if args.server:
        servers = [s for s in servers if s.id == args.server or s.name == args.server or args.server in s.id]
    if not servers:
        err("server non trovato")
        return 1
    if len(servers) > 1 and not args.auto:
        head("Piu' server trovati")
        for index, server in enumerate(servers, 1):
            print("  %d) %s (%s)" % (index, server.name, server.id))
        choice = _pick("quale numero?", None)
        try:
            servers = [servers[int(choice) - 1]]
        except (ValueError, IndexError):
            err("scelta non valida")
            return 1

    exit_code = 0
    for server in servers:
        head("Server: %s" % server.name)
        server.jars = scan_jars(server)
        read_log(server)
        server.mc_version = detect_mc_version(server.log_text)
        server.loader = detect_loader(server.log_text) or guess_loader_from_files(server)
        server.running = is_process_running(server)
        if server.running:
            warn("il server e' in esecuzione: spostare i jar adesso puo' creare confusione")
            if not args.auto and not _confirm("Fermo prima? (Crafty lo riavvia da solo se configurato)", False):
                info("continuo senza fermarlo")
        findings = analyze(server)
        fixable = [f for f in findings if f.remedy in ("quarantine", "move", "update") and f.targets]
        if not fixable:
            ok("niente da riparare automaticamente")
            for finding in findings:
                print("    - %s: %s" % (finding.title, finding.advice))
            continue
        print_findings(server, findings)

        if args.auto:
            selected = fixable
        else:
            head("Cosa faccio?")
            for index, finding in enumerate(fixable, 1):
                print("  %d) [%s] %s -> %s" % (index, finding.severity, finding.title, finding.target_names))
                print("       azione: %s" % action_for(finding, server))
            choice = _pick("numeri da applicare (es. 1,3) o Invio per annullare:", None)
            if not choice:
                info("nessuna modifica fatta")
                continue
            selected = []
            for chunk in re.split(r"[,\s]+", choice):
                try:
                    selected.append(fixable[int(chunk) - 1])
                except (ValueError, IndexError):
                    pass
        if not selected:
            info("nessuna modifica fatta")
            continue

        if args.backup:
            archive = backup_server(home, server)
            if archive:
                ok("backup: %s" % archive)

        batch = home.new_batch(server, "riparazione automatica")
        changed = False
        for finding in selected:
            if apply_finding(home, server, finding, use_update=args.update, batch=batch):
                changed = True
        if changed:
            home.record("riparazione completata su %s" % server.name)
            print()
            if server.running:
                warn("riavvia il server da Crafty per verificare")
            else:
                warn("avvia il server da Crafty per verificare")
            info("se non riparte: python3 %s undo" % sys.argv[0])
        else:
            info("nessuna modifica applicata")
            exit_code = 1
    return exit_code


def cmd_undo(args, home: MedicHome, servers_dir: Path) -> int:
    batches = home.batches()
    if not batches:
        ok("la quarantena e' vuota")
        return 0
    if args.batch:
        batches = [b for b in batches if args.batch in b.name]
        if not batches:
            err("lotto '%s' non trovato" % args.batch)
            return 1

    if not args.all:
        folder = batches[0]
        head("Ultimo intervento")
        print("  " + batch_or_first([folder]))
        if len(batches) > 1:
            info("altri %d interventi in quarantena (quarantine per vederli)" % (len(batches) - 1))
        if not args.yes and not _confirm("Ripristinare questi file?"):
            info("nessuna modifica fatta")
            return 0
        batches = [folder]

    servers = {s.id: s for s in discover_servers(servers_dir)}
    restored, manual = 0, []
    for folder in batches:
        server_id = ""
        info_file = folder / "info.txt"
        if info_file.is_file():
            for line in info_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("server_id:"):
                    server_id = line.split(":", 1)[1].strip()
        server = servers.get(server_id)
        if server is None:
            manual.append(str(folder))
            continue
        moves_file = folder / "spostamenti.txt"
        if moves_file.is_file():
            for line in moves_file.read_text(encoding="utf-8").splitlines():
                if "|" not in line:
                    continue
                original_rel, new_rel = line.split("|", 1)
                source = server.path / new_rel
                destination = server.path / original_rel
                if source.exists():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(source), str(destination))
                    ok("riportato %s -> %s" % (new_rel, original_rel))
                    restored += 1
        for path in sorted(folder.rglob("*")):
            if path.is_dir() or path.name in ("info.txt", "spostamenti.txt"):
                continue
            rel = path.relative_to(folder).as_posix()
            destination = server.path / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(destination))
            ok("ripristinato %s" % rel)
            restored += 1
        home.record("ripristino del lotto %s" % folder.name)
        for leftover in sorted(folder.rglob("*"), reverse=True):
            try:
                leftover.rmdir() if leftover.is_dir() else None
            except OSError:
                pass
        try:
            folder.rmdir()
        except OSError:
            pass

    if manual:
        for folder in manual:
            warn("lotto %s: non trovo il server, i file restano in quarantena" % folder)
    print()
    if restored:
        ok("%d file ripristinati" % restored)
        warn("riavvia il server da Crafty per verificare")
    return 0


def batch_or_first(batches: list) -> str:
    folder = batches[0]
    info_file = folder / "info.txt"
    detail = ""
    if info_file.is_file():
        detail = " | " + " | ".join(info_file.read_text(encoding="utf-8").strip().splitlines())
    files = [p.relative_to(folder).as_posix() for p in sorted(folder.rglob("*"))
             if p.is_file() and p.name not in ("info.txt", "spostamenti.txt")]
    extra = ""
    moves_file = folder / "spostamenti.txt"
    if moves_file.is_file():
        moves = [l.replace("|", " -> ") for l in moves_file.read_text(encoding="utf-8").splitlines() if "|" in l]
        if moves:
            extra = "\n     spostamenti: %s" % ", ".join(moves)
    return "%s%s\n     file: %s%s" % (folder.name, detail, ", ".join(files) or "-", extra)


def cmd_quarantine(args, home: MedicHome) -> int:
    batches = home.batches()
    if not batches:
        ok("la quarantena e' vuota")
        return 0
    head("Quarantena (%d interventi)" % len(batches))
    for folder in batches:
        print("  " + batch_or_first([folder]).replace("\n     ", "\n     "))
        print()
    info("per annullare l'ultimo: python3 %s undo" % sys.argv[0])
    info("per annullarne uno preciso: python3 %s undo <nome-lotto>" % sys.argv[0])
    return 0


def cmd_update(args, home: MedicHome, servers_dir: Path) -> int:
    if not args.file:
        err("indica il file: --file mods/nome.jar")
        return 1
    servers = discover_servers(servers_dir)
    server = next((s for s in servers if s.id == args.server or args.server in s.id or s.name == args.server), None)
    if server is None:
        err("server non trovato")
        return 1
    server.jars = scan_jars(server)
    read_log(server)
    server.mc_version = args.mc or detect_mc_version(server.log_text)
    server.loader = args.loader or detect_loader(server.log_text) or guess_loader_from_files(server)
    jar = next((j for j in server.jars if j.rel == args.file or j.path.name == args.file), None)
    if jar is None:
        err("file non trovato: %s" % args.file)
        return 1
    if not server.mc_version and not _confirm("Non conosco la versione di Minecraft del server. Continuo?"):
        return 1
    if args.backup:
        backup_server(home, server)
    success = do_update(home, server, jar, server.loader, server.mc_version, forced_slug=args.project or "")
    if success:
        warn("riavvia il server da Crafty per verificare")
    return 0 if success else 1


def cmd_history(args, home: MedicHome) -> int:
    lines = home.history(args.limit)
    if not lines:
        ok("nessuna operazione registrata")
        return 0
    head("Ultime operazioni")
    for line in lines:
        print("  " + line)
    return 0


def cmd_menu(home: MedicHome, servers_dir: Path) -> int:
    print(_c("\n  Crafty Medic %s" % VERSION, "1;36"))
    print("  dentro il container di Crafty Controller")
    rule("=")
    servers = discover_servers(servers_dir)
    if not servers:
        err("nessun server trovato in %s" % servers_dir)
        info("se i server stanno altrove: python3 %s scan --dir /percorso" % sys.argv[0])
        return 1
    head("Server trovati")
    for index, server in enumerate(servers, 1):
        server.jars = scan_jars(server)
        read_log(server)
        server.mc_version = detect_mc_version(server.log_text)
        server.loader = detect_loader(server.log_text) or guess_loader_from_files(server)
        findings = analyze(server)
        important = [f for f in findings if f.severity in ("critico", "alto")]
        status = _c("%d problemi" % len(important), "31") if important else _c("ok", "32")
        print("  %d) %-28s MC %-8s %-9s %s" % (index, server.name[:28], server.mc_version or "?",
                                               server.loader or "?", status))
    print("  a) analizza tutti")
    print("  u) annulla l'ultima riparazione")
    print("  q) esci")
    choice = _pick("cosa faccio?", None).strip().lower()
    if choice in ("q", ""):
        return 0
    if choice == "u":
        return cmd_undo(argparse.Namespace(batch="", all=False, yes=False), home, servers_dir)
    if choice == "a":
        return cmd_scan(argparse.Namespace(server=""), home, servers_dir)
    try:
        server = servers[int(choice) - 1]
    except (ValueError, IndexError):
        err("scelta non valida")
        return 1
    return cmd_fix(argparse.Namespace(server=server.id, auto=False, backup=True, update=False), home, servers_dir)


def resolve_home(explicit: str, servers_dir: Path) -> tuple:
    candidates = [explicit] if explicit else []
    candidates.append(os.environ.get("CRAFTY_MEDIC_HOME", ""))
    candidates += [
        str(servers_dir.parent / "crafty-medic"),
        "/crafty/backups/crafty-medic",
        "/crafty/crafty-medic",
        str(Path.home() / ".crafty-medic"),
    ]
    for candidate in candidates:
        if candidate:
            path = Path(candidate)
            try:
                path.mkdir(parents=True, exist_ok=True)
                return MedicHome(path), True
            except OSError:
                continue
    return MedicHome(Path("/tmp/crafty-medic")), False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="crafty-medic",
        description="Trova e ripara i mod/plugin che impediscono al server Minecraft di partire.")
    parser.add_argument("comando", nargs="?", default="",
                        choices=["", "scan", "fix", "undo", "quarantine", "update", "history"],
                        help="scan=analizza, fix=ripara, undo=annulla, quarantine=lista, update=aggiorna, history=storico")
    parser.add_argument("server", nargs="?", default="", help="id o nome del server (facoltativo)")
    parser.add_argument("--dir", dest="servers_dir", default="", help="cartella dei server di Crafty")
    parser.add_argument("--home", dest="home", default="", help="cartella di quarantena/backup")
    parser.add_argument("--auto", action="store_true", help="applica tutte le riparazioni senza chiedere")
    parser.add_argument("--update", action="store_true", help="prova ad aggiornare da Modrinth invece di mettere in quarantena")
    parser.add_argument("--no-backup", dest="backup", action="store_false", help="non creare il backup")
    parser.add_argument("--yes", action="store_true", help="non chiedere conferme")
    parser.add_argument("--all", action="store_true", help="undo: annulla tutti i lotti")
    parser.add_argument("--batch", default="", help="undo: nome del lotto")
    parser.add_argument("--file", default="", help="update: file jar da aggiornare")
    parser.add_argument("--project", default="", help="update: slug Modrinth (se il nome non basta)")
    parser.add_argument("--mc", default="", help="update: versione di Minecraft")
    parser.add_argument("--loader", default="", help="update: loader (fabric/forge/paper)")
    parser.add_argument("--limit", type=int, default=20, help="history: quante righe")
    parser.add_argument("--json", action="store_true", help="output in JSON (solo scan)")
    parser.add_argument("--version", action="version", version="Crafty Medic %s" % VERSION)
    args, unknown = parser.parse_known_args(argv)
    # accetta il nome del server prima o dopo le opzioni (es. "scan demo-1 --dir ...")
    for extra in unknown:
        if not extra.startswith("-") and not args.server:
            args.server = extra

    servers_dir = find_servers_dir(args.servers_dir)
    home, persistent = resolve_home(args.home, servers_dir)

    if not persistent:
        warn("cartella di quarantena non scrivibile: uso /tmp (i file potrebbero perdersi)")
    if args.comando == "scan":
        return cmd_scan(args, home, servers_dir)
    if args.comando == "fix":
        return cmd_fix(args, home, servers_dir)
    if args.comando == "undo":
        return cmd_undo(args, home, servers_dir)
    if args.comando == "quarantine":
        return cmd_quarantine(args, home)
    if args.comando == "update":
        return cmd_update(args, home, servers_dir)
    if args.comando == "history":
        return cmd_history(args, home)
    print("cartella server: %s" % servers_dir)
    return cmd_menu(home, servers_dir)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrotto")
        sys.exit(130)

"""Regole di riconoscimento degli errori nei log di Minecraft.

Ogni regola ha un pattern regex, una funzione `extract` che prova a
individuare il file/mod/plugin colpevole e un rimedio consigliato.

Categorie:  mod | plugin | java | config | world | generic
Severita':  critical | high | medium | low
Rimedi:     quarantine | update | move | config | manual
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

Extractor = Callable[[re.Match, list[str], int], list[str]]
Matcher = Callable[[re.Match], bool]

# --------------------------------------------------------------------------- #
# utility usate dalle `extract`
# --------------------------------------------------------------------------- #
_COLOR_RE = re.compile(r"§[0-9a-fk-orA-FK-OR]")

FILE_IN_QUOTES = re.compile(r"'(?P<file>[^']+\.jar)'")
MOD_SUBJECT = re.compile(r"Mod '(?P<name>[^']+)' \((?P<id>[^)]+)\)")
LOADING_PLUGIN = re.compile(r"Could not(?: pass| load)?[^\n]*'(?P<file>plugins/[^']+\.jar)'", re.I)
ENABLING_PLUGIN = re.compile(r"enabling (?P<name>[^\s]+) v(?P<ver>[^\s(]+)", re.I)


def strip_colors(text: str) -> str:
    return _COLOR_RE.sub("", text)


def clean(token: str | None) -> str:
    if not token:
        return ""
    token = strip_colors(token).strip()
    token = token.strip("\"'`.,;:!()[]{} ")
    token = token.replace("\\", "/").strip()
    return token


def lookback(lines: list[str], idx: int, pattern: re.Pattern, window: int = 6) -> re.Match | None:
    """Cerca `pattern` nelle righe precedenti (utile per 'Could not load plugins/x.jar')."""
    start = max(0, idx - window)
    for i in range(idx, start - 1, -1):
        match = pattern.search(lines[i])
        if match:
            return match
    return None


def lookahead(lines: list[str], idx: int, pattern: re.Pattern, window: int = 4) -> re.Match | None:
    for i in range(idx, min(len(lines), idx + window + 1)):
        match = pattern.search(lines[i])
        if match:
            return match
    return None


def _tokens(*values: str | None) -> list[str]:
    out: list[str] = []
    for value in values:
        value = clean(value)
        if value and value.lower() not in {"minecraft", "java"}:
            out.append(value)
        elif value and value.lower() == "minecraft":
            out.append("minecraft")
    return out


# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    category: str
    severity: str
    pattern: str
    advice: str
    remedy: str = "quarantine"
    confidence: int = 70
    context: int = 6
    extract: Optional[Extractor] = None
    when: Optional[Matcher] = None
    flags: int = re.IGNORECASE
    compiled: re.Pattern = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        object.__setattr__(self, "compiled", re.compile(self.pattern, self.flags))


# =========================================================================== #
#  FABRIC
# =========================================================================== #
def _fabric_missing_dep(m: re.Match, lines: list[str], idx: int) -> list[str]:
    groups = m.groupdict()
    dep = clean(groups.get("dep"))
    if dep.lower() == "minecraft":
        return _tokens(groups.get("subj_id"), groups.get("subj_name"))
    # il colpevole "sicuro" e' il mod che richiede la dipendenza mancante
    tokens = _tokens(groups.get("subj_id"), groups.get("subj_name"))
    if dep:
        tokens.append(dep)
    return tokens


def _fabric_subjects(m: re.Match, lines: list[str], idx: int) -> list[str]:
    g = m.groupdict()
    return _tokens(g.get("subj_id"), g.get("subj_name"), g.get("id"), g.get("name"))


def _fabric_incompat(m: re.Match, lines: list[str], idx: int) -> list[str]:
    g = m.groupdict()
    return _tokens(g.get("subj_id"), g.get("subj_name"), g.get("bid"), g.get("bname"))


def _fabric_entrypoint(m: re.Match, lines: list[str], idx: int) -> list[str]:
    g = m.groupdict()
    return _tokens(g.get("id"))


def _fabric_generic(m: re.Match, lines: list[str], idx: int) -> list[str]:
    """Cerca nel blocco di errore i mod citati (es. 'provided by x')."""
    tokens: list[str] = []
    for i in range(idx, min(len(lines), idx + 15)):
        for match in MOD_SUBJECT.finditer(lines[i]):
            tokens.extend(_tokens(match.group("id"), match.group("name")))
        provided = re.search(r"provided by '(?P<id>[^']+)'", lines[i])
        if provided:
            tokens.extend(_tokens(provided.group("id")))
    # deduplica mantenendo l'ordine
    seen: set[str] = set()
    out = []
    for token in tokens:
        if token.lower() not in seen:
            seen.add(token.lower())
            out.append(token)
    return out[:6]


FABRIC_RULES = [
    Rule(
        id="fabric_minecraft_version",
        title="Mod costruita per un'altra versione di Minecraft",
        category="mod",
        severity="critical",
        confidence=95,
        remedy="update",
        pattern=r"Mod '(?P<subj_name>.+?)' \((?P<subj_id>[^)]+)\)[^!]*?requires (?:version (?P<spec>[^\s]+) of |any version of )minecraft, which is missing",
        advice="Questo mod non supporta la versione di Minecraft del server. Aggiornalo a una release compatibile oppure rimuovilo.",
        extract=lambda m, l, i: _tokens(m.group("subj_id"), m.group("subj_name")),
    ),
    Rule(
        id="fabric_missing_dependency",
        title="Dipendenza mancante",
        category="mod",
        severity="critical",
        confidence=90,
        remedy="quarantine",
        pattern=r"Mod '(?P<subj_name>.+?)' \((?P<subj_id>[^)]+)\)[^!]*?requires (?:version (?P<spec>[^\s]+) of |any version of )(?:mod )?(?P<dep>'[^']+'|[^\s,!]+), which is missing",
        advice="Il mod indicato richiede un altro mod che non e' installato. Puoi rimuoverlo o installare la dipendenza mancante.",
        extract=_fabric_missing_dep,
        when=lambda m: clean(m.groupdict().get("dep")).lower() != "minecraft",
    ),
    Rule(
        id="fabric_incompatible",
        title="Due mod incompatibili tra loro",
        category="mod",
        severity="critical",
        confidence=85,
        remedy="quarantine",
        pattern=r"Mod '(?P<subj_name>.+?)' \((?P<subj_id>[^)]+)\)[^!]*?is incompatible with (?:mod )?'(?P<bname>.+?)' \((?P<bid>[^)]+)\)",
        advice="Questi due mod non possono convivere: rimuovine uno (quello meno importante per te).",
        extract=_fabric_incompat,
    ),
    Rule(
        id="fabric_duplicate_mod",
        title="Mod duplicato",
        category="mod",
        severity="high",
        confidence=85,
        remedy="quarantine",
        pattern=r"(?:duplicate (?:mod|mod ?id|modid)|non-unique mod ?id|mods? with (?:the )?same (?:mod ?)?id)[^'\"]*['\"]?(?P<id>[\w.\-]+)['\"]?",
        advice="Ci sono due copie dello stesso mod (spesso una vecchia e una nuova). Tienine una sola.",
        extract=_fabric_subjects,
    ),
    Rule(
        id="fabric_entrypoint_error",
        title="Errore durante l'avvio di un mod",
        category="mod",
        severity="critical",
        confidence=80,
        remedy="quarantine",
        pattern=r"Could not execute entrypoint stage '(?P<stage>[\w]+)' due to errors, provided by '(?P<id>[^']+)'",
        advice="Il mod si e' rotto durante l'inizializzazione: quasi sempre serve aggiornarlo o rimuoverlo.",
        extract=_fabric_entrypoint,
    ),
    Rule(
        id="fabric_client_mod",
        title="Mod solo client installato sul server",
        category="mod",
        severity="high",
        confidence=70,
        remedy="quarantine",
        pattern=r"Mod '(?P<subj_name>.+?)' \((?P<subj_id>[^)]+)\)[^!]*?(?:is a client(?:-|\s)?(?:side|only) mod|requires environment CLIENT|environment CLIENT)",
        advice="Questo mod funziona solo sul client: va tolto dalla cartella mods/ del server.",
        extract=_fabric_subjects,
    ),
    Rule(
        id="fabric_resolution_failed",
        title="Fabric non riesce a caricare i mod",
        category="mod",
        severity="high",
        confidence=45,
        remedy="manual",
        pattern=r"ModResolutionException",
        advice="Fabric ha rilevato un problema nella risoluzione dei mod: controlla le righe qui sopra per capire quale.",
        extract=_fabric_generic,
        context=15,
    ),
]

# =========================================================================== #
#  FORGE / NEOFORGE
# =========================================================================== #
def _forge_missing_dep(m: re.Match, lines: list[str], idx: int) -> list[str]:
    g = m.groupdict()
    return _tokens(g.get("by"), g.get("dep"))


def _forge_mod_loading(m: re.Match, lines: list[str], idx: int) -> list[str]:
    g = m.groupdict()
    return _tokens(g.get("id"), g.get("name"))


FORGE_RULES = [
    Rule(
        id="forge_missing_dependency",
        title="Dipendenza obbligatoria mancante",
        category="mod",
        severity="critical",
        confidence=92,
        remedy="quarantine",
        pattern=r"Mod ID: '(?P<dep>[^']+)', Requested by: '(?P<by>[^']+)'",
        advice="Il mod 'Requested by' ha bisogno di un altro mod non installato. Rimuovilo o installa la dipendenza.",
        extract=_forge_missing_dep,
    ),
    Rule(
        id="forge_missing_mods_exception",
        title="Forge: mod richiesto non presente",
        category="mod",
        severity="critical",
        confidence=88,
        remedy="quarantine",
        pattern=r"Mod (?P<name>[^\s]+) \((?P<id>[^)]+)\) requires mod (?P<bname>[^\s]+) \((?P<bid>[^)]+)\)",
        advice="Manca un mod richiesto da quello indicato.",
        extract=lambda m, l, i: _tokens(m.group("id"), m.group("name"), m.group("bid")),
    ),
    Rule(
        id="forge_minecraft_version",
        title="Mod per un'altra versione di Minecraft",
        category="mod",
        severity="critical",
        confidence=90,
        remedy="update",
        pattern=r"(?:Mod )?(?P<name>[^\s]+) \((?P<id>[^)]+)\) requires minecraft (?:version )?(?P<spec>[^\s]+)",
        advice="Il mod non e' compatibile con la versione di Minecraft del server: aggiornalo o rimuovilo.",
        extract=_forge_mod_loading,
    ),
    Rule(
        id="forge_mod_loading_error",
        title="Errore nel caricamento di un mod",
        category="mod",
        severity="critical",
        confidence=75,
        remedy="quarantine",
        pattern=r"Mod (?P<name>.+?) \((?P<id>[^)]+)\) encountered an error during the (?P<phase>[\w ]+?) (?:event )?phase",
        advice="Il mod si e' rotto durante il caricamento: prova ad aggiornarlo, altrimenti rimuovilo.",
        extract=_forge_mod_loading,
    ),
    Rule(
        id="forge_missing_modstoml",
        title="File .jar non valido come mod",
        category="mod",
        severity="high",
        confidence=80,
        remedy="move",
        pattern=r"(?:File|Mod File) (?P<file>[^\s]+\.jar) is missing mods\.toml",
        advice="Questo jar non e' un mod Forge: probabilmente e' un plugin Bukkit, il mod sbagliato o un download corrotto.",
        extract=lambda m, l, i: _tokens(m.group("file")),
    ),
    Rule(
        id="forge_mixin_failure",
        title="Mixin fallito",
        category="mod",
        severity="critical",
        confidence=75,
        remedy="quarantine",
        pattern=r"Mixin apply for mod (?P<id>[\w.\-]+) failed",
        advice="Un mod ha provato a modificare il codice di Minecraft e ha fallito: e' il sintomo classico di un mod non aggiornato.",
        extract=lambda m, l, i: _tokens(m.group("id")),
    ),
]

# =========================================================================== #
#  BUKKIT / SPIGOT / PAPER
# =========================================================================== #
def _plugin_file_from_context(m: re.Match, lines: list[str], idx: int) -> list[str]:
    """Recupera 'plugins/xxx.jar' dalle righe vicine."""
    tokens: list[str] = []
    hit = lookback(lines, idx, LOADING_PLUGIN, window=8)
    if hit:
        tokens.append(clean(hit.group("file")))
    hit = lookback(lines, idx, ENABLING_PLUGIN, window=8)
    if hit:
        tokens.append(clean(hit.group("name")))
    return tokens


def _plugin_unknown_dep(m: re.Match, lines: list[str], idx: int) -> list[str]:
    hit = lookback(lines, idx, LOADING_PLUGIN, window=8)
    if hit:
        return [clean(hit.group("file"))]
    return []


BUKKIT_RULES = [
    Rule(
        id="plugin_unsupported_api",
        title="Plugin troppo vecchio (API non supportata)",
        category="plugin",
        severity="critical",
        confidence=95,
        remedy="update",
        pattern=r"Unsupported API version (?P<api>[\d.]+)",
        advice="Il plugin dichiara un'API più vecchia di quella accettata dal server (es. 1.13 con Minecraft 1.20+). Aggiorna il plugin oppure rimuovilo.",
        extract=_plugin_file_from_context,
    ),
    Rule(
        id="plugin_unknown_dependency",
        title="Plugin: dipendenza mancante",
        category="plugin",
        severity="critical",
        confidence=92,
        remedy="quarantine",
        pattern=r"Unknown(?:-| |/missing )?[Dd]ependency[^\n]*?\[?(?P<deps>[^\]\n]+)\]?",
        advice="Il plugin richiede un altro plugin che non e' installato (es. Vault, ProtocolLib). Rimuovi il plugin o installa la dipendenza.",
        extract=_plugin_unknown_dep,
        when=lambda m: "depend" in m.string[max(0, m.start() - 60):m.end() + 40].lower(),
    ),
    Rule(
        id="plugin_invalid_plugin_yml",
        title="plugin.yml non valido",
        category="plugin",
        severity="critical",
        confidence=90,
        remedy="quarantine",
        pattern=r"(?:InvalidDescriptionException|Invalid plugin\.yml|plugin\.yml (?:is )?(?:invalid|missing|not found)|Could not find (?:required )?'?main'? (?:class|attribute))",
        advice="Il file plugin.yml dentro il jar e' mancante o sbagliato: scarica di nuovo il plugin dalla fonte ufficiale.",
        extract=_plugin_file_from_context,
    ),
    Rule(
        id="plugin_enable_error",
        title="Errore durante l'attivazione del plugin",
        category="plugin",
        severity="high",
        confidence=70,
        remedy="update",
        pattern=r"Error occurred (?:while|when) enabling (?P<name>[^\s]+) v(?P<ver>[^\s(]+)",
        advice="Il plugin si e' attivato ma ha generato un errore: di solito basta aggiornarlo, altrimenti va rimosso.",
        extract=lambda m, l, i: _tokens(m.group("name")),
    ),
    Rule(
        id="plugin_noclassdef",
        title="Classi mancanti (plugin non compatibile)",
        category="plugin",
        severity="high",
        confidence=60,
        remedy="update",
        pattern=r"java\.lang\.(?:NoClassDefFoundError|NoSuchMethodError|NoSuchFieldError): (?P<cls>[^\s]+)",
        advice="Il plugin cerca una classe o un metodo che non esiste in questa versione del server: va aggiornato (o rimosso).",
        extract=_plugin_file_from_context,
    ),
    Rule(
        id="plugin_duplicate",
        title="Plugin duplicato",
        category="plugin",
        severity="high",
        confidence=85,
        remedy="quarantine",
        pattern=r"Ambiguous plugin name '(?P<name>[^']+)' for files '(?P<a>[^']+)' and '(?P<b>[^']+)'",
        advice="Due jar dichiarano lo stesso plugin: tienine uno solo.",
        extract=lambda m, l, i: _tokens(m.group("a"), m.group("b")),
    ),
    Rule(
        id="plugin_load_failed",
        title="Impossibile caricare un plugin",
        category="plugin",
        severity="critical",
        confidence=55,
        remedy="quarantine",
        pattern=r"Could not load '(?P<file>plugins/[^']+\.jar)' in folder '(?P<folder>[^']+)'",
        advice="Il server non e' riuscito a caricare questo plugin: nelle righe successive c'e' la causa.",
        extract=lambda m, l, i: _tokens(m.group("file")),
        context=8,
    ),
    Rule(
        id="plugin_paper_only",
        title="Plugin che richiede Paper",
        category="plugin",
        severity="high",
        confidence=65,
        remedy="manual",
        pattern=r"(?:requires|only (?:supports|works (?:on|with))|must be run on) (?:Paper|Purpur)(?![-\w])",
        advice="Questo plugin funziona solo su Paper/Purpur: passa a Paper oppure rimuovi il plugin.",
        extract=_plugin_file_from_context,
    ),
]

# =========================================================================== #
#  SERVER / JAVA / CONFIG
# =========================================================================== #
SERVER_RULES = [
    Rule(
        id="java_class_version",
        title="Versione di Java sbagliata",
        category="java",
        severity="critical",
        confidence=85,
        remedy="config",
        pattern=r"(?:UnsupportedClassVersionError|class file has wrong version|Unsupported class file (?:major )?version (?P<major>\d+)|has been compiled by a more recent version of the Java Runtime \(class file version (?P<clsver>[\d.]+)\))",
        advice="Il server o un mod/plugin e' stato compilato per una versione di Java diversa da quella usata da Crafty. Cambia la versione di Java nelle impostazioni del server (es. Java 17 per MC 1.18-1.20.4, Java 21 da MC 1.20.5+).",
        extract=lambda m, l, i: [],
    ),
    Rule(
        id="java_too_old",
        title="Java troppo vecchio",
        category="java",
        severity="critical",
        confidence=80,
        remedy="config",
        pattern=r"(?:requires?|need(?:s)?|minimum|update to|upgrade to|running) Java ?(?P<ver>\d+)|(?:unsupported|outdated|invalid) Java version|Java (?P<ver2>\d+) or (?:above|later|newer|higher) is required",
        advice="Questa versione di Minecraft richiede una Java piu' recente: imposta la versione corretta in Crafty (Server > Config > Java).",
        extract=lambda m, l, i: [],
    ),
    Rule(
        id="eula_not_accepted",
        title="EULA non accettata",
        category="config",
        severity="critical",
        confidence=99,
        remedy="config",
        pattern=r"You need to agree to the EULA",
        advice="Apri il file eula.txt del server e imposta eula=true, oppure accetta l'EULA dalla scheda del server in Crafty.",
        extract=lambda m, l, i: [],
    ),
    Rule(
        id="port_in_use",
        title="Porta gia' occupata",
        category="config",
        severity="critical",
        confidence=95,
        remedy="config",
        pattern=r"(?:FAILED TO BIND TO PORT|Failed to bind to port|Address already in use)",
        advice="Un altro processo usa gia' quella porta: cambia la porta del server o ferma l'altro processo.",
        extract=lambda m, l, i: [],
    ),
    Rule(
        id="missing_server_jar",
        title="Jar del server non trovato",
        category="config",
        severity="critical",
        confidence=95,
        remedy="config",
        pattern=r"Unable to access jarfile (?P<file>.+)",
        advice="Crafty non trova il jar del server: ricontrolla il comando di avvio / reinstalla il server jar.",
        extract=lambda m, l, i: [],
    ),
    Rule(
        id="out_of_memory",
        title="Memoria esaurita",
        category="config",
        severity="high",
        confidence=90,
        remedy="config",
        pattern=r"java\.lang\.OutOfMemoryError|There is insufficient memory for the Java Runtime",
        advice="La RAM assegnata non basta: aumentala nelle impostazioni del server (o riduci il numero di mod).",
        extract=lambda m, l, i: [],
    ),
    Rule(
        id="world_load_failed",
        title="Mondo danneggiato o non caricabile",
        category="world",
        severity="critical",
        confidence=70,
        remedy="manual",
        pattern=r"(?:Failed to load world|Exception (?:occurred )?(?:while |in )?loading the world|Failed to read session lock|world .* is corrupt)",
        advice="Il salvataggio del mondo e' danneggiato: ripristina un backup del mondo da Crafty.",
        extract=lambda m, l, i: [],
    ),
    Rule(
        id="startup_failed_generic",
        title="Avvio del server fallito",
        category="generic",
        severity="medium",
        confidence=30,
        remedy="manual",
        pattern=r"Failed to start the minecraft server|Encountered an unexpected exception|This crash report has been saved to",
        advice="Il server e' andato in crash: scorri il log per trovare la causa esatta.",
        extract=_fabric_generic,
        context=10,
    ),
]

RULES: list[Rule] = FABRIC_RULES + FORGE_RULES + BUKKIT_RULES + SERVER_RULES
RULES_BY_ID = {rule.id: rule for rule in RULES}

# --------------------------------------------------------------------------- #
#  Rilevamento versione di Minecraft e loader dal log
# --------------------------------------------------------------------------- #
MC_VERSION_PATTERNS = [
    re.compile(r"Starting minecraft server version (?P<v>[\d.]+)"),
    re.compile(r"Loading Minecraft (?P<v>[\d.]+) with Fabric Loader"),
    re.compile(r"Forge mod loading, version [\d.\-]+, for MC (?P<v>[\d.]+)"),
    re.compile(r"Starting Minecraft server on (?P<v>[\d.]+)"),
    re.compile(r"\(MC: (?P<v>[\d.]+)\)"),
    re.compile(r"Minecraft Version: (?P<v>[\d.]+)"),
    re.compile(r"--version, (?P<v>[\d.]+)"),
]

LOADER_PATTERNS = [
    (re.compile(r"neoforge", re.I), "neoforge"),
    (re.compile(r"Forge mod loading|MinecraftForge|fml\.", re.I), "forge"),
    (re.compile(r"Fabric Loader|fabric-loader|net\.fabricmc", re.I), "fabric"),
    (re.compile(r"This server is running (?:Paper|Purpur) version", re.I), "paper"),
    (re.compile(r"This server is running CraftBukkit|Spigot", re.I), "bukkit"),
]


def detect_mc_version(log_text: str) -> str:
    for pattern in MC_VERSION_PATTERNS:
        match = pattern.search(log_text or "")
        if match:
            return match.group("v")
    return ""


def detect_loader(log_text: str) -> str:
    for pattern, loader in LOADER_PATTERNS:
        if pattern.search(log_text or ""):
            return loader
    return ""


# Mappa class file version -> Java
CLASS_FILE_TO_JAVA = {
    52: "Java 8", 55: "Java 11", 60: "Java 16", 61: "Java 17",
    65: "Java 21", 66: "Java 22", 67: "Java 23", 68: "Java 24",
}


def class_version_to_java(major: str) -> str:
    try:
        value = int(float(major))
    except (TypeError, ValueError):
        return ""
    return CLASS_FILE_TO_JAVA.get(value, f"Java (class {value})")

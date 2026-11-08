"""Motore di analisi: log + controlli preventivi sui file installati."""
from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .crafty import Server
from .jarmeta import JarMeta, normalize_token, read_jar_meta
from .rules import (
    Rule,
    RULES,
    detect_loader,
    detect_mc_version,
    strip_colors,
)
from .versions import satisfies, spec_to_text

log = logging.getLogger("crafty_medic.analyzer")

BUILTIN_DEPENDENCIES = {
    "minecraft", "forge", "neoforge", "fml", "fabricloader", "fabric",
    "fabric-api",  # fornito da molte mod, ma spesso e' una vera dipendenza: lo trattiamo a parte
    "java", "jarjar",
}

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

# regole "contenitore": utili solo se non c'e' niente di piu' preciso
GENERIC_RULES = {
    "fabric_resolution_failed",
    "startup_failed_generic",
    "plugin_load_failed",
}

# a parita' di affidabilita' preferiamo l'azione meno distruttiva / piu' utile
REMEDY_PRIORITY = {"move": 0, "update": 1, "quarantine": 2, "delete": 3, "config": 4, "manual": 5}


# --------------------------------------------------------------------------- #
@dataclass
class FileRef:
    rel: str
    abs: str
    size: int
    sha1: str = ""
    meta: JarMeta | None = None

    @property
    def name(self) -> str:
        return os.path.basename(self.rel)

    @property
    def label(self) -> str:
        if self.meta and self.meta.name:
            return self.meta.name
        return self.name


@dataclass
class Action:
    id: str
    kind: str            # quarantine | delete | move | update | config | manual
    target: str          # path relativo del file, o chiave di configurazione
    param: str = ""      # es. cartella di destinazione per `move`
    label: str = ""
    detail: str = ""
    confidence: int = 70


@dataclass
class Finding:
    id: str
    rule_id: str
    title: str
    category: str
    severity: str
    confidence: int
    remedy: str
    advice: str
    evidence: list[str] = field(default_factory=list)
    line_no: int = 0
    targets: list[FileRef] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    source: str = "log"          # log | scan
    actions: list[Action] = field(default_factory=list)

    @property
    def severity_rank(self) -> int:
        return SEVERITY_ORDER.get(self.severity, 9)

    @property
    def primary_target(self) -> FileRef | None:
        return self.targets[0] if self.targets else None


@dataclass
class Analysis:
    server_id: str
    server_name: str
    mc_version: str = ""
    loader: str = ""
    running: bool = False
    log_lines: int = 0
    log_source: str = ""
    root: str = ""
    fs_access: bool = False
    findings: list[Finding] = field(default_factory=list)
    inventory: list[FileRef] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    scanned_at: str = ""

    @property
    def fixable(self) -> list[Finding]:
        return [f for f in self.findings if f.actions and any(a.kind in ("quarantine", "move", "delete") for a in f.actions)]

    @property
    def critical(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "critical"]

    def sorted_findings(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: (f.severity_rank, -f.confidence))


# --------------------------------------------------------------------------- #
class ServerContext:
    def __init__(self, server: Server, root: Path | None, log_text: str, log_source: str, mc_version: str, loader: str):
        self.server = server
        self.root = root
        self.log_text = log_text
        self.log_source = log_source
        self.mc_version = mc_version
        self.loader = loader
        self.jars: list[FileRef] = []


def resolve_server_root(server: Server, servers_dir: str, path_map: list[tuple[str, str]]) -> Path | None:
    """Trova la cartella del server sul filesystem locale."""
    candidates: list[Path] = []
    mapped = server.path
    for src, dst in path_map:
        if server.path and (server.path == src or server.path.startswith(src + "/")):
            mapped = dst + server.path[len(src):]
            break
    if mapped:
        candidates.append(Path(mapped))
    if servers_dir:
        candidates.append(Path(servers_dir) / server.server_id)
        candidates.append(Path(servers_dir) / server.name)
    for candidate in candidates:
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    return candidates[0] if candidates else None


def load_log_text(server: Server, root: Path | None, client, tail_bytes: int) -> tuple[str, str]:
    """Ritorna (testo_log, sorgente). Prova prima le API Crafty, poi il file."""
    if client is not None and client.configured:
        try:
            text = client.get_log(server.server_id)
            if text:
                if tail_bytes and len(text.encode("utf-8", errors="ignore")) > tail_bytes:
                    text = text.encode("utf-8", errors="ignore")[-tail_bytes:].decode("utf-8", errors="ignore")
                return text, "api"
        except Exception as exc:  # pragma: no cover
            log.debug("log via API non disponibile: %s", exc)
    if root:
        for relative in ("logs/latest.log", "logs/latest.log.gz", "logs/server.log"):
            path = root / relative
            if path.is_file():
                try:
                    if path.suffix == ".gz":
                        import gzip
                        text = gzip.open(path, "rt", errors="replace").read()
                    else:
                        data = path.read_bytes()
                        if tail_bytes and len(data) > tail_bytes:
                            data = data[-tail_bytes:]
                        text = data.decode("utf-8", errors="replace")
                    return text, str(path)
                except OSError as exc:
                    log.debug("lettura log fallita %s: %s", path, exc)
    return "", ""


def scan_jars(root: Path) -> list[FileRef]:
    """Elenca i jar in mods/ e plugins/ (e nella radice del server)."""
    refs: list[FileRef] = []
    if not root or not root.is_dir():
        return refs

    folders = ["mods", "plugins"]
    for folder in folders:
        base = root / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if not path.name.lower().endswith((".jar", ".jar.disabled", ".litemod")):
                continue
            rel = path.relative_to(root).as_posix()
            meta = read_jar_meta(path, rel) if path.suffix.lower() == ".jar" else None
            refs.append(FileRef(
                rel=rel,
                abs=str(path),
                size=path.stat().st_size,
                sha1=meta.sha1 if meta else _sha1(path),
                meta=meta,
            ))
    return refs


def _sha1(path: Path) -> str:
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except OSError:
        return ""


# --------------------------------------------------------------------------- #
#  risoluzione dei token (id/nome mod) -> file
# --------------------------------------------------------------------------- #
def resolve_token(token: str, jars: list[FileRef]) -> list[FileRef]:
    """Trova i jar che corrispondono a un token estratto dal log."""
    if not token or not jars:
        return []
    needle = normalize_token(token)
    needle_raw = strip_colors(token).strip().strip("\"'").lower()
    if not needle and not needle_raw:
        return []

    scored: list[tuple[int, FileRef]] = []
    for ref in jars:
        meta = ref.meta
        candidates = [
            normalize_token(ref.name),
            normalize_token(meta.name) if meta else "",
            normalize_token(meta.mod_id) if meta else "",
        ]
        raw_candidates = [
            ref.name.lower(),
            (meta.name or "").lower(),
            (meta.mod_id or "").lower(),
        ]
        if "plugins/" in token.lower() or "mods/" in token.lower():
            raw_candidates.append(ref.rel.lower())
            candidates.append(normalize_token(ref.rel))

        score = 0
        for value in candidates:
            if value and value == needle and value:
                score = max(score, 100)
        for value in raw_candidates:
            if value and value == needle_raw:
                score = max(score, 100)
        if score < 100:
            for value in candidates:
                if needle and value and (value.startswith(needle) or needle.startswith(value)) and len(value) >= 4:
                    score = max(score, 85)
            for value in raw_candidates:
                if needle_raw and value and (value.startswith(needle_raw) or needle_raw.startswith(value)) and len(value) >= 4:
                    score = max(score, 85)
        if score < 85:
            for value in candidates:
                if needle and value and (needle in value or value in needle) and min(len(value), len(needle)) >= 5:
                    score = max(score, 65)
        if score:
            scored.append((score, ref))
    scored.sort(key=lambda item: -item[0])
    if not scored:
        return []
    best = scored[0][0]
    return [ref for score, ref in scored if score == best][:2]


# --------------------------------------------------------------------------- #
class Analyzer:
    def __init__(self, cfg):
        self.cfg = cfg

    # ------------------------------------------------------------------ #
    def build_context(self, server: Server, client=None) -> ServerContext:
        root = resolve_server_root(server, self.cfg.servers_dir, self.cfg.path_map)
        log_text, log_source = load_log_text(server, root, client, self.cfg.log_tail_bytes)
        mc_version = server.mc_version or detect_mc_version(log_text)
        loader = detect_loader(log_text)
        ctx = ServerContext(server, root, log_text, log_source, mc_version, loader)
        if root and root.is_dir():
            ctx.jars = scan_jars(root)
            ctx.loader = loader or self._guess_loader_from_files(ctx.jars, root)
        return ctx

    @staticmethod
    def _guess_loader_from_files(jars: list[FileRef], root: Path) -> str:
        loaders: dict[str, int] = {}
        for ref in jars:
            if ref.meta and ref.meta.loader not in ("unknown", ""):
                loaders[ref.meta.loader] = loaders.get(ref.meta.loader, 0) + 1
        if not loaders:
            for name in os.listdir(root) if (root is not None and root.is_dir()) else []:
                lowered = name.lower()
                if "fabric" in lowered:
                    return "fabric"
                if "forge" in lowered:
                    return "forge"
                if "paper" in lowered:
                    return "paper"
            return ""
        return max(loaders.items(), key=lambda kv: kv[1])[0]

    # ------------------------------------------------------------------ #
    def analyze(self, ctx: ServerContext) -> Analysis:
        analysis = Analysis(
            server_id=ctx.server.server_id,
            server_name=ctx.server.name,
            mc_version=ctx.mc_version,
            loader=ctx.loader,
            running=ctx.server.running,
            log_lines=len(ctx.log_text.splitlines()) if ctx.log_text else 0,
            log_source=ctx.log_source,
            root=str(ctx.root) if ctx.root else "",
            fs_access=bool(ctx.root and ctx.root.is_dir()),
            inventory=ctx.jars,
        )
        findings: list[Finding] = []
        if ctx.log_text:
            findings += self._analyze_log(ctx)
        if analysis.fs_access:
            findings += self._preflight_scan(ctx)
        findings = self._refine(findings, ctx)
        findings = self._dedupe(findings)
        analysis.findings = sorted(findings, key=lambda f: (f.severity_rank, -f.confidence))
        return analysis

    # ------------------------------------------------------------------ #
    @staticmethod
    def _refine(findings: list[Finding], ctx: ServerContext) -> list[Finding]:
        """Aggiusta i casi in cui il messaggio del log e' ambiguo."""
        specific = [f for f in findings
                    if f.rule_id not in GENERIC_RULES and f.severity in ("critical", "high")]

        refined: list[Finding] = []
        for finding in findings:
            # Forge: "Requested by" con dipendenza 'minecraft' => versione MC sbagliata
            if finding.rule_id == "forge_missing_dependency" and "minecraft" in [u.lower() for u in finding.unmatched]:
                finding.title = "Mod per un'altra versione di Minecraft"
                finding.remedy = "update"
                finding.advice = ("Il mod indicato richiede una versione di Minecraft diversa da quella del server "
                                  f"({ctx.mc_version}). Aggiornalo a una release compatibile oppure rimuovilo.")
                finding.actions = Analyzer._build_actions(finding, ctx)
            # le regole generiche servono solo se non c'e' di meglio
            if finding.rule_id in GENERIC_RULES and specific:
                continue
            refined.append(finding)
        return refined

    # ------------------------------------------------------------------ #
    def _analyze_log(self, ctx: ServerContext) -> list[Finding]:
        raw_lines = ctx.log_text.splitlines()
        lines = [strip_colors(line) for line in raw_lines]
        findings: list[Finding] = []

        for rule in RULES:
            for idx, line in enumerate(lines):
                match = rule.compiled.search(line)
                if not match:
                    continue
                if rule.when and not rule.when(match):
                    continue
                findings.append(self._finding_from_match(rule, match, lines, idx, ctx))

        return [f for f in findings if f is not None]

    def _finding_from_match(self, rule: Rule, match: re.Match, lines: list[str], idx: int, ctx: ServerContext) -> Finding | None:
        start = max(0, idx - rule.context)
        end = min(len(lines), idx + rule.context + 1)
        evidence = [line.rstrip() for line in lines[start:end]]

        tokens: list[str] = []
        if rule.extract:
            try:
                tokens = [t for t in rule.extract(match, lines, idx) if t]
            except Exception as exc:  # pragma: no cover
                log.debug("extract fallita per %s: %s", rule.id, exc)

        targets: list[FileRef] = []
        unmatched: list[str] = []
        seen: set[str] = set()
        for token in tokens:
            resolved = resolve_token(token, ctx.jars)
            if not resolved:
                unmatched.append(token)
                continue
            for ref in resolved:
                if ref.rel in seen:
                    continue
                seen.add(ref.rel)
                targets.append(ref)

        detail = match.group(0).strip()
        finding = Finding(
            id=uuid.uuid4().hex[:12],
            rule_id=rule.id,
            title=rule.title,
            category=rule.category,
            severity=rule.severity,
            confidence=rule.confidence if targets or not tokens else max(20, rule.confidence - 25),
            remedy=rule.remedy,
            advice=rule.advice,
            evidence=evidence,
            line_no=idx + 1,
            targets=targets,
            unmatched=unmatched,
            source="log",
        )
        finding.actions = self._build_actions(finding, ctx)
        return finding

    # ------------------------------------------------------------------ #
    def _preflight_scan(self, ctx: ServerContext) -> list[Finding]:
        """Controlli che non dipendono dal log: si possono fare anche a server spento."""
        findings: list[Finding] = []
        jars = ctx.jars
        if not jars:
            return findings

        by_id: dict[str, list[FileRef]] = {}
        provided: set[str] = set()
        for ref in jars:
            meta = ref.meta
            if not meta:
                continue
            if meta.mod_id:
                by_id.setdefault(meta.mod_id.lower(), []).append(ref)
                provided.add(meta.mod_id.lower())
            if meta.name:
                provided.add(normalize_token(meta.name))

        for ref in jars:
            meta = ref.meta
            if meta is None:
                findings.append(self._finding(
                    rule_id="scan_unreadable_jar",
                    title="Jar non leggibile",
                    category="mod",
                    severity="high",
                    confidence=80,
                    remedy="quarantine",
                    advice="Non sono riuscito a leggere questo jar: potrebbe essere corrotto o non essere un jar valido.",
                    evidence=[ref.rel],
                    targets=[ref],
                    ctx=ctx,
                ))
                continue

            if not meta.valid:
                findings.append(self._finding(
                    rule_id="scan_corrupt_jar",
                    title="Jar corrotto o non valido",
                    category=ref.rel.split("/")[0] if "/" in ref.rel else "mod",
                    severity="high",
                    confidence=95,
                    remedy="quarantine",
                    advice=f"Il file non e' un jar valido ({meta.error}). Riscaricalo oppure rimuovilo.",
                    evidence=[ref.rel, meta.error],
                    targets=[ref],
                    ctx=ctx,
                ))
                continue

            if meta.loader == "unknown":
                continue

            # 1) mod/plugin nella cartella sbagliata
            wrong_folder = self._wrong_folder_check(ref, ctx)
            if wrong_folder:
                findings.append(wrong_folder)
                continue

            # 2) loader diverso da quello del server
            loader_finding = self._loader_check(ref, ctx)
            if loader_finding:
                findings.append(loader_finding)
                continue

            # 3) versione di Minecraft non compatibile
            if meta.mc_spec and ctx.mc_version and meta.loader != "bukkit" and meta.loader != "paper":
                ok = satisfies(ctx.mc_version, meta.mc_spec)
                if ok is False:
                    findings.append(self._finding(
                        rule_id="scan_mc_version_mismatch",
                        title="Versione di Minecraft non compatibile",
                        category="mod" if meta.loader in ("fabric", "forge", "neoforge") else "plugin",
                        severity="critical",
                        confidence=90,
                        remedy="update",
                        advice=f"'{meta.label}' dichiara Minecraft {spec_to_text(meta.mc_spec)} ma il server e' su {ctx.mc_version}. Aggiorna il mod/plugin o rimuovilo.",
                        evidence=[f"{ref.rel} -> {meta.label} {meta.version} | minecraft {meta.mc_spec}"],
                        targets=[ref],
                        ctx=ctx,
                    ))
                    continue

            # 4) api-version di un plugin piu' nuova del server
            if meta.api_version and ctx.mc_version and meta.loader in ("bukkit", "paper"):
                diff = _version_tuple(meta.api_version) > _version_tuple(ctx.mc_version)
                if diff:
                    findings.append(self._finding(
                        rule_id="scan_plugin_api_newer",
                        title="Plugin piu' nuovo del server",
                        category="plugin",
                        severity="medium",
                        confidence=65,
                        remedy="update",
                        advice=f"Il plugin dichiara api-version {meta.api_version} ma il server e' {ctx.mc_version}: potrebbe non caricarsi.",
                        evidence=[f"{ref.rel} -> api-version {meta.api_version}"],
                        targets=[ref],
                        ctx=ctx,
                    ))
                    continue

            # 5) mod solo client
            if meta.environment == "client" and ctx.loader in ("fabric", "forge", "neoforge"):
                findings.append(self._finding(
                    rule_id="scan_client_only_mod",
                    title="Mod solo client in un server",
                    category="mod",
                    severity="high",
                    confidence=80,
                    remedy="quarantine",
                    advice=f"'{meta.label}' e' un mod che gira solo sul client: va tolto dal server.",
                    evidence=[f"{ref.rel} -> environment: client"],
                    targets=[ref],
                    ctx=ctx,
                ))
                continue

            # 6) dipendenze non soddisfatte
            missing = self._missing_dependencies(meta, provided, by_id)
            if missing:
                findings.append(self._finding(
                    rule_id="scan_missing_dependency",
                    title="Dipendenza non installata",
                    category="mod",
                    severity="critical",
                    confidence=80,
                    remedy="quarantine",
                    advice=f"'{meta.label}' richiede {', '.join(missing)} che non risulta installato. Rimuovi il mod oppure installa la dipendenza.",
                    evidence=[f"{ref.rel} -> dipende da: {', '.join(missing)}"],
                    targets=[ref],
                    ctx=ctx,
                ))
                continue

            # 7) mod annidato in una sottocartella (non viene caricato)
            parts = ref.rel.split("/")
            if len(parts) > 2 and parts[0] in ("mods", "plugins"):
                findings.append(self._finding(
                    rule_id="scan_nested_jar",
                    title="Jar in una sottocartella",
                    category="mod" if parts[0] == "mods" else "plugin",
                    severity="medium",
                    confidence=85,
                    remedy="move",
                    advice=f"Minecraft carica solo i jar direttamente in {parts[0]}/, non nelle sottocartelle.",
                    evidence=[ref.rel],
                    targets=[ref],
                    ctx=ctx,
                    param=parts[0],
                ))
                continue

        # 8) duplicati (stesso mod id)
        for mod_id, refs in by_id.items():
            if len(refs) > 1:
                keep = max(refs, key=lambda r: (r.size, r.rel))
                findings.append(self._finding(
                    rule_id="scan_duplicate_mod",
                    title="Stesso mod installato due volte",
                    category="mod",
                    severity="high",
                    confidence=85,
                    remedy="quarantine",
                    advice=f"Ci sono {len(refs)} copie di '{refs[0].meta.label if refs[0].meta else mod_id}'. Ne basta una.",
                    evidence=[r.rel for r in refs],
                    targets=[r for r in refs if r is not keep],
                    ctx=ctx,
                ))
        return findings

    # ------------------------------------------------------------------ #
    @staticmethod
    def _missing_dependencies(meta, provided: set[str], by_id: dict[str, list[FileRef]]) -> list[str]:
        missing: list[str] = []
        for dep_id in meta.depends:
            key = dep_id.lower()
            if key in BUILTIN_DEPENDENCIES and key != "fabric-api":
                continue
            if key in provided or normalize_token(dep_id) in provided:
                continue
            if key in {k.lower() for k in by_id}:
                continue
            if key == "fabric-api" and any("fabric-api" in k for k in provided):
                continue
            missing.append(dep_id)
        return missing

    @staticmethod
    def _wrong_folder_check(ref: FileRef, ctx: ServerContext) -> Finding | None:
        meta = ref.meta
        folder = ref.rel.split("/")[0]
        loader = ctx.loader
        if folder == "mods" and meta.loader in ("bukkit", "paper"):
            return Analyzer._finding(
                rule_id="scan_plugin_in_mods",
                title="Plugin nella cartella dei mod",
                category="plugin",
                severity="high",
                confidence=90,
                remedy="move",
                advice="Questo e' un plugin Bukkit/Paper: va messo in plugins/, non in mods/.",
                evidence=[f"{ref.rel} -> {meta.label} ({meta.loader})"],
                targets=[ref],
                ctx=ctx,
                param="plugins",
            )
        if folder == "plugins" and meta.loader in ("fabric", "forge", "neoforge"):
            return Analyzer._finding(
                rule_id="scan_mod_in_plugins",
                title="Mod nella cartella dei plugin",
                category="mod",
                severity="high",
                confidence=90,
                remedy="move",
                advice="Questo e' un mod Forge/Fabric: va messo in mods/, non in plugins/.",
                evidence=[f"{ref.rel} -> {meta.label} ({meta.loader})"],
                targets=[ref],
                ctx=ctx,
                param="mods",
            )
        return None

    @staticmethod
    def _loader_check(ref: FileRef, ctx: ServerContext) -> Finding | None:
        meta = ref.meta
        loader = ctx.loader
        if not loader or meta.loader == loader:
            return None
        compatible_pairs = {
            ("forge", "neoforge"), ("neoforge", "forge"),
            ("bukkit", "paper"), ("paper", "bukkit"),
            ("fabric", "quilt"), ("quilt", "fabric"),
        }
        if (meta.loader, loader) in compatible_pairs:
            return None
        folder = ref.rel.split("/")[0]
        if folder == "plugins" and loader in ("fabric", "forge", "neoforge"):
            return None  # gestito dal controllo cartella sbagliata
        return Analyzer._finding(
            rule_id="scan_loader_mismatch",
            title="Mod/plugin per un loader diverso",
            category="mod" if meta.loader in ("fabric", "forge", "neoforge") else "plugin",
            severity="high",
            confidence=70,
            remedy="quarantine",
            advice=f"'{meta.label}' e' per {meta.loader} ma il server usa {loader}.",
            evidence=[f"{ref.rel} -> loader: {meta.loader} (server: {loader})"],
            targets=[ref],
            ctx=ctx,
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _finding(rule_id: str, title: str, category: str, severity: str, confidence: int,
                 remedy: str, advice: str, evidence: list[str], targets: list[FileRef],
                 ctx: ServerContext, param: str = "") -> Finding:
        finding = Finding(
            id=uuid.uuid4().hex[:12],
            rule_id=rule_id,
            title=title,
            category=category,
            severity=severity,
            confidence=confidence,
            remedy=remedy,
            advice=advice,
            evidence=evidence,
            targets=targets,
            source="scan",
        )
        finding.actions = Analyzer._build_actions(finding, ctx, param_override=param)
        return finding

    @staticmethod
    def _build_actions(finding: Finding, ctx: ServerContext, param_override: str = "") -> list[Action]:
        actions: list[Action] = []
        if not finding.targets:
            return actions

        if finding.remedy == "move":
            for ref in finding.targets:
                destination = param_override or ("plugins" if (ref.meta and ref.meta.loader in ("bukkit", "paper")) else "mods")
                actions.append(Action(
                    id=uuid.uuid4().hex[:12],
                    kind="move",
                    target=ref.rel,
                    param=destination,
                    label=f"Sposta {ref.name} in {destination}/",
                    detail=finding.advice,
                    confidence=finding.confidence,
                ))
            return actions

        if finding.remedy == "update":
            for ref in finding.targets:
                actions.append(Action(
                    id=uuid.uuid4().hex[:12],
                    kind="update",
                    target=ref.rel,
                    label=f"Aggiorna {ref.label}",
                    detail="Cerca su Modrinth una versione compatibile con questo server e sostituisci il jar.",
                    confidence=finding.confidence,
                ))
                actions.append(Action(
                    id=uuid.uuid4().hex[:12],
                    kind="quarantine",
                    target=ref.rel,
                    label=f"Metti in quarantena {ref.name}",
                    detail="Sposta il file in quarantena (il server riparte senza).",
                    confidence=finding.confidence,
                ))
            return actions

        if finding.remedy == "quarantine":
            for ref in finding.targets:
                actions.append(Action(
                    id=uuid.uuid4().hex[:12],
                    kind="quarantine",
                    target=ref.rel,
                    label=f"Metti in quarantena {ref.name}",
                    detail=finding.advice,
                    confidence=finding.confidence,
                ))
            return actions

        return actions

    # ------------------------------------------------------------------ #
    @staticmethod
    def _dedupe(findings: list[Finding]) -> list[Finding]:
        """Un solo problema (e una sola azione) per file: vince il piu' affidabile."""
        ordered = sorted(
            findings,
            key=lambda f: (-f.confidence, REMEDY_PRIORITY.get(f.remedy, 9), f.severity_rank),
        )
        claimed: set[str] = set()
        output: list[Finding] = []
        for finding in ordered:
            if not finding.targets:
                key = ("", finding.rule_id)
                if key in claimed:  # stessa regola senza file: una volta sola
                    continue
                claimed.add(key)
                output.append(finding)
                continue

            free_targets = [t for t in finding.targets if t.rel not in claimed]
            if not free_targets:
                continue
            if len(free_targets) != len(finding.targets):
                finding.targets = free_targets
                finding.actions = [
                    a for a in finding.actions
                    if a.target in {t.rel for t in free_targets}
                ]
                if not finding.actions and finding.remedy in ("quarantine", "update", "move", "delete"):
                    continue
            for target in free_targets:
                claimed.add(target.rel)
            output.append(finding)
        return output


def _version_tuple(value: str) -> tuple[int, ...]:
    parts = []
    for chunk in (value or "").split("."):
        if chunk.isdigit():
            parts.append(int(chunk))
        else:
            break
    return tuple(parts)

"""Ricerca e download di aggiornamenti da Modrinth (nessuna API key richiesta)."""
from __future__ import annotations

import difflib
import json
import logging
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import requests

from .jarmeta import JarMeta, normalize_token

log = logging.getLogger("crafty_medic.updater")

PROJECT_TYPE_BY_LOADER = {
    "fabric": "mod", "quilt": "mod", "forge": "mod", "neoforge": "mod", "liteloader": "mod",
    "bukkit": "plugin", "spigot": "plugin", "paper": "plugin", "purpur": "plugin", "folia": "plugin",
}

MODRINTH_LOADERS = {
    "fabric": ["fabric", "quilt"],
    "quilt": ["quilt", "fabric"],
    "forge": ["forge"],
    "neoforge": ["neoforge", "forge"],
    "bukkit": ["bukkit", "spigot", "paper", "purpur"],
    "paper": ["paper", "purpur", "bukkit", "spigot"],
    "spigot": ["spigot", "bukkit", "paper"],
}


@dataclass
class Candidate:
    project_id: str
    slug: str
    title: str
    description: str = ""
    project_type: str = ""
    downloads: int = 0
    score: float = 0.0


@dataclass
class UpdateResult:
    ok: bool
    message: str = ""
    path: Path | None = None
    version_name: str = ""
    project: str = ""
    url: str = ""
    candidates: list[Candidate] = None  # type: ignore[assignment]


class Updater:
    def __init__(self, base_url: str = "https://api.modrinth.com/v2", user_agent: str = "CraftyMedic/1.0",
                 timeout: int = 25, min_score: int = 55, mapping: dict | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.min_score = min_score
        self.mapping = mapping or {}
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "application/json",
        })

    # ------------------------------------------------------------------ #
    def _get(self, path: str, **params) -> object:
        url = f"{self.base_url}{path}"
        response = self.session.get(url, params=params, timeout=self.timeout)
        if response.status_code >= 400:
            raise RuntimeError(f"Modrinth ha risposto {response.status_code}")
        return response.json()

    # ------------------------------------------------------------------ #
    @staticmethod
    def guess_slug(meta: JarMeta) -> str:
        """Prova a ricavare lo slug Modrinth dai metadati del jar."""
        for value in (getattr(meta, "modrinth_slug", ""), getattr(meta, "homepage", ""), getattr(meta, "sources", "")):
            if not value:
                continue
            match = re.search(r"modrinth\.com/(?:mod|plugin|datapack)/([A-Za-z0-9_\-]+)", str(value))
            if match:
                return match.group(1)
        return ""

    def search(self, query: str, loader: str, mc_version: str) -> list[Candidate]:
        project_type = PROJECT_TYPE_BY_LOADER.get(loader, "mod")
        facets = [["project_type:" + project_type]]
        if loader:
            facets.append(["categories:" + loader])
        if mc_version:
            facets.append(["versions:" + mc_version])
        params = {
            "query": query,
            "limit": 8,
            "index": "relevance",
            "facets": json.dumps(facets),
        }
        try:
            payload = self._get("/search", **params)
        except Exception as exc:
            log.warning("ricerca Modrinth fallita: %s", exc)
            raise
        hits = payload.get("hits", []) if isinstance(payload, dict) else []
        return [
            Candidate(
                project_id=hit.get("project_id") or hit.get("slug", ""),
                slug=hit.get("slug", ""),
                title=hit.get("title", ""),
                description=(hit.get("description") or "")[:180],
                project_type=hit.get("project_type", ""),
                downloads=hit.get("downloads", 0) or 0,
            )
            for hit in hits
        ]

    # ------------------------------------------------------------------ #
    @staticmethod
    def _score(query: str, candidate: Candidate) -> float:
        left = normalize_token(query)
        right = normalize_token(candidate.title)
        slug = normalize_token(candidate.slug)
        if not left:
            return 0.0
        ratio = difflib.SequenceMatcher(None, left, right).ratio() * 100
        ratio_slug = difflib.SequenceMatcher(None, left, slug).ratio() * 100
        score = max(ratio, ratio_slug)
        if left == right or left == slug:
            score = 100.0
        elif right and (left in right or right in left):
            score = max(score, 88.0)
        return score

    def find_candidates(self, meta: JarMeta, loader: str, mc_version: str) -> list[Candidate]:
        query = meta.name or meta.mod_id or Path(meta.rel).name
        key = (meta.mod_id or "").strip().lower()
        forced = ""
        if key and key in self.mapping:
            forced = self.mapping[key]
        elif Path(meta.rel).name.lower() in self.mapping:
            forced = self.mapping[Path(meta.rel).name.lower()]

        if forced:
            try:
                project = self._get(f"/project/{forced}")
                return [Candidate(
                    project_id=project.get("id", forced),
                    slug=project.get("slug", forced),
                    title=project.get("title", forced),
                    description=(project.get("description") or "")[:180],
                    project_type=project.get("project_type", ""),
                    downloads=project.get("downloads", 0) or 0,
                    score=100.0,
                )]
            except Exception as exc:
                log.warning("progetto Modrinth '%s' non trovato: %s", forced, exc)

        guessed = self.guess_slug(meta)
        if guessed:
            try:
                project = self._get(f"/project/{guessed}")
                return [Candidate(
                    project_id=project.get("id", guessed),
                    slug=project.get("slug", guessed),
                    title=project.get("title", guessed),
                    description=(project.get("description") or "")[:180],
                    project_type=project.get("project_type", ""),
                    downloads=project.get("downloads", 0) or 0,
                    score=95.0,
                )]
            except Exception:
                pass

        try:
            hits = self.search(query, loader, mc_version)
        except Exception as exc:
            raise RuntimeError(f"Impossibile interrogare Modrinth: {exc}")
        for hit in hits:
            hit.score = self._score(query, hit)
        hits.sort(key=lambda c: (-c.score, -c.downloads))
        return hits

    # ------------------------------------------------------------------ #
    def versions(self, project_id: str, loader: str, mc_version: str) -> list[dict]:
        loaders = MODRINTH_LOADERS.get(loader, [loader] if loader else [])
        params: dict = {}
        if loaders:
            params["loaders"] = json.dumps(loaders)
        if mc_version:
            params["game_versions"] = json.dumps([mc_version])
        payload = self._get(f"/project/{project_id}/version", **params)
        return payload if isinstance(payload, list) else []

    def best_version(self, versions: list[dict], loader: str, mc_version: str) -> dict | None:
        if not versions:
            return None
        wanted = set(MODRINTH_LOADERS.get(loader, [loader]))

        def usable(version: dict) -> bool:
            loaders = {str(x).lower() for x in version.get("loaders", [])}
            if wanted and not (loaders & wanted):
                return False
            if mc_version and mc_version not in [str(v) for v in version.get("game_versions", [])]:
                return False
            return any(f.get("primary") or True for f in version.get("files", []))

        candidates = [v for v in versions if usable(v)]
        if not candidates:
            # ripiego: almeno la versione di Minecraft giusta
            candidates = [v for v in versions if not mc_version or mc_version in [str(x) for x in v.get("game_versions", [])]]
        if not candidates:
            return None
        candidates.sort(key=lambda v: v.get("date_published", ""), reverse=True)
        return candidates[0]

    @staticmethod
    def primary_file(version: dict) -> dict | None:
        files = version.get("files") or []
        for file in files:
            if file.get("primary"):
                return file
        return files[0] if files else None

    def download(self, version: dict, destination: Path) -> Path:
        file = self.primary_file(version)
        if not file:
            raise RuntimeError("nessun file scaricabile in questa versione")
        url = file.get("url")
        if not url:
            raise RuntimeError("URL del file mancante")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.session.get(url, stream=True, timeout=self.timeout * 4) as response:
            response.raise_for_status()
            with open(destination, "wb") as handle:
                for chunk in response.iter_content(chunk_size=65536):
                    if chunk:
                        handle.write(chunk)
        return destination

    # ------------------------------------------------------------------ #
    def find_update(self, meta: JarMeta, loader: str, mc_version: str) -> UpdateResult:
        """Ritorna la migliore versione disponibile, o i candidati se l'abbinamento non e' sicuro."""
        if not meta:
            return UpdateResult(False, "metadati del jar non disponibili")
        try:
            candidates = self.find_candidates(meta, loader, mc_version)
        except Exception as exc:
            return UpdateResult(False, str(exc))
        if not candidates:
            return UpdateResult(False, "nessun progetto trovato su Modrinth per questo nome")

        best = candidates[0]
        if best.score < self.min_score:
            return UpdateResult(False, "abbinamento non sicuro: scegli il progetto giusto", candidates=candidates)

        try:
            versions = self.versions(best.project_id, loader, mc_version)
        except Exception as exc:
            return UpdateResult(False, f"impossibile elencare le versioni: {exc}", candidates=candidates)
        version = self.best_version(versions, loader, mc_version)
        if not version:
            return UpdateResult(False, "nessuna versione compatibile con questo server", candidates=candidates)

        file = self.primary_file(version)
        result = UpdateResult(
            ok=True,
            message="trovato",
            version_name=version.get("version_number") or version.get("name", ""),
            project=f"{best.title} ({best.slug})",
            url=f"https://modrinth.com/{'plugin' if best.project_type == 'plugin' else 'mod'}/{best.slug}",
            candidates=candidates,
        )
        result.path = None
        result.__dict__["_version"] = version
        result.__dict__["_file"] = file
        return result

    def download_update(self, result: UpdateResult, destination: Path) -> Path:
        version = result.__dict__.get("_version")
        if not version:
            raise RuntimeError("nessuna versione selezionata")
        return self.download(version, destination)

    @staticmethod
    def encode_facets(query: str, loader: str, mc_version: str) -> str:
        return urllib.parse.quote(query)

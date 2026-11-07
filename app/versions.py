"""Utility per confrontare versioni e version-range di Minecraft.

Supporta i formati piu' comuni trovati nei metadati dei mod:
  * "1.20.1" / "1.20"                -> corrispondenza esatta
  * "1.20.x" / "1.20.*"              -> intervallo 1.20.0 <= v < 1.21.0
  * ">=1.20" ">1.20" "<=1.21" "<1.21"
  * "~1.20.1"                        -> >=1.20.1 <1.21.0
  * "^1.20.1"                        -> >=1.20.1 <2.0.0
  * "[1.20,1.21)" "(,1.21]" "[1.20,)" -> range maven (Forge)
  * "*" / ""                         -> qualsiasi versione
  * "1.20.1, 1.21"                   -> piu' vincoli separati da virgola
"""
from __future__ import annotations

import re

_RANGE_RE = re.compile(r"^([\[\(])\s*([^,]*)\s*,\s*([^\]\)]*)\s*([\]\)])$")
_WILDCARD_RE = re.compile(r"^(\d+(?:\.\d+)*)\.[xX*]$")


def parse_version(value: str) -> tuple[int, ...]:
    """'1.20.1' -> (1, 20, 1);  '1.20.1-rc1' -> (1, 20, 1)."""
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
    """-1/0/1 confrontando due versioni (padding con zeri)."""
    left, right = parse_version(a), parse_version(b)
    if not left or not right:
        return 0
    size = max(len(left), len(right))
    left = left + (0,) * (size - len(left))
    right = right + (0,) * (size - len(right))
    return (left > right) - (left < right)


def _bump(major_minor: tuple[int, ...]) -> tuple[int, ...]:
    """1.20 -> 1.21.0 ; 1.20.1 -> 1.21.0 ; 1 -> 2.0.0"""
    if not major_minor:
        return ()
    digits = list(major_minor[:2]) + [0] * (2 - len(major_minor[:2]))
    digits[-1] += 1
    while len(digits) < 3:
        digits.append(0)
    return tuple(digits)


def _fmt(digits: tuple[int, ...]) -> str:
    return ".".join(str(d) for d in digits)


def _next_major(version: str) -> str:
    digits = parse_version(version)
    if not digits:
        return ""
    return str(digits[0] + 1) + ".0.0"


Constraints = list[tuple[str, str]]  # (operatore, versione)


def _split_constraints(spec: str) -> list[str]:
    """Divide sui comma che sono fuori dalle parentesi (es. range maven)."""
    chunks: list[str] = []
    depth = 0
    current = ""
    for char in spec:
        if char in "[(":
            depth += 1
        elif char in "])":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            chunks.append(current)
            current = ""
        else:
            current += char
    chunks.append(current)
    return [chunk.strip() for chunk in chunks]


def parse_spec(spec: str) -> Constraints:
    """Converte una stringa di range in una lista di vincoli (op, versione)."""
    if spec is None:
        return []
    spec = spec.strip()
    if not spec or spec in {"*", "any"}:
        return []

    constraints: Constraints = []
    for chunk in _split_constraints(spec):
        if not chunk:
            continue
        range_match = _RANGE_RE.match(chunk)
        if range_match:
            open_br, low, high, close_br = range_match.groups()
            if low.strip():
                constraints.append((">=" if open_br == "[" else ">", low.strip()))
            if high.strip():
                constraints.append(("<=" if close_br == "]" else "<", high.strip()))
            continue
        if chunk[0] in "[(" and chunk[-1] in "])":
            # range con un solo valore: "[1.19.4]" equivale a "==1.19.4"
            inner = chunk[1:-1].strip()
            if inner:
                constraints.append(("==", inner))
            continue
        for prefix in (">=", "<=", "==", "!=", "~", "^", ">", "<", "="):
            if chunk.startswith(prefix):
                rest = chunk[len(prefix):].strip()
                if prefix == "~":
                    digits = parse_version(rest)
                    constraints.append((">=", rest))
                    constraints.append(("<", _fmt(_bump(digits)) if digits else ""))
                elif prefix == "^":
                    constraints.append((">=", rest))
                    constraints.append(("<", _next_major(rest)))
                else:
                    constraints.append(("==" if prefix == "=" else prefix, rest))
                break
        else:
            wildcard = _WILDCARD_RE.match(chunk)
            if wildcard:
                low = _fmt(parse_version(wildcard.group(1)))
                constraints.append((">=", low))
                constraints.append(("<", _fmt(_bump(parse_version(wildcard.group(1))))))
            else:
                constraints.append(("==", chunk))
    return [c for c in constraints if c[1]]


def satisfies(version: str, spec: str) -> bool | None:
    """True/False, oppure None se non si riesce a valutare il range."""
    if not version:
        return None
    constraints = parse_spec(spec or "")
    if not constraints:
        return True
    unknown = False
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
        if diff == 0 and not parse_version(expected):
            unknown = True
    return None if unknown else True


def spec_to_text(spec: str) -> str:
    """Rende leggibile in italiano un range."""
    spec = (spec or "").strip()
    if not spec or spec == "*":
        return "qualsiasi versione"
    return spec

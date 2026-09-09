"""Flexible name matching for Asana sections and status-like field values."""

from __future__ import annotations

import re


def normalize_column_key(name: str) -> str:
    """Collapse a column or status name to lowercase alphanumeric for comparison."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").casefold())


def parse_section_names(raw: str | None) -> list[str]:
    """Split a comma-separated section or status value list."""
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def match_asana_section(status_name, sections):
    """Map a status name to an Asana section via normalized / fuzzy match."""
    if not status_name:
        return None
    key = normalize_column_key(status_name)
    if not key:
        return None

    indexed = []
    for section in sections:
        name = section.get("name") or ""
        indexed.append((normalize_column_key(name), section))

    for sk, section in indexed:
        if sk == key:
            return section
    for sk, section in indexed:
        if key in sk or sk in key:
            return section
    return None


def match_named_value(actual: str | None, wanted_names: list[str]) -> bool:
    """True if actual matches any wanted name via the same rules as sections."""
    if not actual or not wanted_names:
        return False
    actual_key = normalize_column_key(actual)
    if not actual_key:
        return False
    wanted_keys = [normalize_column_key(name) for name in wanted_names]
    if actual_key in wanted_keys:
        return True
    return any(actual_key in wanted or wanted in actual_key for wanted in wanted_keys if wanted)


def resolve_sections(wanted_names: list[str], sections: list[dict]) -> tuple[list[dict], list[str]]:
    """Resolve wanted names to sections. Returns (resolved, missing names)."""
    resolved: list[dict] = []
    missing: list[str] = []
    seen: set[str] = set()
    for name in wanted_names:
        section = match_asana_section(name, sections)
        if section is None:
            missing.append(name)
            continue
        gid = section.get("gid")
        if gid and gid not in seen:
            seen.add(gid)
            resolved.append(section)
    return resolved, missing

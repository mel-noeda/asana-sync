"""Strongly typed accessors for process environment / .env."""

from __future__ import annotations

import logging
import os
import sys
from functools import cached_property

from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name, default)
    if val == "":
        return default
    return val


def _require(name: str) -> str:
    val = _get(name)
    if not val:
        sys.exit(f"Missing required environment variable: {name}")
    return val


def _bool(name: str, default: bool = False) -> bool:
    raw = _get(name)
    if raw is None:
        return default
    return raw.lower() == "true"


def _int(name: str) -> int | None:
    raw = _get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        sys.exit(f"{name} must be an integer, got: {raw!r}")


class Config:
    """Typed wrappers around the environment variables this project uses."""

    @cached_property
    def asana_token(self) -> str:
        return _require("ASANA_TOKEN")

    @cached_property
    def asana_project_gid(self) -> str:
        return _require("ASANA_PROJECT_GID")

    @cached_property
    def github_token(self) -> str:
        return _require("GITHUB_TOKEN")

    @cached_property
    def github_repo(self) -> str | None:
        repo = _get("GITHUB_REPO")
        if repo and "/" not in repo:
            sys.exit(f'GITHUB_REPO must be "owner/repo", got: {repo!r}')
        return repo

    @cached_property
    def github_project_owner(self) -> str | None:
        return _get("GITHUB_PROJECT_OWNER")

    @cached_property
    def github_project_number(self) -> int | None:
        return _int("GITHUB_PROJECT_NUMBER")

    @cached_property
    def asana_github_issue_field_gid(self) -> str | None:
        return _get("ASANA_GITHUB_ISSUE_FIELD_GID")

    @cached_property
    def sync_completed(self) -> bool:
        return _bool("SYNC_COMPLETED")

    @cached_property
    def dry_run(self) -> bool:
        return _bool("DRY_RUN")

    @cached_property
    def default_labels(self) -> list[str]:
        raw = _get("DEFAULT_LABELS", "") or ""
        return [label.strip() for label in raw.split(",") if label.strip()]

    @cached_property
    def github_issue_number(self) -> int | None:
        return _int("GITHUB_ISSUE_NUMBER")

    @cached_property
    def github_project_status(self) -> str | None:
        return _get("GITHUB_PROJECT_STATUS")

    @cached_property
    def columns_only(self) -> bool:
        return _bool("COLUMNS_ONLY")

    @cached_property
    def sync_all_project_items(self) -> bool:
        return _bool("SYNC_ALL_PROJECT_ITEMS")

    @cached_property
    def log_level(self) -> str:
        return (_get("LOG_LEVEL") or "INFO").upper()

cfg = Config()

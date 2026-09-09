"""Strongly typed accessors for process environment / .env."""

from __future__ import annotations

import os
import re
import sys
from functools import cached_property

from dotenv import load_dotenv

load_dotenv()

_ASANA_GID_DIGITS = re.compile(r"^\d+$")
_ASANA_GID_SCIENTIFIC = re.compile(r"^\d+\.\d+[eE][+-]?\d+$")


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


def normalize_asana_gid(raw: str, *, name: str = "ASANA_PROJECT_GID") -> str:
    """
    Return a digit-only Asana GID.

    Unquoted GIDs in YAML become floats such as 1.21077172090522E+15, which
    Asana rejects. Fail clearly so the workflow can quote the value.
    """
    value = raw.strip()
    if _ASANA_GID_DIGITS.fullmatch(value):
        return value
    if _ASANA_GID_SCIENTIFIC.fullmatch(value):
        sys.exit(
            f"{name} looks like a floating-point number ({value!r}). "
            "Asana GIDs are large integers. Quote them as strings in YAML, "
            'for example asana_project_gid: "1210771720905224".'
        )
    sys.exit(f"{name} must be an Asana project GID (digits only), got: {value!r}")


class Config:
    """Typed wrappers around the environment variables this project uses."""

    @cached_property
    def asana_token(self) -> str:
        return _require("ASANA_TOKEN")

    @cached_property
    def asana_project_gid(self) -> str:
        return normalize_asana_gid(_require("ASANA_PROJECT_GID"))

    def using_actions_job_token(self) -> bool:
        """True when this process is using the built-in Actions GITHUB_TOKEN."""
        current = _get("GITHUB_REPOSITORY")
        return bool(current) and not _get("GH_TOKEN")

    @cached_property
    def github_token(self) -> str:
        token = _get("GITHUB_TOKEN") or _get("GH_TOKEN")
        if not token:
            sys.exit("Missing GitHub token. Set GITHUB_TOKEN locally, or secret GH_TOKEN in Actions.")
        return token

    def require_github_token(self, repo: str | None = None) -> str:
        """
        Return a GitHub token, or exit if the Actions job token cannot serve this run.

        The built-in job token can read and write issues in this repository
        (`GITHUB_REPOSITORY`). Secret `GH_TOKEN` (a PAT) is required when the
        target repo is different, or when GitHub Projects is configured.
        """
        token = self.github_token
        current = _get("GITHUB_REPOSITORY" ) or ""
        using_builtin = bool(current) and not _get("GH_TOKEN")
        if not using_builtin:
            return token
        if self.github_project_owner and self.github_project_number:
            sys.exit(
                "GitHub Projects requires a PAT. Set secret GH_TOKEN "
                "(the built-in GITHUB_TOKEN cannot access Projects v2)."
            )
        target = repo or current
        if (target or "").casefold() != current.casefold():
            sys.exit(f"Target repository {target!r} is not this Actions repository ({current}). Set secret GH_TOKEN.")
        return token

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
    def asana_section(self) -> str | None:
        return _get("ASANA_SECTION")

    @cached_property
    def asana_status_field(self) -> str | None:
        return _get("ASANA_STATUS_FIELD")

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
        return _bool("SYNC_ALL_PROJECT_ITEMS", default=True)

    @cached_property
    def log_level(self) -> str:
        return (_get("LOG_LEVEL") or "INFO").upper()

    @cached_property
    def a2g_enrichment(self) -> str:
        """Post-create hook mode. Default off — no extra GitHub or Asana writes."""
        return (_get("A2G_ENRICHMENT") or "off").strip().lower()


cfg = Config()

"""Tests for scripts/run-sync.sh validation and the uv argv it builds."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run-sync.sh"


@pytest.fixture
def uv_log(tmp_path: Path) -> Path:
    """Install a stub `uv` on PATH that records argv; return the log path."""
    log = tmp_path / "uv-argv.log"
    stub = tmp_path / "uv"
    stub.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "$UV_ARGV_LOG"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return log


def test_invalid_mode_exits(uv_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run_sync(uv_log, monkeypatch, SYNC_MODE="nope")

    assert result.returncode == 1
    assert "::error::mode must be a2g or g2a, got: nope" in _output(result)
    assert _uv_argv(uv_log) == []


def test_missing_asana_token_exits(uv_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run_sync(uv_log, monkeypatch, ASANA_TOKEN="")

    assert result.returncode == 1
    assert "::error::asana_token is required" in _output(result)
    assert _uv_argv(uv_log) == []


def test_missing_asana_project_gid_exits(uv_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run_sync(uv_log, monkeypatch, ASANA_PROJECT_GID="")

    assert result.returncode == 1
    assert "::error::asana_project_gid is required" in _output(result)
    assert _uv_argv(uv_log) == []


def test_float_looking_project_gid_exits(uv_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run_sync(uv_log, monkeypatch, ASANA_PROJECT_GID="1.21e+15")

    assert result.returncode == 1
    assert "::error::asana_project_gid looks like a float (1.21e+15)" in _output(result)
    assert _uv_argv(uv_log) == []


def test_a2g_missing_destination_exits(uv_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run_sync(
        uv_log,
        monkeypatch,
        GITHUB_REPO="",
        GITHUB_PROJECT_OWNER="",
        GITHUB_PROJECT_NUMBER="",
        GITHUB_REPOSITORY="",
    )

    assert result.returncode == 1
    assert "::error::Configure github_project_owner + github_project_number and/or github_repo" in _output(result)
    assert _uv_argv(uv_log) == []


def test_a2g_success_records_repo_and_project_gid(uv_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run_sync(uv_log, monkeypatch)

    assert result.returncode == 0
    assert _uv_argv(uv_log) == ["run A2G --repo owner/repo --project-gid 1210771720905224"]


def test_a2g_optional_flags_are_forwarded(uv_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run_sync(
        uv_log,
        monkeypatch,
        ASANA_SECTION="In Progress",
        ASANA_STATUS_FIELD="Status",
        DRY_RUN="true",
        SYNC_COMPLETED="true",
    )

    assert result.returncode == 0
    assert _uv_argv(uv_log) == [
        "run A2G --section In Progress --status-field Status "
        "--repo owner/repo --project-gid 1210771720905224 --dry-run --sync-completed"
    ]


def test_missing_gh_token_with_projects_exits(uv_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run_sync(
        uv_log,
        monkeypatch,
        GH_TOKEN="",
        GITHUB_PROJECT_OWNER="acme",
        GITHUB_PROJECT_NUMBER="3",
    )

    assert result.returncode == 1
    assert "::error::GitHub Projects requires a PAT. Set secret GH_TOKEN." in _output(result)
    assert _uv_argv(uv_log) == []


def test_same_repo_without_projects_uses_builtin_token(uv_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run_sync(
        uv_log,
        monkeypatch,
        GH_TOKEN="",
        GITHUB_REPOSITORY="owner/repo",
        GITHUB_REPO="owner/repo",
        GITHUB_PROJECT_OWNER="",
        GITHUB_PROJECT_NUMBER="",
    )

    assert result.returncode == 0
    assert "Using the built-in GITHUB_TOKEN for owner/repo (same repository, no Projects board)." in _output(result)
    assert _uv_argv(uv_log) == ["run A2G --repo owner/repo --project-gid 1210771720905224"]


def test_g2a_requires_issue_number_or_all_items(uv_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run_sync(
        uv_log,
        monkeypatch,
        SYNC_MODE="g2a",
        SYNC_ALL_PROJECT_ITEMS="false",
        GITHUB_ISSUE_NUMBER="",
    )

    assert result.returncode == 1
    assert "::error::g2a requires issue_number, or set sync_all_project_items=true" in _output(result)
    assert _uv_argv(uv_log) == []


def test_g2a_single_item_records_issue_flag(uv_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run_sync(
        uv_log,
        monkeypatch,
        SYNC_MODE="g2a",
        SYNC_ALL_PROJECT_ITEMS="false",
        GITHUB_ISSUE_NUMBER="42",
    )

    assert result.returncode == 0
    assert "Syncing owner/repo#42 → Asana" in _output(result)
    assert _uv_argv(uv_log) == ["run G2A --issue 42"]


def test_g2a_all_items_and_columns_only(uv_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run_sync(
        uv_log,
        monkeypatch,
        SYNC_MODE="g2a",
        SYNC_ALL_PROJECT_ITEMS="true",
        GITHUB_PROJECT_OWNER="acme",
        GITHUB_PROJECT_NUMBER="3",
        COLUMNS_ONLY="true",
    )

    assert result.returncode == 0
    assert "Reconciling all project items on acme/#3" in _output(result)
    assert "Mode: columns-only (Status → Asana section)" in _output(result)
    assert _uv_argv(uv_log) == ["run G2A --all-project-items --columns-only"]


def test_g2a_fills_repo_from_github_repository(uv_log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run_sync(
        uv_log,
        monkeypatch,
        SYNC_MODE="g2a",
        GITHUB_REPO="",
        GITHUB_REPOSITORY="owner/from-actions",
        SYNC_ALL_PROJECT_ITEMS="true",
        GITHUB_PROJECT_OWNER="",
        GITHUB_PROJECT_NUMBER="",
    )

    assert result.returncode == 0
    assert "Reconciling all issues in owner/from-actions" in _output(result)
    assert _uv_argv(uv_log) == ["run G2A --all-project-items"]


def _run_sync(
    uv_log: Path,
    monkeypatch: pytest.MonkeyPatch,
    **overrides: str,
) -> subprocess.CompletedProcess[str]:
    stub_dir = uv_log.parent
    env = _base_env()
    env.update(overrides)
    env["UV_ARGV_LOG"] = str(uv_log)
    env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
    monkeypatch.chdir(REPO_ROOT)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _base_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "SYNC_MODE": "a2g",
            "ASANA_TOKEN": "asana-test-token",
            "GH_TOKEN": "gh-test-token",
            "ASANA_PROJECT_GID": "1210771720905224",
            "GITHUB_PROJECT_OWNER": "",
            "GITHUB_PROJECT_NUMBER": "",
            "GITHUB_REPO": "owner/repo",
            "GITHUB_REPOSITORY": "owner/repo",
            "ASANA_SECTION": "",
            "ASANA_STATUS_FIELD": "",
            "DRY_RUN": "false",
            "SYNC_COMPLETED": "false",
            "GITHUB_ISSUE_NUMBER": "",
            "COLUMNS_ONLY": "false",
            "SYNC_ALL_PROJECT_ITEMS": "false",
        }
    )
    return env


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return f"{result.stdout}{result.stderr}"


def _uv_argv(uv_log: Path) -> list[str]:
    if not uv_log.exists():
        return []
    return [line for line in uv_log.read_text(encoding="utf-8").splitlines() if line]

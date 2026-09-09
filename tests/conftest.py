"""Shared fixtures and HTTP stubs for Asana ↔ GitHub contract tests."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import responses

from asana_sync.cfg import cfg

FIXTURES_DIR = Path(__file__).parent / "fixtures"

ASANA_BASE = "https://app.asana.com/api/1.0"
GITHUB_API = "https://api.github.com"
PROJECT_GID = "111222333"
REPO = "owner/repo"
TASK_IN_PROGRESS_GID = "444555666"
FIELD_GITHUB_ISSUE = "fld-gh"
FIELD_STATUS = "fld-status"
SECTION_IN_PROGRESS = "sec-in-progress"
SECTION_BACKLOG = "sec-backlog"
WORKSPACE_GID = "ws-1"
ISSUE_URL = "https://github.com/owner/repo/issues/42"


def load_fixture(name: str):
    """Return parsed JSON from tests/fixtures/<name>."""
    return json.loads((FIXTURES_DIR / name).read_text())


@pytest.fixture
def http():
    """Mock requests HTTP. Unexpected live calls fail instead of hitting the network."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield rsps


@pytest.fixture
def sync_env(monkeypatch):
    """Process env for A2G/G2A tests, isolated from a developer .env."""
    monkeypatch.setenv("ASANA_TOKEN", "asana-test-token")
    monkeypatch.setenv("ASANA_PROJECT_GID", PROJECT_GID)
    monkeypatch.setenv("GITHUB_TOKEN", "gh-test-token")
    monkeypatch.setenv("GITHUB_REPO", REPO)
    monkeypatch.setenv("ASANA_GITHUB_ISSUE_FIELD_GID", FIELD_GITHUB_ISSUE)
    monkeypatch.delenv("GITHUB_PROJECT_OWNER", raising=False)
    monkeypatch.delenv("GITHUB_PROJECT_NUMBER", raising=False)
    monkeypatch.delenv("ASANA_SECTION", raising=False)
    monkeypatch.delenv("ASANA_STATUS_FIELD", raising=False)
    monkeypatch.delenv("SYNC_COMPLETED", raising=False)
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("DEFAULT_LABELS", raising=False)
    monkeypatch.delenv("A2G_ENRICHMENT", raising=False)
    monkeypatch.delenv("GITHUB_PROJECT_STATUS", raising=False)
    monkeypatch.delenv("COLUMNS_ONLY", raising=False)
    cfg.__dict__.clear()
    yield
    cfg.__dict__.clear()


@pytest.fixture
def skip_validate(monkeypatch):
    """Skip live token/project probes so tests cover sync HTTP only."""
    monkeypatch.setattr("asana_sync.asana_to_github.validate_environment", lambda repo, project_gid: None)
    monkeypatch.setattr("asana_sync.asana_to_github.time.sleep", lambda _seconds: None)


def run_a2g(*args: str) -> None:
    """Run the A2G CLI with the given argv (no program name)."""
    from asana_sync.asana_to_github import main

    main(list(args))


def run_g2a(*args: str):
    """Run the G2A CLI with the given argv (no program name). Returns main()'s value."""
    from asana_sync.github_to_asana import main

    return main(list(args))


def stub_github_issue_list(http: responses.RequestsMock, issues: list[dict]) -> None:
    """Paginated GET /repos/{repo}/issues used to find already-synced GIDs."""

    def _callback(request):
        page = parse_qs(urlparse(request.url).query).get("page", ["1"])[0]
        payload = issues if page == "1" else []
        return (200, {}, json.dumps(payload))

    http.add_callback(
        responses.GET,
        f"{GITHUB_API}/repos/{REPO}/issues",
        callback=_callback,
        content_type="application/json",
    )


def stub_asana_sections(http: responses.RequestsMock, sections: list[dict] | None = None) -> None:
    http.add(
        responses.GET,
        f"{ASANA_BASE}/projects/{PROJECT_GID}/sections",
        json={"data": sections if sections is not None else load_fixture("asana_sections.json")},
        status=200,
    )


def stub_asana_section_tasks(http: responses.RequestsMock, section_gid: str, tasks: list[dict]) -> None:
    http.add(
        responses.GET,
        f"{ASANA_BASE}/sections/{section_gid}/tasks",
        json={"data": tasks},
        status=200,
    )


def stub_asana_project_tasks(http: responses.RequestsMock, tasks: list[dict]) -> None:
    http.add(
        responses.GET,
        f"{ASANA_BASE}/projects/{PROJECT_GID}/tasks",
        json={"data": tasks},
        status=200,
    )


def stub_custom_field_settings(http: responses.RequestsMock, payload: dict | None = None) -> None:
    http.add(
        responses.GET,
        f"{ASANA_BASE}/projects/{PROJECT_GID}/custom_field_settings",
        json=payload if payload is not None else load_fixture("asana_custom_field_settings.json"),
        status=200,
    )


def stub_create_issue(http: responses.RequestsMock, issue: dict | None = None) -> None:
    http.add(
        responses.POST,
        f"{GITHUB_API}/repos/{REPO}/issues",
        json=issue if issue is not None else load_fixture("github_issue_created.json"),
        status=201,
    )


def stub_reconcile(
    http: responses.RequestsMock,
    task_gid: str,
    *,
    attachments: list[dict] | None = None,
    allow_write: bool = True,
    stories: list[dict] | None = None,
    comments: list[dict] | None = None,
    issue_number: int = 42,
) -> None:
    """Asana write-back after a GitHub issue exists: field PUT + URL attachment + comments."""
    http.add(
        responses.GET,
        f"{ASANA_BASE}/attachments",
        json={"data": attachments if attachments is not None else []},
        status=200,
    )
    http.add(
        responses.GET,
        f"{ASANA_BASE}/tasks/{task_gid}/stories",
        json={"data": stories if stories is not None else []},
        status=200,
    )
    if stories:
        http.add(
            responses.GET,
            f"{GITHUB_API}/repos/{REPO}/issues/{issue_number}/comments",
            json=comments if comments is not None else [],
            status=200,
        )
        if allow_write:
            http.add(
                responses.POST,
                f"{GITHUB_API}/repos/{REPO}/issues/{issue_number}/comments",
                json={"id": 1, "body": "ok"},
                status=201,
            )
    if allow_write:
        http.add(
            responses.PUT,
            f"{ASANA_BASE}/tasks/{task_gid}",
            json={"data": {"gid": task_gid}},
            status=200,
        )
        http.add(
            responses.POST,
            f"{ASANA_BASE}/attachments",
            json={"data": {"gid": "att-1"}},
            status=201,
        )
        http.add(
            responses.PATCH,
            f"{GITHUB_API}/repos/{REPO}/issues/{issue_number}",
            json={"number": issue_number, "html_url": ISSUE_URL},
            status=200,
        )


def stub_asana_project(http: responses.RequestsMock, workspace_gid: str = WORKSPACE_GID) -> None:
    http.add(
        responses.GET,
        f"{ASANA_BASE}/projects/{PROJECT_GID}",
        json={"data": {"gid": PROJECT_GID, "workspace": {"gid": workspace_gid}}},
        status=200,
    )


def stub_github_get_issue(http: responses.RequestsMock, issue: dict) -> None:
    http.add(
        responses.GET,
        f"{GITHUB_API}/repos/{REPO}/issues/{issue['number']}",
        json=issue,
        status=200,
    )


def stub_asana_update_task(http: responses.RequestsMock, task_gid: str) -> None:
    http.add(
        responses.PUT,
        f"{ASANA_BASE}/tasks/{task_gid}",
        json={"data": {"gid": task_gid}},
        status=200,
    )


def stub_asana_search(http: responses.RequestsMock, tasks: list[dict], workspace_gid: str = WORKSPACE_GID) -> None:
    http.add(
        responses.GET,
        f"{ASANA_BASE}/workspaces/{workspace_gid}/tasks/search",
        json={"data": tasks},
        status=200,
    )


def stub_asana_add_task(http: responses.RequestsMock, section_gid: str) -> None:
    http.add(
        responses.POST,
        f"{ASANA_BASE}/sections/{section_gid}/addTask",
        json={"data": {}},
        status=200,
    )


def g2a_prereqs(http: responses.RequestsMock) -> None:
    """Asana field settings + project workspace that G2A main() always fetches."""
    stub_custom_field_settings(http)
    stub_asana_project(http)


def calls_matching(http: responses.RequestsMock, method: str, url_part: str) -> list:
    """Return recorded calls whose method and URL contain the given fragments."""
    return [call for call in http.calls if call.request.method == method and url_part in call.request.url]

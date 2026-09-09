"""G2A supported extra: title/notes/labels sync, columns-only, and Done status."""

from __future__ import annotations

import json

from asana_sync.github_to_asana import (
    body_for_asana,
    build_task_update,
    index_custom_fields,
    sync_one_issue,
)
from tests.conftest import (
    ASANA_BASE,
    FIELD_GITHUB_ISSUE,
    PROJECT_GID,
    REPO,
    SECTION_IN_PROGRESS,
    TASK_IN_PROGRESS_GID,
    WORKSPACE_GID,
    calls_matching,
    g2a_prereqs,
    load_fixture,
    run_g2a,
    stub_asana_add_task,
    stub_asana_update_task,
    stub_github_issue_list,
)

FIELD_LABELS = "fld-labels"
FIELD_TYPE = "fld-type"
FIELD_PRIORITY = "fld-priority"
FIELD_POINTS = "fld-points"


def _fields():
    return index_custom_fields(load_fixture("asana_custom_field_settings_labels.json")["data"])


def _issue(**overrides):
    issue = load_fixture("github_issue_open_linked.json")
    issue.update(overrides)
    return issue


def _sync(issue, *, sections=None, columns_only=False, status_name=None):
    return sync_one_issue(
        asana_token="asana-test-token",
        gh_token="gh-test-token",
        asana_project_gid=PROJECT_GID,
        repo=REPO,
        issue=issue,
        fields_by_name=_fields(),
        github_issue_field_gid=FIELD_GITHUB_ISSUE,
        sections=sections,
        project_owner=None,
        project_number=None,
        workspace_gid=WORKSPACE_GID,
        columns_only=columns_only,
        dry_run=False,
        status_name=status_name,
    )


def test_body_for_asana_strips_marker_and_meta():
    issue = load_fixture("github_issue_closed_linked.json")
    notes = body_for_asana(issue["body"])
    assert notes == "Ship usage tracking for the dashboard."
    assert "asana-task-gid" not in notes
    assert "View in Asana" not in notes


def test_title_body_and_labels_map_onto_asana_fields():
    issue = _issue(
        title="Renamed from GitHub",
        labels=[
            {"name": "needs-docs"},
            {"name": "bug"},
            {"name": "P1"},
            {"name": "sp:3"},
        ],
    )
    update, unmatched = build_task_update(issue, _fields(), FIELD_GITHUB_ISSUE)

    assert unmatched == []
    assert update["name"] == "Renamed from GitHub"
    assert update["notes"] == "Ship usage tracking for the dashboard."
    assert update["completed"] is False
    assert update["custom_fields"][FIELD_GITHUB_ISSUE] == "42"
    assert update["custom_fields"][FIELD_LABELS] == ["opt-docs"]
    assert update["custom_fields"][FIELD_TYPE] == "opt-type-bug"
    assert update["custom_fields"][FIELD_PRIORITY] == "opt-p1"
    assert update["custom_fields"][FIELD_POINTS] == 3.0


def test_full_sync_writes_name_notes_and_labels(http, sync_env):
    issue = _issue(
        title="Renamed from GitHub",
        labels=[{"name": "needs-docs"}],
    )
    stub_asana_update_task(http, TASK_IN_PROGRESS_GID)

    ok, _message = _sync(issue)

    assert ok
    puts = calls_matching(http, "PUT", f"{ASANA_BASE}/tasks/{TASK_IN_PROGRESS_GID}")
    assert len(puts) == 1
    payload = json.loads(puts[0].request.body)["data"]
    assert payload["name"] == "Renamed from GitHub"
    assert payload["notes"] == "Ship usage tracking for the dashboard."
    assert payload["custom_fields"][FIELD_LABELS] == ["opt-docs"]
    assert "name" in payload


def test_columns_only_updates_completion_and_section_not_title(http, sync_env):
    issue = _issue(title="Should not be written", state="closed")
    sections = load_fixture("asana_sections.json")
    stub_asana_update_task(http, TASK_IN_PROGRESS_GID)
    stub_asana_add_task(http, SECTION_IN_PROGRESS)

    ok, message = _sync(issue, sections=sections, columns_only=True, status_name="In Progress")

    assert ok
    puts = calls_matching(http, "PUT", f"{ASANA_BASE}/tasks/{TASK_IN_PROGRESS_GID}")
    assert len(puts) == 1
    payload = json.loads(puts[0].request.body)["data"]
    assert payload == {"completed": True}
    assert "name" not in payload
    assert "notes" not in payload
    moves = calls_matching(http, "POST", f"{ASANA_BASE}/sections/{SECTION_IN_PROGRESS}/addTask")
    assert len(moves) == 1
    assert json.loads(moves[0].request.body)["data"]["task"] == TASK_IN_PROGRESS_GID
    assert "moved to section 'In Progress'" in message


def test_projects_status_done_completes_even_if_github_issue_is_open(http, sync_env):
    issue = _issue(state="open")
    stub_asana_update_task(http, TASK_IN_PROGRESS_GID)

    ok, _message = _sync(issue, status_name="Done")

    assert ok
    puts = calls_matching(http, "PUT", f"{ASANA_BASE}/tasks/{TASK_IN_PROGRESS_GID}")
    assert json.loads(puts[0].request.body)["data"]["completed"] is True


def test_all_items_without_project_syncs_repo_issues(http, sync_env, monkeypatch):
    monkeypatch.setattr("asana_sync.github_to_asana.time.sleep", lambda _seconds: None)
    issue = _issue()
    g2a_prereqs(http)
    stub_github_issue_list(http, [issue])
    stub_asana_update_task(http, TASK_IN_PROGRESS_GID)

    result = run_g2a("--all-project-items")

    assert result == 0
    puts = calls_matching(http, "PUT", f"{ASANA_BASE}/tasks/{TASK_IN_PROGRESS_GID}")
    assert len(puts) == 1
    payload = json.loads(puts[0].request.body)["data"]
    assert payload["name"] == issue["title"]
    assert payload["completed"] is False


def test_all_items_without_project_requires_repo(http, sync_env, monkeypatch):
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    from asana_sync.cfg import cfg

    cfg.__dict__.clear()
    g2a_prereqs(http)

    try:
        run_g2a("--all-project-items")
    except SystemExit as exc:
        assert "GITHUB_REPO" in str(exc)
    else:
        raise AssertionError("expected SystemExit when GITHUB_REPO is unset")


def test_unmatched_section_skips_column_move_but_still_completes(http, sync_env):
    issue = _issue(state="closed")
    sections = load_fixture("asana_sections.json")
    stub_asana_update_task(http, TASK_IN_PROGRESS_GID)

    ok, message = _sync(issue, sections=sections, status_name="Waiting on review")

    assert ok
    puts = calls_matching(http, "PUT", f"{ASANA_BASE}/tasks/{TASK_IN_PROGRESS_GID}")
    assert json.loads(puts[0].request.body)["data"]["completed"] is True
    assert calls_matching(http, "POST", "/sections/") == []
    assert "no Asana section matched" in message

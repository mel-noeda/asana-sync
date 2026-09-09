"""Contract tests for bidirectional Asana ↔ GitHub linking."""

from __future__ import annotations

import json

import responses

from asana_sync.asana_to_github import MARKER_TEMPLATE, build_body, reconcile_asana_github_link
from asana_sync.github_to_asana import resolve_asana_task
from tests.conftest import (
    ASANA_BASE,
    FIELD_GITHUB_ISSUE,
    ISSUE_URL,
    PROJECT_GID,
    REPO,
    TASK_IN_PROGRESS_GID,
    calls_matching,
    load_fixture,
    run_a2g,
    stub_asana_section_tasks,
    stub_asana_sections,
    stub_create_issue,
    stub_github_issue_list,
    stub_reconcile,
)


def test_build_body_includes_notes_asana_url_and_marker():
    task = load_fixture("asana_task_in_progress.json")
    body = build_body(task)

    assert "Ship usage tracking for the dashboard." in body
    assert f"[View in Asana]({task['permalink_url']})" in body
    assert MARKER_TEMPLATE.format(gid=task["gid"]) in body
    assert f"<!-- asana-task-gid:{task['gid']} -->" in body


def test_a2g_create_writes_marker_issue_number_and_attachment(http, sync_env, skip_validate):
    task = load_fixture("asana_task_in_progress.json")
    stub_github_issue_list(http, [])
    stub_asana_sections(http)
    stub_asana_section_tasks(http, "sec-in-progress", [task])
    stub_create_issue(http)
    stub_reconcile(http, TASK_IN_PROGRESS_GID)

    run_a2g("--repo", REPO, "--project-gid", PROJECT_GID, "--section", "In Progress")

    creates = calls_matching(http, "POST", f"{REPO}/issues")
    assert len(creates) == 1
    posted = json.loads(creates[0].request.body)
    assert posted["title"] == "Usage Tracking"
    assert f"<!-- asana-task-gid:{TASK_IN_PROGRESS_GID} -->" in posted["body"]
    assert task["permalink_url"] in posted["body"]

    puts = calls_matching(http, "PUT", f"{ASANA_BASE}/tasks/{TASK_IN_PROGRESS_GID}")
    assert len(puts) == 1
    assert json.loads(puts[0].request.body) == {"data": {"custom_fields": {FIELD_GITHUB_ISSUE: "42"}}}

    attachments = calls_matching(http, "POST", f"{ASANA_BASE}/attachments")
    assert len(attachments) == 1
    assert ISSUE_URL in attachments[0].request.body.decode()


def test_a2g_rerun_is_noop_when_already_linked(http, sync_env, skip_validate):
    task = load_fixture("asana_task_in_progress.json")
    task["custom_fields"] = [
        {**field, "text_value": "42", "display_value": "42"} if field["gid"] == FIELD_GITHUB_ISSUE else field
        for field in task["custom_fields"]
    ]
    existing = load_fixture("github_issue_existing_linked.json")
    stub_github_issue_list(http, [existing])
    stub_asana_sections(http)
    stub_asana_section_tasks(http, "sec-in-progress", [task])
    stub_reconcile(
        http,
        TASK_IN_PROGRESS_GID,
        attachments=[{"view_url": ISSUE_URL, "name": "#42 Usage Tracking"}],
        allow_write=False,
    )

    run_a2g("--repo", REPO, "--project-gid", PROJECT_GID, "--section", "In Progress")

    assert calls_matching(http, "POST", f"{REPO}/issues") == []
    assert calls_matching(http, "PUT", f"{ASANA_BASE}/tasks/{TASK_IN_PROGRESS_GID}") == []
    assert calls_matching(http, "POST", f"{ASANA_BASE}/attachments") == []


def test_reconcile_is_noop_when_field_and_attachment_match(http):
    task = load_fixture("asana_task_in_progress.json")
    task["custom_fields"] = [
        {**field, "text_value": "42"} if field["gid"] == FIELD_GITHUB_ISSUE else field
        for field in task["custom_fields"]
    ]
    stub_reconcile(
        http,
        TASK_IN_PROGRESS_GID,
        attachments=[{"view_url": ISSUE_URL}],
        allow_write=False,
    )
    issue = load_fixture("github_issue_created.json")

    status = reconcile_asana_github_link("asana-test-token", task, issue, FIELD_GITHUB_ISSUE)

    assert status == "already linked"
    assert calls_matching(http, "PUT", f"{ASANA_BASE}/tasks/") == []
    assert calls_matching(http, "POST", f"{ASANA_BASE}/attachments") == []


def test_g2a_resolves_task_from_body_marker_first(http):
    issue = {
        "number": 42,
        "body": "notes\n<!-- asana-task-gid:444555666 -->",
    }
    fields = {"GitHub Issue #": {"gid": FIELD_GITHUB_ISSUE}}

    gid, source = resolve_asana_task(
        "asana-test-token",
        PROJECT_GID,
        issue,
        fields,
        workspace_gid="ws-1",
    )

    assert gid == TASK_IN_PROGRESS_GID
    assert source == "issue body marker"
    assert calls_matching(http, "GET", "/tasks/search") == []


def test_g2a_resolves_task_from_github_issue_field(http):
    issue = {"number": 42, "body": "no marker here"}
    fields = {"GitHub Issue #": {"gid": FIELD_GITHUB_ISSUE}}
    http.add(
        responses.GET,
        f"{ASANA_BASE}/workspaces/ws-1/tasks/search",
        json={"data": [{"gid": TASK_IN_PROGRESS_GID, "name": "Usage Tracking"}]},
        status=200,
    )

    gid, source = resolve_asana_task(
        "asana-test-token",
        PROJECT_GID,
        issue,
        fields,
        workspace_gid="ws-1",
    )

    assert gid == TASK_IN_PROGRESS_GID
    assert source == "GitHub Issue # custom field"
    searches = calls_matching(http, "GET", "/tasks/search")
    assert len(searches) == 1
    assert f"custom_fields.{FIELD_GITHUB_ISSUE}.value=42" in searches[0].request.url

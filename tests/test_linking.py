"""Contract tests for bidirectional Asana ↔ GitHub linking."""

from __future__ import annotations

import json

import responses

from asana_sync.asana_to_github import (
    MARKER_TEMPLATE,
    STORY_MARKER_TEMPLATE,
    asana_find_github_issue_field,
    build_body,
    build_github_comment_from_story,
    reconcile_asana_github_link,
    reconcile_existing_issue,
    reconcile_github_issue_from_asana,
    sync_asana_comments_to_github,
)
from asana_sync.github_to_asana import github_issue_field, resolve_asana_task
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
    stub_custom_field_settings,
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
    assert calls_matching(http, "PATCH", f"{REPO}/issues/42") == []
    assert calls_matching(http, "POST", f"{REPO}/issues/42/comments") == []


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


def test_find_github_issue_field_accepts_name_without_hash(http):
    stub_custom_field_settings(http, load_fixture("asana_custom_field_settings_github_issue.json"))

    gid, name = asana_find_github_issue_field("asana-test-token", PROJECT_GID)

    assert gid == FIELD_GITHUB_ISSUE
    assert name == "GitHub Issue"


def test_github_issue_field_prefers_hashed_name():
    fields = {
        "GitHub Issue #": {"gid": "fld-hash"},
        "GitHub Issue": {"gid": "fld-plain"},
    }

    assert github_issue_field(fields)["gid"] == "fld-hash"
    assert github_issue_field({"GitHub Issue": {"gid": "fld-plain"}})["gid"] == "fld-plain"
    assert github_issue_field({}) is None


def test_reconcile_attaches_url_without_custom_field(http):
    task = load_fixture("asana_task_in_progress.json")
    stub_reconcile(http, TASK_IN_PROGRESS_GID)
    issue = load_fixture("github_issue_created.json")

    status = reconcile_asana_github_link("asana-test-token", task, issue, None)

    assert "linked" in status
    assert calls_matching(http, "PUT", f"{ASANA_BASE}/tasks/") == []
    assert len(calls_matching(http, "POST", f"{ASANA_BASE}/attachments")) == 1


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


def test_g2a_resolves_task_from_github_issue_field_without_hash(http):
    issue = {"number": 42, "body": "no marker here"}
    fields = {"GitHub Issue": {"gid": FIELD_GITHUB_ISSUE}}
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


def test_build_github_comment_includes_author_text_and_story_marker():
    story = load_fixture("asana_comment_stories.json")[0]
    body = build_github_comment_from_story(story)

    assert "**Luis Ackermann** commented in Asana (2026-09-08):" in body
    assert "I'm investigating the reload abort." in body
    assert STORY_MARKER_TEMPLATE.format(gid=story["gid"]) in body


def test_existing_issue_updates_title_and_body_when_asana_changed(http):
    task = load_fixture("asana_task_in_progress.json")
    issue = {
        "number": 42,
        "html_url": ISSUE_URL,
        "title": "Old title",
        "body": "stale body\n<!-- asana-task-gid:444555666 -->",
    }
    stub_reconcile(http, TASK_IN_PROGRESS_GID)

    status = reconcile_github_issue_from_asana("gh-test-token", REPO, task, issue)

    assert "update title" in status[0]
    assert "update body from Asana" in status
    patches = calls_matching(http, "PATCH", f"{REPO}/issues/42")
    assert len(patches) == 1
    posted = json.loads(patches[0].request.body)
    assert posted["title"] == "Usage Tracking"
    assert MARKER_TEMPLATE.format(gid=TASK_IN_PROGRESS_GID) in posted["body"]
    assert "Ship usage tracking for the dashboard." in posted["body"]


def test_existing_issue_skips_patch_when_title_and_body_match(http):
    task = load_fixture("asana_task_in_progress.json")
    issue = load_fixture("github_issue_existing_linked.json")

    status = reconcile_github_issue_from_asana("gh-test-token", REPO, task, issue)

    assert status == []
    assert calls_matching(http, "PATCH", f"{REPO}/issues/42") == []


def test_asana_comments_are_copied_to_github(http):
    task = load_fixture("asana_task_in_progress.json")
    issue = load_fixture("github_issue_existing_linked.json")
    stories = load_fixture("asana_comment_stories.json")
    stub_reconcile(http, TASK_IN_PROGRESS_GID, stories=stories)

    status = sync_asana_comments_to_github("asana-test-token", "gh-test-token", REPO, task, issue)

    assert status == "copied 2 comment(s)"
    posts = calls_matching(http, "POST", f"{REPO}/issues/42/comments")
    assert len(posts) == 2
    bodies = [json.loads(call.request.body)["body"] for call in posts]
    joined = "".join(bodies)
    assert "I'm investigating the reload abort." in joined
    assert "Upgrade Dapr and re-check the logs." in joined
    assert STORY_MARKER_TEMPLATE.format(gid="1211111111111111") in joined
    assert STORY_MARKER_TEMPLATE.format(gid="1211111111111113") in joined
    assert "moved this task" not in joined


def test_asana_comments_already_on_github_are_not_duplicated(http):
    task = load_fixture("asana_task_in_progress.json")
    issue = load_fixture("github_issue_existing_linked.json")
    stories = load_fixture("asana_comment_stories.json")
    existing_comments = [
        {"id": 1, "body": build_github_comment_from_story(stories[0])},
        {"id": 2, "body": build_github_comment_from_story(stories[2])},
    ]
    stub_reconcile(
        http,
        TASK_IN_PROGRESS_GID,
        stories=stories,
        comments=existing_comments,
        allow_write=False,
    )

    status = sync_asana_comments_to_github("asana-test-token", "gh-test-token", REPO, task, issue)

    assert status is None
    assert calls_matching(http, "POST", f"{REPO}/issues/42/comments") == []


def test_a2g_rerun_copies_new_asana_comments(http, sync_env, skip_validate):
    task = load_fixture("asana_task_in_progress.json")
    task["custom_fields"] = [
        {**field, "text_value": "42", "display_value": "42"} if field["gid"] == FIELD_GITHUB_ISSUE else field
        for field in task["custom_fields"]
    ]
    existing = load_fixture("github_issue_existing_linked.json")
    stories = load_fixture("asana_comment_stories.json")
    stub_github_issue_list(http, [existing])
    stub_asana_sections(http)
    stub_asana_section_tasks(http, "sec-in-progress", [task])
    stub_reconcile(
        http,
        TASK_IN_PROGRESS_GID,
        attachments=[{"view_url": ISSUE_URL, "name": "#42 Usage Tracking"}],
        stories=stories,
    )

    run_a2g("--repo", REPO, "--project-gid", PROJECT_GID, "--section", "In Progress")

    assert len(calls_matching(http, "POST", f"{REPO}/issues/42/comments")) == 2
    created = [call for call in calls_matching(http, "POST", f"{REPO}/issues") if "/comments" not in call.request.url]
    assert created == []


def test_reconcile_existing_issue_combines_content_link_and_comments(http):
    task = load_fixture("asana_task_in_progress.json")
    issue = {
        "number": 42,
        "html_url": ISSUE_URL,
        "title": "Old title",
        "body": "stale",
    }
    stories = load_fixture("asana_comment_stories.json")
    stub_reconcile(http, TASK_IN_PROGRESS_GID, stories=stories)

    status = reconcile_existing_issue(
        "asana-test-token",
        "gh-test-token",
        REPO,
        task,
        issue,
        FIELD_GITHUB_ISSUE,
    )

    assert "update title" in status
    assert "linked" in status
    assert "copied 2 comment(s)" in status

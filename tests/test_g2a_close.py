"""G2A primary contract: complete Asana on GitHub close; skip unlinked issues."""

from __future__ import annotations

import json

from tests.conftest import (
    ASANA_BASE,
    TASK_IN_PROGRESS_GID,
    calls_matching,
    g2a_prereqs,
    load_fixture,
    run_g2a,
    stub_asana_search,
    stub_asana_update_task,
    stub_github_get_issue,
)


def test_closed_issue_with_marker_completes_asana(http, sync_env):
    issue = load_fixture("github_issue_closed_linked.json")
    g2a_prereqs(http)
    stub_github_get_issue(http, issue)
    stub_asana_update_task(http, TASK_IN_PROGRESS_GID)

    result = run_g2a("42")

    assert result == 0
    puts = calls_matching(http, "PUT", f"{ASANA_BASE}/tasks/{TASK_IN_PROGRESS_GID}")
    assert len(puts) == 1
    assert json.loads(puts[0].request.body)["data"]["completed"] is True


def test_closed_issue_with_github_issue_field_completes_asana(http, sync_env):
    issue = load_fixture("github_issue_closed_field_only.json")
    g2a_prereqs(http)
    stub_github_get_issue(http, issue)
    stub_asana_search(http, [{"gid": TASK_IN_PROGRESS_GID, "name": "Usage Tracking"}])
    stub_asana_update_task(http, TASK_IN_PROGRESS_GID)

    result = run_g2a("42")

    assert result == 0
    puts = calls_matching(http, "PUT", f"{ASANA_BASE}/tasks/{TASK_IN_PROGRESS_GID}")
    assert len(puts) == 1
    assert json.loads(puts[0].request.body)["data"]["completed"] is True


def test_open_linked_issue_does_not_complete_asana(http, sync_env):
    issue = load_fixture("github_issue_open_linked.json")
    g2a_prereqs(http)
    stub_github_get_issue(http, issue)
    stub_asana_update_task(http, TASK_IN_PROGRESS_GID)

    result = run_g2a("42")

    assert result == 0
    puts = calls_matching(http, "PUT", f"{ASANA_BASE}/tasks/{TASK_IN_PROGRESS_GID}")
    assert len(puts) == 1
    payload = json.loads(puts[0].request.body)["data"]
    assert payload["completed"] is False
    assert payload["name"] == "Usage Tracking"


def test_unlinked_issue_skips_and_exits_0(http, sync_env):
    issue = load_fixture("github_issue_unlinked.json")
    g2a_prereqs(http)
    stub_github_get_issue(http, issue)
    stub_asana_search(http, [])

    result = run_g2a("99")

    assert result == 0
    assert calls_matching(http, "PUT", f"{ASANA_BASE}/tasks/") == []


def test_reopened_issue_sets_asana_completed_false(http, sync_env):
    issue = load_fixture("github_issue_open_linked.json")
    issue["title"] = "Usage Tracking (reopened)"
    g2a_prereqs(http)
    stub_github_get_issue(http, issue)
    stub_asana_update_task(http, TASK_IN_PROGRESS_GID)

    result = run_g2a("42")

    assert result == 0
    puts = calls_matching(http, "PUT", f"{ASANA_BASE}/tasks/{TASK_IN_PROGRESS_GID}")
    assert len(puts) == 1
    assert json.loads(puts[0].request.body)["data"]["completed"] is False

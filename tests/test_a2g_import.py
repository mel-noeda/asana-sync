"""Contract tests for A2G section/status gates, completion, and dry-run."""

from __future__ import annotations

import logging

from asana_sync.cfg import cfg
from tests.conftest import (
    ASANA_BASE,
    PROJECT_GID,
    REPO,
    TASK_IN_PROGRESS_GID,
    calls_matching,
    load_fixture,
    run_a2g,
    stub_asana_project_tasks,
    stub_asana_section_tasks,
    stub_asana_sections,
    stub_create_issue,
    stub_custom_field_settings,
    stub_github_issue_list,
    stub_reconcile,
)

A2G_GATE = ("--repo", REPO, "--project-gid", PROJECT_GID, "--section", "In Progress")


def test_in_progress_section_creates_one_issue(http, sync_env, skip_validate):
    task = load_fixture("asana_task_in_progress.json")
    stub_github_issue_list(http, [])
    stub_asana_sections(http)
    stub_asana_section_tasks(http, "sec-in-progress", [task])
    stub_create_issue(http)
    stub_reconcile(http, TASK_IN_PROGRESS_GID)

    run_a2g(*A2G_GATE)

    assert len(calls_matching(http, "POST", f"{REPO}/issues")) == 1


def test_backlog_section_task_is_not_created(http, sync_env, skip_validate):
    backlog = load_fixture("asana_task_backlog.json")
    stub_github_issue_list(http, [])
    stub_asana_sections(http)
    stub_asana_section_tasks(http, "sec-in-progress", [])
    stub_asana_section_tasks(http, "sec-backlog", [backlog])

    run_a2g(*A2G_GATE)

    assert calls_matching(http, "POST", f"{REPO}/issues") == []
    assert calls_matching(http, "GET", f"{ASANA_BASE}/sections/sec-backlog/tasks") == []


def test_status_in_progress_creates_one_issue(http, sync_env, skip_validate):
    tasks = [
        load_fixture("asana_task_in_progress.json"),
        load_fixture("asana_task_backlog.json"),
    ]
    stub_custom_field_settings(http)
    stub_github_issue_list(http, [])
    stub_asana_project_tasks(http, tasks)
    stub_create_issue(http)
    stub_reconcile(http, TASK_IN_PROGRESS_GID)

    run_a2g(*A2G_GATE, "--status-field", "Status")

    creates = calls_matching(http, "POST", f"{REPO}/issues")
    assert len(creates) == 1
    assert b"Usage Tracking" in creates[0].request.body


def test_status_backlog_is_not_created(http, sync_env, skip_validate):
    stub_custom_field_settings(http)
    stub_github_issue_list(http, [])
    stub_asana_project_tasks(http, [load_fixture("asana_task_backlog.json")])

    run_a2g(*A2G_GATE, "--status-field", "Status")

    assert calls_matching(http, "POST", f"{REPO}/issues") == []


def test_completed_task_skipped_unless_sync_completed(http, sync_env, skip_validate):
    completed = load_fixture("asana_task_completed.json")
    stub_github_issue_list(http, [])
    stub_asana_sections(http)
    stub_asana_section_tasks(http, "sec-in-progress", [completed])

    run_a2g(*A2G_GATE)

    assert calls_matching(http, "POST", f"{REPO}/issues") == []


def test_completed_task_imported_with_sync_completed(http, sync_env, skip_validate):
    completed = load_fixture("asana_task_completed.json")
    stub_github_issue_list(http, [])
    stub_asana_sections(http)
    stub_asana_section_tasks(http, "sec-in-progress", [completed])
    stub_create_issue(http)
    stub_reconcile(http, completed["gid"])

    run_a2g(*A2G_GATE, "--sync-completed")

    assert len(calls_matching(http, "POST", f"{REPO}/issues")) == 1


def test_dry_run_makes_no_github_or_asana_writes(http, sync_env, skip_validate):
    task = load_fixture("asana_task_in_progress.json")
    stub_github_issue_list(http, [])
    stub_asana_sections(http)
    stub_asana_section_tasks(http, "sec-in-progress", [task])

    run_a2g(*A2G_GATE, "--dry-run")

    assert calls_matching(http, "POST", f"{REPO}/issues") == []
    assert calls_matching(http, "PUT", f"{ASANA_BASE}/tasks/") == []
    assert calls_matching(http, "POST", f"{ASANA_BASE}/attachments") == []


def test_missing_github_issue_field_still_creates_without_writeback(http, sync_env, skip_validate, monkeypatch, caplog):
    monkeypatch.delenv("ASANA_GITHUB_ISSUE_FIELD_GID", raising=False)
    cfg.__dict__.clear()

    task = load_fixture("asana_task_in_progress.json")
    stub_custom_field_settings(http, load_fixture("asana_custom_field_settings_no_github.json"))
    stub_github_issue_list(http, [])
    stub_asana_sections(http)
    stub_asana_section_tasks(http, "sec-in-progress", [task])
    stub_create_issue(http)
    stub_reconcile(http, TASK_IN_PROGRESS_GID)

    with caplog.at_level(logging.WARNING):
        run_a2g(*A2G_GATE)

    assert len(calls_matching(http, "POST", f"{REPO}/issues")) == 1
    assert calls_matching(http, "PUT", f"{ASANA_BASE}/tasks/") == []
    assert len(calls_matching(http, "POST", f"{ASANA_BASE}/attachments")) == 1
    assert "not found" in caplog.text
    assert "will still be attached" in caplog.text


def test_github_issue_field_without_hash_writes_back(http, sync_env, skip_validate, monkeypatch):
    monkeypatch.delenv("ASANA_GITHUB_ISSUE_FIELD_GID", raising=False)
    cfg.__dict__.clear()

    task = load_fixture("asana_task_in_progress.json")
    stub_custom_field_settings(http, load_fixture("asana_custom_field_settings_github_issue.json"))
    stub_github_issue_list(http, [])
    stub_asana_sections(http)
    stub_asana_section_tasks(http, "sec-in-progress", [task])
    stub_create_issue(http)
    stub_reconcile(http, TASK_IN_PROGRESS_GID)

    run_a2g(*A2G_GATE)

    puts = calls_matching(http, "PUT", f"{ASANA_BASE}/tasks/{TASK_IN_PROGRESS_GID}")
    assert len(puts) == 1
    assert len(calls_matching(http, "POST", f"{ASANA_BASE}/attachments")) == 1


def test_a2g_enrichment_defaults_to_off(sync_env):
    assert cfg.a2g_enrichment == "off"


def test_enrichment_off_makes_no_extra_http(http, sync_env, skip_validate):
    from asana_sync.asana_to_github import enrich_created_issue

    task = load_fixture("asana_task_in_progress.json")
    stub_github_issue_list(http, [])
    stub_asana_sections(http)
    stub_asana_section_tasks(http, "sec-in-progress", [task])
    stub_create_issue(http)
    stub_reconcile(http, TASK_IN_PROGRESS_GID)

    run_a2g(*A2G_GATE)

    assert len(calls_matching(http, "POST", f"{REPO}/issues")) == 1
    assert calls_matching(http, "POST", f"{REPO}/issues/42/comments") == []
    assert calls_matching(http, "POST", f"{REPO}/issues/42/sub_issues") == []

    before = len(http.calls)
    enrich_created_issue("gh-test-token", REPO, {"number": 42}, task, mode="off")
    assert len(http.calls) == before

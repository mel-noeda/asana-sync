"""Asana section matching and G2A Status → column moves."""

from __future__ import annotations

import json

from asana_sync.asana_columns import (
    match_asana_section,
    match_named_value,
    normalize_column_key,
    parse_section_names,
    resolve_sections,
)
from asana_sync.github_to_asana import DONE_STATUS_KEYS, sync_column
from tests.conftest import ASANA_BASE, TASK_IN_PROGRESS_GID, calls_matching, stub_asana_add_task

SECTIONS = [
    {"gid": "sec-backlog", "name": "Backlog"},
    {"gid": "sec-ready", "name": "Ready / Sprint"},
    {"gid": "sec-in-progress", "name": "In Progress"},
    {"gid": "sec-done", "name": "Done"},
]


def test_normalize_column_key_strips_punctuation():
    assert normalize_column_key("Ready / Sprint") == "readysprint"
    assert normalize_column_key("ready-sprint") == "readysprint"


def test_match_asana_section_fuzzy_prefix():
    matched = match_asana_section("Ready", SECTIONS)
    assert matched is not None
    assert matched["gid"] == "sec-ready"


def test_match_named_value_accepts_status_aliases():
    assert match_named_value("In progress", ["In Progress"])
    assert not match_named_value("Backlog", ["In Progress"])


def test_parse_section_names_splits_and_strips():
    assert parse_section_names("In Progress, Backlog") == ["In Progress", "Backlog"]
    assert parse_section_names("") == []


def test_resolve_sections_reports_missing_names():
    resolved, missing = resolve_sections(["In Progress", "Unknown"], SECTIONS)
    assert [section["gid"] for section in resolved] == ["sec-in-progress"]
    assert missing == ["Unknown"]


def test_done_status_keys_include_complete_aliases():
    assert normalize_column_key("Done") in DONE_STATUS_KEYS
    assert normalize_column_key("completed") in DONE_STATUS_KEYS


def test_sync_column_skips_when_status_missing():
    assert sync_column("asana-test-token", TASK_IN_PROGRESS_GID, None, SECTIONS) == "no Status on project item"


def test_sync_column_skips_when_no_section_matches():
    message = sync_column("asana-test-token", TASK_IN_PROGRESS_GID, "Waiting", SECTIONS)
    assert message == "no Asana section matched Status 'Waiting'"


def test_sync_column_dry_run_does_not_post(http):
    message = sync_column(
        "asana-test-token",
        TASK_IN_PROGRESS_GID,
        "In Progress",
        SECTIONS,
        dry_run=True,
    )
    assert "would move to section 'In Progress'" in message
    assert calls_matching(http, "POST", "/sections/") == []


def test_sync_column_moves_task_to_matched_section(http):
    stub_asana_add_task(http, "sec-done")

    message = sync_column("asana-test-token", TASK_IN_PROGRESS_GID, "Done", SECTIONS)

    assert message == "moved to section 'Done' (from Status 'Done')"
    moves = calls_matching(http, "POST", f"{ASANA_BASE}/sections/sec-done/addTask")
    assert len(moves) == 1
    assert json.loads(moves[0].request.body)["data"]["task"] == TASK_IN_PROGRESS_GID

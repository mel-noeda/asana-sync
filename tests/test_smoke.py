"""Smoke tests that the flat-layout modules import and column matching works."""

from asana_columns import match_asana_section, normalize_column_key


def test_normalize_column_key_collapses_punctuation_and_case():
    assert normalize_column_key("In Progress") == "inprogress"
    assert normalize_column_key("IN-PROGRESS") == "inprogress"


def test_match_asana_section_exact_normalized():
    sections = [
        {"gid": "1", "name": "Backlog"},
        {"gid": "2", "name": "In Progress"},
    ]
    matched = match_asana_section("in-progress", sections)
    assert matched is not None
    assert matched["gid"] == "2"


def test_match_asana_section_returns_none_when_unmatched():
    sections = [{"gid": "1", "name": "Backlog"}]
    assert match_asana_section("Done", sections) is None

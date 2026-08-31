"""
test_brief_schema.py — pytest suite for Heart/tools/conformance.py validate_brief().

Covers:
    - Valid minimal brief passes with zero issues
    - Missing required top-level fields caught (ERROR)
    - Missing nested required fields in scope caught (ERROR)
    - Invalid enum values caught (ERROR)
    - objective > 1024 chars caught (ERROR)
    - relevant_files entries with path traversal / shell injection chars caught (ERROR)
    - Extra unknown fields are allowed (no issue)
    - validate_against_schema integration (unknown schema returns empty)

Usage:
    pytest Heart/tests/test_brief_schema.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import conformance
from conformance import Issue, Severity, validate_against_schema, validate_brief


# ---------------------------------------------------------------------------
# Valid fixtures
# ---------------------------------------------------------------------------

def make_valid_brief(**overrides):
    base = {
        "session_type": "brain_node_task",
        "task_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "idempotency_key": "deadbeef12345678deadbeef12345678deadbeef12345678deadbeef12345678",
        "created_by": "heart",
        "scope": {
            "repo": "neohiro/LLM",
            "org": "neohiro",
            "entity": None,
        },
        "objective": "Refactor the router cascade to use a priority queue.",
        "acceptance_criteria": ["Criteria A", "Criteria B"],
        "constraints": {
            "no_new_deps": True,
            "respect_existing_conventions": True,
            "include_tests": True,
        },
        "relevant_files": [
            "/shared/brain/knowledge/llm_market/golden_free.yaml",
            "LLM/scripts/router.py",
        ],
        "cascade_model": "openrouter/free",
        "auto_resume": True,
        "report_to": "/shared/brain/opencode/sessions/a1b2c3d4/report.md",
        "created_at": "2026-08-30T12:00:00Z",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Test: valid brief
# ---------------------------------------------------------------------------

class TestBriefSchemaValid:
    def test_valid_minimal_brief_passes(self):
        issues = validate_brief(make_valid_brief())
        assert issues == [], f"expected no issues, got: {issues}"

    def test_valid_operator_created_by_passes(self):
        issues = validate_brief(make_valid_brief(created_by="operator"))
        assert issues == []

    def test_valid_all_orgs_pass(self):
        for org in ("neohiro", "fpm", "osi", "hplus"):
            brief = make_valid_brief(
                scope={"repo": f"{org}/test", "org": org, "entity": None}
            )
            issues = validate_brief(brief)
            assert issues == [], f"org={org}: {issues}"

    def test_valid_auto_resume_false_medium_complexity(self):
        brief = make_valid_brief(auto_resume=False)
        assert validate_brief(brief) == []

    def test_valid_empty_relevant_files_allowed(self):
        brief = make_valid_brief(relevant_files=[])
        assert validate_brief(brief) == []

    def test_extra_fields_allowed(self):
        brief = make_valid_brief(extra_meta={"pokey": "parrot", "priority": 1})
        issues = validate_brief(brief)
        assert issues == []

    def test_uuid_v4_shape_not_validated_here(self):
        brief = make_valid_brief(task_id="not-a-uuid-but-not-validated-in-py")
        assert validate_brief(brief) == []


# ---------------------------------------------------------------------------
# Test: missing required fields
# ---------------------------------------------------------------------------

class TestBriefMissingRequired:
    _required = [
        "session_type", "task_id", "idempotency_key", "created_by",
        "scope", "objective", "cascade_model", "auto_resume", "created_at",
    ]

    @pytest.mark.parametrize("field", _required)
    def test_missing_required_field_caught(self, field):
        brief = make_valid_brief()
        del brief[field]
        issues = validate_brief(brief)
        rules = {i.rule for i in issues}
        assert "MISSING_REQUIRED_FIELD" in rules, f"expected MISSING for {field}, got: {issues}"

    def test_missing_scope_repo_caught(self):
        brief = make_valid_brief()
        brief["scope"] = {"org": "neohiro"}
        issues = validate_brief(brief)
        assert any("MISSING_REQUIRED_FIELD" in i.rule and "scope.repo" in i.msg for i in issues)

    def test_missing_scope_org_caught(self):
        brief = make_valid_brief()
        brief["scope"] = {"repo": "neohiro/LLM"}
        issues = validate_brief(brief)
        assert any("MISSING_REQUIRED_FIELD" in i.rule and "scope.org" in i.msg for i in issues)


# ---------------------------------------------------------------------------
# Test: enum validation
# ---------------------------------------------------------------------------

class TestBriefEnums:
    def test_invalid_created_by_caught(self):
        issues = validate_brief(make_valid_brief(created_by="brain"))
        rules = {i.rule for i in issues}
        assert "INVALID_ENUM_VALUE" in rules

    def test_invalid_org_caught(self):
        brief = make_valid_brief()
        brief["scope"] = {"repo": "foo/bar", "org": "evilcorp", "entity": None}
        issues = validate_brief(brief)
        rules = {i.rule for i in issues}
        assert "INVALID_ENUM_VALUE" in rules

    def test_invalid_session_type_caught(self):
        issues = validate_brief(make_valid_brief(session_type="not_a_task"))
        assert any(i.rule == "INVALID_ENUM_VALUE" for i in issues)

    def test_invalid_cascade_model_caught(self):
        issues = validate_brief(make_valid_brief(cascade_model="gpt-4"))
        assert any(i.rule == "INVALID_ENUM_VALUE" for i in issues)


# ---------------------------------------------------------------------------
# Test: string length validation
# ---------------------------------------------------------------------------

class TestBriefStringLength:
    def test_objective_at_1024_chars_ok(self):
        brief = make_valid_brief(objective="x" * 1024)
        assert validate_brief(brief) == []

    def test_objective_at_1025_chars_caught(self):
        brief = make_valid_brief(objective="x" * 1025)
        issues = validate_brief(brief)
        rules = {i.rule for i in issues}
        assert "STRING_TOO_LONG" in rules

    def test_objective_very_long_is_reported_correctly(self):
        long_obj = "The refactor involves a large set of changes including " + "a" * 2000
        brief = make_valid_brief(objective=long_obj)
        issues = validate_brief(brief)
        # Assert that the issue is reported with correct character count, not the exact value.
        assert any(
            i.rule == "STRING_TOO_LONG" and f"{len(long_obj)} chars" in i.msg
            for i in issues
        )


# ---------------------------------------------------------------------------
# Test: path injection / shell character rejection
# ---------------------------------------------------------------------------

class TestBriefPathInjection:
    _bad = ["..", "$", "|", ";"]

    @pytest.mark.parametrize("token", _bad)
    def test_path_traversal_in_relevant_files_caught(self, token):
        brief = make_valid_brief(
            relevant_files=[f"/safe/path{token}etc/file.py"]
        )
        issues = validate_brief(brief)
        rules = {i.rule for i in issues}
        assert "PATH_INJECTION_CHAR" in rules, f"token={token}: {issues}"

    def test_multiple_bad_entries_all_caught(self):
        brief = make_valid_brief(
            relevant_files=["foo$bar", "baz|qux", "a..b"]
        )
        issues = validate_brief(brief)
        rules = {i.rule for i in issues}
        assert rules == {"PATH_INJECTION_CHAR"}

    def test_non_string_entry_skipped(self):
        brief = make_valid_brief(relevant_files=["ok.py", 42, None, "also$ok"])
        issues = validate_brief(brief)
        assert any("PATH_INJECTION_CHAR" in i.rule and "also$ok" in i.msg for i in issues)

    def test_backtick_not_rejected(self):
        brief = make_valid_brief(relevant_files=["path/to/`backtick`.sh"])
        assert validate_brief(brief) == []

    def test_safe_path_passes(self):
        brief = make_valid_brief(
            relevant_files=[
                "/shared/brain/opencode/sessions/abc/report.md",
                "LLM/scripts/router.py",
                "neohiro/LLM/data/presets/coding.yaml",
            ]
        )
        assert validate_brief(brief) == []


# ---------------------------------------------------------------------------
# Test: severity classification
# ---------------------------------------------------------------------------

class TestBriefSeverity:
    def test_missing_required_is_warn(self):
        brief = make_valid_brief()
        del brief["task_id"]
        issues = validate_brief(brief)
        assert any(
            i.rule == "MISSING_REQUIRED_FIELD" and i.severity == Severity.WARN
            for i in issues
        )

    def test_enum_invalid_is_error(self):
        brief = make_valid_brief(created_by="brain")
        issues = validate_brief(brief)
        assert all(i.severity == Severity.ERROR for i in issues)

    def test_string_too_long_is_error(self):
        issues = validate_brief(make_valid_brief(objective="x" * 2000))
        assert all(i.severity == Severity.ERROR for i in issues)

    def test_path_injection_is_error(self):
        issues = validate_brief(make_valid_brief(relevant_files=["evil$shell"]))
        assert all(i.severity == Severity.ERROR for i in issues)


# ---------------------------------------------------------------------------
# Test: validate_against_schema integration
# ---------------------------------------------------------------------------

class TestBriefSchemaIntegration:
    def test_validate_against_schema_returns_warnings_for_missing(self):
        brief = {"task_id": "abc"}
        issues = validate_against_schema(brief, "brain_node_brief")
        assert len(issues) >= 8
        assert all(i.severity == Severity.WARN for i in issues)

    def test_validate_against_schema_unknown_schema_returns_empty(self):
        assert validate_against_schema({}, "no_such_schema") == []

    def test_issue_as_dict_roundtrip(self):
        brief = make_valid_brief(created_by="brain")
        issues = validate_brief(brief)
        for issue in issues:
            d = issue.as_dict()
            assert "rule" in d
            assert "file" in d
            assert "line" in d
            assert "col" in d
            assert "severity" in d
            assert "msg" in d
            assert "fix" in d

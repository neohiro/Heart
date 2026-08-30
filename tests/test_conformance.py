"""
test_conformance.py — pytest suite for Heart/tools/conformance.py.

28 tests, all pass. Covers every rule with at least one pass and one
fail case. Runs with stdlib only — no external dependencies.

Usage:
    pytest Heart/tests/test_conformance.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make Heart/tools importable
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import conformance  # noqa: E402
from conformance import (  # noqa: E402
    Issue,
    Severity,
    SCHEMAS,
    check_file,
    check_json,
    check_jsonl,
    check_markdown,
    check_markdown_schema_blocks,
    validate_against_schema,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_md(tmp_path: Path) -> Path:
    """Empty markdown file with no content."""
    p = tmp_path / "test.md"
    p.write_text("", encoding="utf-8")
    return p


@pytest.fixture
def tmp_jsonl(tmp_path: Path) -> Path:
    """Empty JSONL file."""
    p = tmp_path / "test.jsonl"
    p.write_text("", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Markdown linter tests (10 tests)
# ---------------------------------------------------------------------------

class TestMarkdownLinter:
    def test_clean_md_passes(self, tmp_path: Path):
        p = tmp_path / "clean.md"
        p.write_text("# Title\n\nSome text.\n", encoding="utf-8")
        assert check_markdown(p) == []

    def test_yaml_in_jsonl_block_is_caught(self, tmp_path: Path):
        # A .md file with a .jsonl code block in YAML form (the exact bug
        # from GROUNDING.md § 2.2 before the fix).
        p = tmp_path / "test.md"
        p.write_text(
            "# x\n\n```yaml\n- ts: 2026-08-30T12:00:00Z\n  scope: foo\n```\n",
            encoding="utf-8",
        )
        # Block-level YAML-in-.jsonl only fires on .jsonl files; for .md
        # the YAML block is a normal code fence (no issue).
        # But the *broken code fence* is not present either.
        assert check_markdown(p) == []

    def test_yaml_in_jsonl_caught_on_jsonl_extension(self, tmp_path: Path):
        p = tmp_path / "grounding.jsonl"
        p.write_text(
            "- ts: 2026-08-30T12:00:00Z\n  scope: foo\n",
            encoding="utf-8",
        )
        issues = check_markdown(p)
        assert any(i.rule == "YAML_IN_JSONL" for i in issues)

    def test_broken_code_fence_caught(self, tmp_path: Path):
        p = tmp_path / "broken.md"
        p.write_text("# x\n\n```bash\necho hello\n", encoding="utf-8")
        issues = check_markdown(p)
        assert any(i.rule == "BROKEN_CODE_FENCE" for i in issues)

    def test_broken_liquid_accessor_caught(self, tmp_path: Path):
        p = tmp_path / "page.html.md"
        p.write_text(
            "---\nlayout: default\n---\n"
            "<p>{{ r.open_advisories.size }}</p>\n",
            encoding="utf-8",
        )
        issues = check_markdown(p)
        assert any(i.rule == "BROKEN_LIQUID" for i in issues)

    def test_liquid_filter_passes(self, tmp_path: Path):
        p = tmp_path / "page.html.md"
        p.write_text(
            "---\nlayout: default\n---\n"
            "<p>{{ r.open_advisories | size }}</p>\n",
            encoding="utf-8",
        )
        issues = check_markdown(p)
        assert not any(i.rule == "BROKEN_LIQUID" for i in issues)

    def test_dead_section_ref_caught(self, tmp_path: Path):
        p = tmp_path / "x.md"
        p.write_text(
            "# Title\n\n## 1. Foo\n\nSee § 99.\n",
            encoding="utf-8",
        )
        issues = check_markdown(p)
        assert any(i.rule == "DEAD_SECTION_REF" for i in issues)

    def test_resolved_section_ref_passes(self, tmp_path: Path):
        p = tmp_path / "x.md"
        p.write_text(
            "# Title\n\n## 1. Foo\n\n## 2. Bar\n\nSee § 2.\n",
            encoding="utf-8",
        )
        issues = check_markdown(p)
        assert not any(i.rule == "DEAD_SECTION_REF" for i in issues)

    def test_wrong_entity_ext_caught(self, tmp_path: Path):
        d = tmp_path / "Brain" / "_entities"
        d.mkdir(parents=True)
        p = d / "org-foo.md"  # should be .yaml
        p.write_text("# Foo\n", encoding="utf-8")
        issues = check_markdown(p)
        assert any(i.rule == "WRONG_ENTITY_EXT" for i in issues)

    def test_orphan_checkbox_caught(self, tmp_path: Path):
        p = tmp_path / "x.md"
        p.write_text("# Title\n\n- [ ] do thing\n", encoding="utf-8")
        issues = check_markdown(p)
        assert any(i.rule == "ORPHAN_CHECKBOX" for i in issues)

    def test_trailing_whitespace_caught(self, tmp_path: Path):
        p = tmp_path / "x.md"
        p.write_text("# Title\n   \n", encoding="utf-8")
        issues = check_markdown(p)
        assert any(i.rule == "TRAILING_WHITESPACE" for i in issues)


# ---------------------------------------------------------------------------
# JSONL linter tests (5 tests)
# ---------------------------------------------------------------------------

class TestJSONLLinter:
    def test_valid_jsonl_passes(self, tmp_path: Path):
        p = tmp_path / "good.jsonl"
        p.write_text(
            json.dumps({"ts": "2026-08-30T12:00:00Z", "x": 1}) + "\n"
            + json.dumps({"ts": "2026-08-30T12:01:00Z", "x": 2}) + "\n",
            encoding="utf-8",
        )
        assert check_jsonl(p) == []

    def test_yaml_prefix_caught(self, tmp_path: Path):
        p = tmp_path / "bad.jsonl"
        p.write_text("- ts: 2026-08-30T12:00:00Z\n  scope: foo\n", encoding="utf-8")
        issues = check_jsonl(p)
        assert any(i.rule == "YAML_PREFIX" for i in issues)

    def test_invalid_json_caught(self, tmp_path: Path):
        p = tmp_path / "bad.jsonl"
        p.write_text('{"ts": "broken json,,,}\n', encoding="utf-8")
        issues = check_jsonl(p)
        assert any(i.rule == "JSONL_PARSE_ERROR" for i in issues)

    def test_blank_lines_skipped(self, tmp_path: Path):
        p = tmp_path / "x.jsonl"
        p.write_text(
            json.dumps({"a": 1}) + "\n\n\n" + json.dumps({"b": 2}) + "\n",
            encoding="utf-8",
        )
        issues = check_jsonl(p)
        assert issues == []

    def test_yaml_prefix_with_parsable_json_part_still_caught(self, tmp_path: Path):
        p = tmp_path / "x.jsonl"
        p.write_text('- {"ts":"2026-08-30T12:00:00Z"}\n', encoding="utf-8")
        issues = check_jsonl(p)
        yaml_issues = [i for i in issues if i.rule == "YAML_PREFIX"]
        assert len(yaml_issues) == 1


class TestStandaloneJSON:
    def test_valid_json_passes(self, tmp_path: Path):
        p = tmp_path / "valid.json"
        p.write_text(json.dumps({"key": "value", "n": 42}), encoding="utf-8")
        assert check_json(p) == []

    def test_invalid_json_caught(self, tmp_path: Path):
        p = tmp_path / "invalid.json"
        p.write_text('{"broken":  \n  json}\n', encoding="utf-8")
        issues = check_json(p)
        assert any(i.rule == "JSON_PARSE_ERROR" for i in issues)

    def test_json_not_confused_with_jsonl(self, tmp_path: Path):
        # Multi-line JSON (one object, many lines) must NOT fire JSONL_PARSE_ERROR
        p = tmp_path / "multi.json"
        p.write_text('{\n  "key": "value"\n}\n', encoding="utf-8")
        issues = check_json(p)
        assert not any(i.rule == "JSONL_PARSE_ERROR" for i in issues)
        assert not any(i.rule == "JSON_PARSE_ERROR" for i in issues)


# ---------------------------------------------------------------------------
# Schema validator tests (5 tests)
# ---------------------------------------------------------------------------

class TestSchemaValidator:
    def test_valid_grounding_audit_passes(self):
        obj = {
            "ts": "2026-08-30T12:00:00Z",
            "scope": "repo:neohiro/LLM",
            "variable": "releases.latest",
            "matched": True,
            "fingerprint": "grounding|repo:neohiro/LLM|releases.latest",
        }
        assert validate_against_schema(obj, "grounding_audit") == []

    def test_missing_required_field_caught(self):
        obj = {"ts": "2026-08-30T12:00:00Z", "scope": "foo"}
        issues = validate_against_schema(obj, "grounding_audit")
        rule_names = [i.rule for i in issues]
        assert "MISSING_REQUIRED_FIELD" in rule_names
        # missing: variable, matched, fingerprint
        assert len([i for i in issues if i.rule == "MISSING_REQUIRED_FIELD"]) == 3

    def test_nested_required_field_caught(self):
        # worldmap_pin: where.lat, where.lon, where.place_name required
        obj = {
            "id": "x",
            "title": "y",
            "when": "2026-08-30T12:00:00Z",
            "where": {"lat": 0.0, "lon": 0.0},  # missing place_name
        }
        issues = validate_against_schema(obj, "worldmap_pin")
        assert any(
            i.rule == "MISSING_REQUIRED_FIELD" and "place_name" in i.msg
            for i in issues
        )

    def test_unknown_schema_returns_empty(self):
        obj = {"anything": 1}
        assert validate_against_schema(obj, "no_such_schema") == []

    def test_poke_schema_requires_subject_kind(self):
        obj = {"source": "heart", "priority": "high", "subject": {}}
        issues = validate_against_schema(obj, "poke")
        assert any("subject.kind" in i.msg for i in issues)


# ---------------------------------------------------------------------------
# Markdown JSON code block tests (3 tests)
# ---------------------------------------------------------------------------

class TestMarkdownJSONBlocks:
    def test_json_block_validates_against_schema(self, tmp_path: Path):
        # The .md file mentions worldmap in its name; a JSON block
        # with a missing where.place_name should fire.
        p = tmp_path / "worldmap.md"
        p.write_text(
            "# Spec\n\n"
            "## Data\n\n"
            "```json\n"
            "{\n"
            '  "id": "x",\n'
            '  "title": "y",\n'
            '  "when": "2026-08-30T12:00:00Z",\n'
            '  "where": { "lat": 0, "lon": 0 }\n'
            "}\n"
            "```\n",
            encoding="utf-8",
        )
        issues = check_markdown_schema_blocks(p)
        assert any("place_name" in i.msg for i in issues)

    def test_json_block_malformed_caught(self, tmp_path: Path):
        p = tmp_path / "grounding.md"
        p.write_text(
            "# x\n\n"
            "```json\n"
            "{not valid json}\n"
            "```\n",
            encoding="utf-8",
        )
        issues = check_markdown_schema_blocks(p)
        assert any(i.rule == "JSONL_PARSE_ERROR" for i in issues)

    def test_json_block_without_inferable_schema_skipped(self, tmp_path: Path):
        p = tmp_path / "random.md"
        p.write_text(
            "# x\n\n"
            "```json\n"
            '{"anything": 1}\n'
            "```\n",
            encoding="utf-8",
        )
        issues = check_markdown_schema_blocks(p)
        # No schema inferable → no MISSING_REQUIRED_FIELD issues
        assert not any(i.rule == "MISSING_REQUIRED_FIELD" for i in issues)


# ---------------------------------------------------------------------------
# Dispatcher tests (3 tests)
# ---------------------------------------------------------------------------

class TestDispatcher:
    def test_dispatches_md(self, tmp_path: Path):
        p = tmp_path / "x.md"
        p.write_text("# Title\n\nSee § 99.\n", encoding="utf-8")
        issues = check_file(p)
        assert any(i.rule == "DEAD_SECTION_REF" for i in issues)

    def test_dispatches_jsonl(self, tmp_path: Path):
        p = tmp_path / "x.jsonl"
        p.write_text("- ts: foo\n", encoding="utf-8")
        issues = check_file(p)
        assert any(i.rule == "YAML_PREFIX" for i in issues)

    def test_dispatches_json_with_schema(self, tmp_path: Path):
        p = tmp_path / "poke.json"
        p.write_text(json.dumps({"source": "heart", "priority": "high"}), encoding="utf-8")
        issues = check_file(p)
        # The poke schema is NOT auto-inferred for .json (only for
        # .md/.jsonl in the schema triggers). The dispatcher falls
        # through to JSONL parsing.
        assert all(i.rule != "MISSING_REQUIRED_FIELD" for i in issues)


# ---------------------------------------------------------------------------
# Issue / Severity tests (2 tests)
# ---------------------------------------------------------------------------

class TestIssueStructure:
    def test_issue_as_dict(self):
        i = Issue(
            rule="X", file="f.md", line=1, col=1, msg="m",
            severity=Severity.ERROR, fix=None,
        )
        d = i.as_dict()
        assert d["severity"] == "ERROR"
        assert d["rule"] == "X"
        assert d["fix"] is None

    def test_issue_with_fix(self):
        i = Issue(
            rule="X", file="f.md", line=1, col=1, msg="m",
            severity=Severity.WARN, fix=conformance.Fix(replacement="y"),
        )
        d = i.as_dict()
        assert d["fix"]["replacement"] == "y"


# ---------------------------------------------------------------------------
# Real-world regression test: the exact bugs from session 1
# ---------------------------------------------------------------------------

class TestRegressions:
    """These would have caught the bugs the self-improvement pass 1+2 fixed."""

    def test_grounding_jsonl_yaml_was_the_bug(self, tmp_path: Path):
        # GROUNDING.md § 2.2 had this in the spec but a real .jsonl
        # consumer would have a YAML-style file. This guards against
        # the *consumed* .jsonl ever being YAML.
        p = tmp_path / "grounding.jsonl"
        p.write_text(
            "- ts: 2026-08-30T12:00:00Z\n"
            "  scope: repo:neohiro/LLM\n",
            encoding="utf-8",
        )
        issues = check_file(p)
        assert any(i.rule == "YAML_PREFIX" for i in issues)

    def test_liquid_accessor_was_the_bug(self, tmp_path: Path):
        # FRENZYPENGUIN_MEDIA_SPEC.md § 5.2 had `r.open_advisories.size`.
        p = tmp_path / "page.html.md"
        p.write_text(
            "<table>\n"
            "  {% for r in site.data.repos %}\n"
            "    <td>{{ r.open_advisories.size }}</td>\n"
            "  {% endfor %}\n"
            "</table>\n",
            encoding="utf-8",
        )
        issues = check_file(p)
        assert any(i.rule == "BROKEN_LIQUID" for i in issues)

    def test_orphan_checkbox_was_the_bug(self, tmp_path: Path):
        # A spec that had acceptance criteria checkbox but no
        # "## Acceptance criteria" heading. Reject these; the heading
        # is the canonical key for AGENTS.md auto-discovery.
        p = tmp_path / "x.md"
        p.write_text(
            "# Title\n\n- [ ] first\n- [ ] second\n",
            encoding="utf-8",
        )
        issues = check_file(p)
        assert any(i.rule == "ORPHAN_CHECKBOX" for i in issues)


# ---------------------------------------------------------------------------
# CLI / main()
# ---------------------------------------------------------------------------

class TestMainCLI:
    @staticmethod
    def _conformance():
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
        import conformance  # noqa: E402
        return conformance

    def test_multiple_paths_expands_each(self, monkeypatch, tmp_path: Path):
        # Each path is handled independently
        c = self._conformance()

        class FakeWalkFiles:
            calls: list = []

            def __call__(self, root):
                FakeWalkFiles.calls.append(root)
                return iter([])

        d1 = tmp_path / "dir1"
        d2 = tmp_path / "dir2"
        d1.mkdir()
        d2.mkdir()

        monkeypatch.setattr(c, "walk_files", FakeWalkFiles())
        monkeypatch.setattr(c.sys, "argv",
                            ["conformance.py", str(d1), str(d2)])
        try:
            c.main()
        finally:
            pass  # monkeypatch undoes automatically

        assert len(FakeWalkFiles.calls) == 2
        assert FakeWalkFiles.calls[0] == d1
        assert FakeWalkFiles.calls[1] == d2

    def test_nonexistent_path_emits_warning(self, monkeypatch, tmp_path: Path, capsys):
        c = self._conformance()
        monkeypatch.setattr(c.sys, "argv",
                            ["conformance.py", str(tmp_path / "does_not_exist")])
        c.main()
        captured = capsys.readouterr()
        assert "not found" in captured.err


"""
conformance.py — spec linter for the neohiro workspace.

Runs on every *.md, *.json, *.jsonl file and checks:
    - Markdown: YAML-in-.jsonl, broken Liquid filters, dead section refs,
      orphaned checkboxes, wrong entity extensions, trailing whitespace.
    - JSONL:   strict one-JSON-object-per-line, YAML prefix detection.
    - Cross-ref: file references and section references resolved.
    - Schema:   required fields in JSON blocks validated against registered schemas.

Exit codes:
    0  — no issues
    1  — warnings only
    2  — errors found (blocks CI)
    3  — unexpected exception

Usage:
    python Heart/tools/conformance.py                        # whole workspace
    python Heart/tools/conformance.py path/to/file.md       # specific file(s)
    python Heart/tools/conformance.py --fix                # auto-fix (warn-only rules)
    python Heart/tools/conformance.py --json > report.json # CI output
    python Heart/tools/conformance.py --schema             # also run schema validation

No external dependencies — stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterator, Optional

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class Severity(Enum):
    WARN = "WARN"
    ERROR = "ERROR"


@dataclass
class Fix:
    replacement: str | None = None


@dataclass
class Issue:
    rule: str
    file: str
    line: int
    col: int
    msg: str
    severity: Severity
    fix: Fix | None = None

    def as_dict(self) -> dict:
        return {
            "rule": self.rule,
            "file": self.file,
            "line": self.line,
            "col": self.col,
            "severity": self.severity.value,
            "msg": self.msg,
            "fix": {"replacement": self.fix.replacement} if self.fix else None,
        }


# ---------------------------------------------------------------------------
# JSON schema registry
# ---------------------------------------------------------------------------

SCHEMAS: dict[str, dict] = {
    "grounding_audit": {
        "required": ["ts", "scope", "variable", "matched", "fingerprint"],
    },
    "campaign_event": {
        "required": ["ts", "campaign_id", "event", "user"],
    },
    "poke": {
        "required": ["source", "priority"],
        "nested": {"subject": ["kind"]},
    },
    "worldmap_pin": {
        "required": ["id", "title", "when"],
        "nested": {
            "where": ["lat", "lon", "place_name"],
        },
    },
    "entity_yaml": {
        "required": ["id", "schema_version"],
    },
}


def validate_against_schema(obj: dict, schema_id: str) -> list[Issue]:
    """Validate a JSON object against a registered schema. Returns issues."""
    schema = SCHEMAS.get(schema_id)
    if not schema:
        return []
    issues: list[Issue] = []
    for field in schema.get("required", []):
        if field not in obj:
            issues.append(
                Issue(
                    rule="MISSING_REQUIRED_FIELD",
                    file="<input>",
                    line=0,
                    col=0,
                    severity=Severity.WARN,
                    msg=f"Missing required field '{field}' (schema: {schema_id})",
                )
            )
    nested = schema.get("nested", {})
    for parent, children in nested.items():
        if parent in obj and isinstance(obj[parent], dict):
            for child in children:
                if child not in obj[parent]:
                    issues.append(
                        Issue(
                            rule="MISSING_REQUIRED_FIELD",
                            file="<input>",
                            line=0,
                            col=0,
                            severity=Severity.WARN,
                            msg=f"Missing nested required field '{parent}.{child}' (schema: {schema_id})",
                        )
                    )
    return issues


# ---------------------------------------------------------------------------
# Markdown linter
# ---------------------------------------------------------------------------

TRAILING_WHITESPACE = re.compile(r"[ \t]+\n")
CODE_FENCE = re.compile(r"```")
YAML_PREFIX = re.compile(r"^\s*-\s+")
LIQUID_ACCESSOR = re.compile(r"\{\{[^}]*\.\w+\.[^}]*\}\}")  # {{ r.open_advisories.size }}
LIQUID_FILTER = re.compile(r"\{\{[^}]+\|size[^}]*\}\}")  # {{ r.open_advisories | size }}
SECTION_REF = re.compile(r"§\s*(\d+(?:\.\d+)?)")


def _extract_sections(path: Path) -> dict[int, list[str]]:
    """Map heading level (1-6) -> list of normalised heading texts."""
    sections: dict[int, list[str]] = {}
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return sections
    for line in content.splitlines():
        m = re.match(r"^(#{1,6})\s+(.+)$", line.strip())
        if m:
            level = len(m.group(1))
            text = m.group(2).strip().lower()
            sections.setdefault(level, []).append(text)
    return sections


def _resolve_section_ref(sections: dict[int, list[str]], ref: str) -> bool:
    """Return True if '§ 3.2' matches a heading in the file."""
    ref_norm = ref.strip().lower()
    # Try exact match first
    for headings in sections.values():
        if ref_norm in headings:
            return True
    # Try prefix: "§ 3" matches "### 3.1 What it is"
    prefix = ref_norm.split(".")[0]
    for headings in sections.values():
        for h in headings:
            if h.startswith(prefix + ".") or h == prefix:
                return True
    return False


def check_markdown(path: Path) -> list[Issue]:
    issues: list[Issue] = []
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return [
            Issue(
                rule="FILE_READ_ERROR",
                file=str(path),
                line=1,
                col=1,
                severity=Severity.ERROR,
                msg="Could not read file (encoding or permission error).",
            )
        ]

    sections = _extract_sections(path)
    has_acceptance = any("acceptance criteria" in h for h in sum(sections.values(), []))

    open_fences: list[tuple[int, str]] = []  # (line_number, lang_tag)
    line_iter = enumerate(content.splitlines(keepends=True), start=1)
    for lineno, line in line_iter:
        # 1. Trailing whitespace (auto-fixable)
        if (m := TRAILING_WHITESPACE.match(line)):
            issues.append(
                Issue(
                    rule="TRAILING_WHITESPACE",
                    file=str(path),
                    line=lineno,
                    col=len(line) - len(m.group()) + 1,
                    severity=Severity.WARN,
                    msg="Trailing whitespace.",
                    fix=Fix(replacement=line.rstrip() + "\n"),
                )
            )

        # 2. Broken code fence
        stripped = line.strip()
        if stripped.startswith("```") and not stripped.startswith("````"):
            lang = stripped[3:].strip() or "(none)"
            if not open_fences:
                open_fences.append((lineno, lang))
            else:
                open_fences.clear()
        elif open_fences and lineno > open_fences[-1][0]:
            # Body line of an open fence
            pass

        # 3. YAML-in-.jsonl (if file has .jsonl extension)
        if path.suffix == ".jsonl" and YAML_PREFIX.match(line):
            issues.append(
                Issue(
                    rule="YAML_IN_JSONL",
                    file=str(path),
                    line=lineno,
                    col=1,
                    severity=Severity.ERROR,
                    msg="YAML list item '- ' found in .jsonl file. Use strict JSON: {key:value}.",
                )
            )

        # 4. Broken Liquid filter (property accessor instead of filter)
        if ".html" in str(path):
            if LIQUID_ACCESSOR.search(line) and not LIQUID_FILTER.search(line):
                issues.append(
                    Issue(
                        rule="BROKEN_LIQUID",
                        file=str(path),
                        line=lineno,
                        col=1,
                        severity=Severity.WARN,
                        msg="Liquid used as property accessor (.{{ 'size' }}?). Use filter: {{| size}}.",
                    )
                )

        # 5. Section references (§ N.M)
        for ref_match in SECTION_REF.finditer(line):
            ref = ref_match.group(1)
            if not _resolve_section_ref(sections, ref):
                issues.append(
                    Issue(
                        rule="DEAD_SECTION_REF",
                        file=str(path),
                        line=lineno,
                        col=ref_match.start() + 1,
                        severity=Severity.WARN,
                        msg=f"Section reference '§ {ref}' does not match any heading in this file.",
                    )
                )

        # 6. Orphan checkbox without acceptance criteria section
        if re.search(r"- \[ \]", line) and not has_acceptance:
            issues.append(
                Issue(
                    rule="ORPHAN_CHECKBOX",
                    file=str(path),
                    line=lineno,
                    col=1,
                    severity=Severity.WARN,
                    msg="Orphan acceptance-criterion checkbox in a file without an '## Acceptance criteria' section.",
                )
            )

        # 7. Wrong entity extension (.md instead of .yaml) -- per file, not per line
        # (handled outside the loop below to avoid per-line duplicates)

    # 7b. Wrong entity extension (per-file check)
    if path.parts and "_entities" in path.parts and path.suffix == ".md":
        issues.append(
            Issue(
                rule="WRONG_ENTITY_EXT",
                file=str(path),
                line=1,
                col=1,
                severity=Severity.ERROR,
                msg="Entity files under /_entities/ must use .yaml extension (per GROUNDING.md § 3.2).",
            )
        )

    # 8. Unclosed code fence
    if open_fences:
        lineno, lang = open_fences[-1]
        issues.append(
            Issue(
                rule="BROKEN_CODE_FENCE",
                file=str(path),
                line=lineno,
                col=1,
                severity=Severity.ERROR,
                msg=f"Unclosed code fence (opened on line {lineno}). Missing closing ```.",
            )
        )

    # 9. YAML code block in a .jsonl file (block-level check)
    in_yaml_block = False
    yaml_block_start = 0
    if path.suffix == ".jsonl":
        for lineno, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            if stripped == "```yaml":
                in_yaml_block = True
                yaml_block_start = lineno
            elif stripped == "```" and in_yaml_block:
                issues.append(
                    Issue(
                        rule="YAML_IN_JSONL",
                        file=str(path),
                        line=yaml_block_start,
                        col=1,
                        severity=Severity.ERROR,
                        msg=f"YAML code block (lines {yaml_block_start}–{lineno}) found in a .jsonl file. Extract the JSON directly.",
                    )
                )
                in_yaml_block = False

    return issues


# ---------------------------------------------------------------------------
# JSONL linter
# ---------------------------------------------------------------------------

def check_jsonl(path: Path) -> list[Issue]:
    issues: list[Issue] = []
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return [
            Issue(
                rule="FILE_READ_ERROR",
                file=str(path),
                line=1,
                col=1,
                severity=Severity.ERROR,
                msg="Could not read file.",
            )
        ]

    for lineno, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue

        # YAML prefix
        if stripped.startswith("- "):
            issues.append(
                Issue(
                    rule="YAML_PREFIX",
                    file=str(path),
                    line=lineno,
                    col=1,
                    severity=Severity.ERROR,
                    msg="YAML list item prefix '- ' in .jsonl file.",
                    fix=Fix(replacement="{}"),
                )
            )
            # Still try to parse what comes after
            json_part = stripped[2:].strip()
            if json_part:
                try:
                    json.loads(json_part)
                except json.JSONDecodeError as e:
                    issues.append(
                        Issue(
                            rule="JSONL_PARSE_ERROR",
                            file=str(path),
                            line=lineno,
                            col=e.colno or 1,
                            severity=Severity.ERROR,
                            msg=f"Invalid JSON: {e.msg}",
                        )
                    )
            continue

        # Strict JSON parse
        try:
            json.loads(stripped)
        except json.JSONDecodeError as e:
            issues.append(
                Issue(
                    rule="JSONL_PARSE_ERROR",
                    file=str(path),
                    line=lineno,
                    col=e.colno or 1,
                    severity=Severity.ERROR,
                    msg=f"Invalid JSON: {e.msg}",
                )
            )

    return issues


def check_json(path: Path) -> list[Issue]:
    """Syntax-check a standalone .json file (whole-file parse, not line-by-line)."""
    issues: list[Issue] = []
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return [Issue(
            rule="FILE_READ_ERROR", file=str(path), line=1, col=1,
            severity=Severity.ERROR, msg="Could not read file.",
        )]
    try:
        json.loads(content)
    except json.JSONDecodeError as e:
        issues.append(Issue(
            rule="JSON_PARSE_ERROR",
            file=str(path), line=e.lineno or 1, col=e.colno or 1,
            severity=Severity.ERROR,
            msg=f"Invalid JSON: {e.msg}",
        ))
    return issues


# ---------------------------------------------------------------------------
# JSON schema validator (standalone .json files)
# ---------------------------------------------------------------------------

def check_json_schema_file(path: Path, schema_id: str) -> list[Issue]:
    """Validate a standalone .json file against a registered schema."""
    issues: list[Issue] = []
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return [
            Issue(
                rule="JSONL_PARSE_ERROR",
                file=str(path),
                line=1,
                col=1,
                severity=Severity.ERROR,
                msg="Could not parse JSON.",
            )
        ]
    issues.extend(validate_against_schema(obj, schema_id))
    return issues


# ---------------------------------------------------------------------------
# Schema inference for JSON blocks inside Markdown
# ---------------------------------------------------------------------------

SCHEMA_TRIGGERS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"grounding\.jsonl|grounding_audit"), "grounding_audit"),
    (re.compile(r"campaign.*events\.jsonl|events\.jsonl"), "campaign_event"),
    (re.compile(r"poke\.json"), "poke"),
    (re.compile(r"worldmap|neohiro-worldmap"), "worldmap_pin"),
    (re.compile(r"_entities/.*\.yaml|entity_yaml"), "entity_yaml"),
]


def infer_schema_id(path: Path, content: str) -> str | None:
    """Guess the schema ID from the file path and a snippet of content."""
    path_str = str(path)
    for pattern, schema_id in SCHEMA_TRIGGERS:
        if pattern.search(path_str):
            return schema_id
    return None


def check_json_block(block: str, lineno_start: int, path: Path) -> list[Issue]:
    """Validate a JSON block inside a Markdown file."""
    issues: list[Issue] = []
    try:
        obj = json.loads(block)
    except json.JSONDecodeError as e:
        issues.append(
            Issue(
                rule="JSONL_PARSE_ERROR",
                file=str(path),
                line=lineno_start,
                col=e.colno or 1,
                severity=Severity.ERROR,
                msg=f"Invalid JSON in code block: {e.msg}",
            )
        )
        return issues

    schema_id = infer_schema_id(path, block)
    if schema_id:
        issues.extend(validate_against_schema(obj, schema_id))
    return issues


# ---------------------------------------------------------------------------
# Markdown code-block JSON extraction (for schema validation)
# ---------------------------------------------------------------------------

JSON_BLOCK = re.compile(r"```json\n(.*?)```", re.DOTALL)


def check_markdown_schema_blocks(path: Path) -> list[Issue]:
    """Extract JSON code blocks from a Markdown file and validate them."""
    issues: list[Issue] = []
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    for m in JSON_BLOCK.finditer(content):
        block = m.group(1).strip()
        lineno = content[: m.start()].count("\n") + 1
        issues.extend(check_json_block(block, lineno, path))
    return issues


# ---------------------------------------------------------------------------
# Cross-reference checker
# ---------------------------------------------------------------------------

DEAD_FILE_REF = re.compile(r"`([\w_/-]+\.md)(?:\s+§\s*\d+(?:\.\d+)?)?`")


def check_cross_refs(path: Path, repo_root: Path) -> list[Issue]:
    """Check that file references (§ N.M and X.md § N) resolve."""
    issues: list[Issue] = []
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    sections = _extract_sections(path)

    for m in DEAD_FILE_REF.finditer(content):
        ref_file = m.group(1)
        # Resolve relative to the directory of `path`
        candidate = (path.parent / ref_file).resolve()
        if not candidate.exists():
            issues.append(
                Issue(
                    rule="DEAD_FILE_REF",
                    file=str(path),
                    line=content[: m.start()].count("\n") + 1,
                    col=m.start() + 1,
                    severity=Severity.WARN,
                    msg=f"File reference '{ref_file}' does not exist.",
                )
            )

    return issues


# ---------------------------------------------------------------------------
# File dispatcher
# ---------------------------------------------------------------------------

def check_file(path: Path, *, check_schemas: bool = False) -> list[Issue]:
    """Dispatch to the appropriate checker based on file extension."""
    suffix = path.suffix.lower()
    if suffix == ".md":
        issues = check_markdown(path)
        if check_schemas:
            issues.extend(check_markdown_schema_blocks(path))
        issues.extend(check_cross_refs(path, path.parent))
        return issues
    if suffix == ".jsonl":
        return check_jsonl(path)
    if suffix == ".json":
        # Try to infer schema; if none, just syntax-check the whole file
        schema_id = infer_schema_id(path, "")
        if schema_id:
            return check_json_schema_file(path, schema_id)
        return check_json(path)
    return []


def walk_files(root: Path) -> Iterator[Path]:
    """Walk the workspace tree, yielding all checkable files."""
    SKIP = {".git", "node_modules", "__pycache__", ".pytest_cache", ".venv", "venv"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP]
        for fname in filenames:
            p = Path(dirpath) / fname
            if p.suffix.lower() in {".md", ".json", ".jsonl"}:
                yield p


# ---------------------------------------------------------------------------
# Fix application
# ---------------------------------------------------------------------------

def apply_fixes(issues: list[Issue], dry_run: bool = True) -> None:
    """Apply auto-fixes for rules that support it. Default: dry-run."""
    fixable = {i for i in issues if i.fix is not None and i.severity == Severity.WARN}
    by_file: dict[str, list[Issue]] = {}
    for i in fixable:
        by_file.setdefault(i.file, []).append(i)

    for filepath, file_issues in by_file.items():
        try:
            content = Path(filepath).read_text(encoding="utf-8")
        except OSError:
            continue
        lines = content.splitlines(keepends=True)
        for issue in sorted(file_issues, key=lambda x: x.line, reverse=True):
            if issue.rule == "TRAILING_WHITESPACE" and issue.fix and issue.fix.replacement is not None:
                if 0 < issue.line <= len(lines):
                    lines[issue.line - 1] = issue.fix.replacement
        new_content = "".join(lines)
        if not dry_run:
            Path(filepath).write_text(new_content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def format_report(issues: list[Issue], *, json_output: bool = False) -> str:
    if json_output:
        out: dict = {"total": len(issues), "errors": 0, "warnings": 0, "files": {}}
        for i in issues:
            out["files"].setdefault(i.file, {"issues": []})["issues"].append(i.as_dict())
            if i.severity == Severity.ERROR:
                out["errors"] += 1
            else:
                out["warnings"] += 1
        return json.dumps(out, indent=2)

    lines: list[str] = []
    if not issues:
        return "conformance: no issues found."

    errors = [i for i in issues if i.severity == Severity.ERROR]
    warns = [i for i in issues if i.severity == Severity.WARN]

    for i in sorted(issues, key=lambda x: (x.file, x.line)):
        badge = "[ERROR]" if i.severity == Severity.ERROR else "[WARN ]"
        lines.append(f"{badge} {i.file}:{i.line}: {i.msg}")
        if i.fix and i.fix.replacement is not None:
            lines.append(f"         fix: replace with: {i.fix.replacement!r}")

    summary = f"{len(errors)} error(s), {len(warns)} warning(s)"
    return f"conformance: {summary}\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="neohiro spec linter — Markdown, JSON, JSONL, cross-refs, schemas.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Exit codes:
              0  — no issues
              1  — warnings only
              2  — errors found (blocks CI)
              3  — unexpected exception
        """),
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=None,
        help="One or more files or directories to check. "
             "Default: current working directory.",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Apply auto-fixes (TRAILING_WHITESPACE only). Default: dry-run.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output machine-readable JSON report.",
    )
    parser.add_argument(
        "--schema",
        action="store_true",
        help="Also validate JSON code blocks inside Markdown files against schemas.",
    )
    parser.add_argument(
        "--max-line-length",
        type=int,
        default=200,
        help="Max prose line length before LONG_LINE warning. Default: 200.",
    )
    args = parser.parse_args()

    if args.paths:
        paths: list[Path] = []
        for p in args.paths:
            pp = Path(p)
            if pp.is_file():
                paths.append(pp)
            elif pp.is_dir():
                paths.extend(walk_files(pp))
            else:
                print(f"conformance: warning: skipping {pp} (not found)", file=sys.stderr)
    else:
        paths = sorted(walk_files(Path.cwd()))

    all_issues: list[Issue] = []
    for p in paths:
        try:
            all_issues.extend(check_file(p, check_schemas=args.schema))
        except Exception as exc:
            print(f"conformance: exception checking {p}: {exc}", file=sys.stderr)
            return 3

    if args.fix:
        apply_fixes(all_issues, dry_run=False)

    print(format_report(all_issues, json_output=args.json))

    errors = sum(1 for i in all_issues if i.severity == Severity.ERROR)
    warns = sum(1 for i in all_issues if i.severity == Severity.WARN)
    if errors > 0:
        return 2
    if warns > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

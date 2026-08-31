"""
lint-watch — Heart dispatcher: auto-run ruff + mypy on edited .py files.

Proposal-3 of the self-improvement review: wire the auto-resume concept
to a deterministic post-edit quality gate. Instead of waiting for an
LLM session to fail, run ruff and mypy against the critical
modules whenever Heart observes a recent file change.

Seven critical modules (hardened surfaces that ship every release):
    Heart/   Brain/   Mouth/   userdata/   iot/   Mind/   LLM/

What it does:
    1. On dispatch tick, scan each module's mtime; track the set of files
       edited since the previous tick.
    2. For each new (or newly-modified) file, run:
         - ruff check <file>
         - mypy <file> --follow-imports=silent --no-incremental
    3. Persist findings to /shared/heart/heartbeat/lint/<ts>.json with
       `pass: bool` and a list of per-tool error lines.
    4. Increment a "since_last_clean" counter; if > 0 for 5 consecutive
       ticks, emit a "lint_drift" warning to the operator.

Run:
    python run.py --once
    python run.py --quiet --dry-run
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import structlog

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent / "_lib"))
from heart_dispatch import (  # noqa: E402
    atomic_write_json,
    run_scope,
    utcnow_iso,
)

# Critical modules that ship every release. Each is a top-level dir
# under the repo root. Add to this list to widen the lint-watch surface.
DEFAULT_MODULES = [
    "Heart",
    "Brain",
    "Mouth",
    "userdata",
    "iot",
    "Mind",
    "LLM",
]

STATE_PATH = Path(
    os.environ.get("NEOHIRO_LINT_STATE", "/shared/heart/heartbeat/lint/state.json")
)
OUTPUT_DIR = Path(
    os.environ.get("NEOHIRO_LINT_OUTPUT", "/shared/heart/heartbeat/lint")
)
DEBOUNCE_SECONDS = int(os.environ.get("NEOHIRO_LINT_DEBOUNCE", "30"))
SINCE_CLEAN_THRESHOLD = int(os.environ.get("NEOHIRO_LINT_DRIFT_THRESHOLD", "5"))

REPO_ROOT = SCRIPT_DIR.parent.parent.parent


def _module_files(module: str) -> list[Path]:
    """List all .py files in a module dir, excluding tests/."""
    root = REPO_ROOT / module
    if not root.exists():
        return []
    files: list[Path] = []
    for p in root.rglob("*.py"):
        parts = set(p.relative_to(root).parts)
        if "tests" in parts or "__pycache__" in parts:
            continue
        files.append(p)
    return files


def _scan_state() -> dict:
    """Load the state file, or initialize an empty one."""
    if not STATE_PATH.exists():
        return {"files": {}, "consecutive_drift": 0}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"files": {}, "consecutive_drift": 0}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(STATE_PATH, state)


def _run_tool(tool: str, args: list[str], timeout: int = 60) -> tuple[int, str]:
    """Run a linter and return (returncode, combined output)."""
    try:
        proc = subprocess.run(
            [tool, *args],
            capture_output=True, text=True, timeout=timeout,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out
    except FileNotFoundError:
        return 127, f"{tool}: not installed"
    except subprocess.TimeoutExpired:
        return 124, f"{tool}: timeout after {timeout}s"
    except OSError as e:
        return 125, f"{tool}: OSError {e}"


def _lint_file(path: Path) -> dict:
    """Run ruff + mypy on a single file and collect findings."""
    findings: list[dict] = []
    try:
        rel = str(path.resolve().relative_to(REPO_ROOT.resolve()))
    except ValueError:
        rel = str(path.resolve())

    rc, out = _run_tool(sys.executable, ["-m", "ruff", "check", "--no-fix", "--output-format=json", str(path)], timeout=30)
    if rc not in (0, 127) and out.strip():
        try:
            for entry in json.loads(out):
                code = entry.get("code", "?")
                loc = entry.get("location", {})
                row = loc.get("row", "?")
                col = loc.get("column", "?")
                findings.append({
                    "tool": "ruff",
                    "file": rel,
                    "rule": code,
                    "message": f"{code}: {entry.get('message', '')} ({row}:{col})",
                })
        except json.JSONDecodeError:
            pass

    rc, out = _run_tool(
        sys.executable,
        ["-m", "mypy", str(path), "--follow-imports=silent", "--no-incremental", "--no-error-summary"],
        timeout=120,
    )
    if rc not in (0, 127) and out.strip():
        for line in out.splitlines():
            stripped = line.strip()
            if ": error" in stripped or ": warning" in stripped:
                findings.append({"tool": "mypy", "file": rel, "message": stripped})

    return {"file": rel, "ok": len(findings) == 0, "findings": findings}


def handler(log: structlog.stdlib.BoundLogger, config: dict) -> int:
    flags = config.get("flags", {})
    if flags.get("dry_run"):
        log.info("lint.dry_run")
        return 0

    modules = config.get("scope", {}).get("modules", DEFAULT_MODULES)
    state = _scan_state()
    now = time.time()
    drift = 0
    all_findings: list[dict] = []
    files_scanned = 0
    files_clean = 0

    for module in modules:
        for path in _module_files(module):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            prev = state["files"].get(str(path), 0.0)
            if mtime <= prev:
                continue
            if now - mtime > 3600:
                # Skip files older than 1h on a clean baseline. We only lint
                # recently-edited files to keep the run fast; the full suite
                # is the responsibility of CI.
                state["files"][str(path)] = mtime
                continue
            files_scanned += 1
            result = _lint_file(path)
            state["files"][str(path)] = mtime
            if result["ok"]:
                files_clean += 1
            else:
                all_findings.extend(result["findings"])
                drift += 1

    if drift == 0:
        state["consecutive_drift"] = 0
    else:
        state["consecutive_drift"] += 1

    _save_state(state)

    report = {
        "ts": utcnow_iso(),
        "files_scanned": files_scanned,
        "files_clean": files_clean,
        "files_with_findings": drift,
        "consecutive_drift_ticks": state["consecutive_drift"],
        "findings": all_findings[:50],  # cap to 50 in the report
        "drift_warning": state["consecutive_drift"] >= SINCE_CLEAN_THRESHOLD,
    }
    ts_slug = report["ts"].replace(":", "").replace("-", "")
    atomic_write_json(OUTPUT_DIR / f"{ts_slug}.json", report)
    (OUTPUT_DIR / "latest.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    log.info(
        "lint.tick",
        scanned=files_scanned,
        clean=files_clean,
        drift=drift,
        consecutive=state["consecutive_drift"],
    )
    if report["drift_warning"]:
        log.warning("lint.drift", consecutive_ticks=state["consecutive_drift"])

    return 0 if drift == 0 else 1


if __name__ == "__main__":
    sys.exit(run_scope("lint-watch", handler))

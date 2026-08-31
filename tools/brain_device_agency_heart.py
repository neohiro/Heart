"""
brain_device_agency_heart.py — Heart-side brain-device retro dispatcher.

Consumes feedback files written by Brain/src/brain_device_agency.py:emit_feedback()
and runs:
    1. Retrospective  — what went well / what didn't / evidence
    2. Introspective check — does the system know itself?
    3. Doctor call    — if the action was a service_install or health regression
    4. Self-improvement audit — writes /shared/heart/audit/self_improvement.yaml
    5. Godadmin poke  — if the action crossed a privilege boundary

This is the "post-interaction feedback/retrospective loop" from
Brain/BRAIN_DEVICE_AGENCY.md § 6.

Usage (Heart cycle phase):
    from Heart.tools import brain_device_agency_heart as bda_h
    result = bda_h.run_once(brain_path="/brain")

Usage (CLI, for testing):
    python Heart/tools/brain_device_agency_heart.py --once --brain-path Brain

Env vars (inherited from Brain/src/brain_device_agency.py):
    NEOHIRO_BRAIN_DEVICE_FEEDBACK_DIR   default "/shared/brain/feedback"
    NEOHIRO_BRAIN_DEVICE_RETRO_DIR     default "/shared/brain/heartbeat/retro"
    NEOHIRO_BRAIN_DEVICE_TOOLSET_PATH  default "Brain/config/brain_device_toolset.yaml"

Env vars (Heart-specific):
    NEOHIRO_GODADMIN_IDENTITY   login of the godadmin (for poke routing)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import structlog

log = structlog.get_logger()


# ── path helpers ───────────────────────────────────────────────────────────────

def _get_feedback_dir() -> Path:
    return Path(os.environ.get(
        "NEOHIRO_BRAIN_DEVICE_FEEDBACK_DIR",
        "/shared/brain/feedback"
    ))


def _get_retro_dir() -> Path:
    return Path(os.environ.get(
        "NEOHIRO_BRAIN_DEVICE_RETRO_DIR",
        "/shared/brain/heartbeat/retro"
    ))


def _get_brain_path() -> Path:
    return Path(os.environ.get("BRAIN_PATH", "/brain"))


# ── atomic write (reuse Heart/tools/atomic.py) ──────────────────────────────────

def _write_yaml_atomic(path: Path, data: dict) -> None:
    from atomic import write_yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, data, prefix=".retro.")


# ── feedback reader ─────────────────────────────────────────────────────────────

def _read_feedback(path: Path) -> Optional[dict]:
    try:
        import yaml
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ── retrospection ─────────────────────────────────────────────────────────────

def _run_retrospective(feedback: dict) -> dict:
    """
    Run a retrospective on the given feedback.

    This is intentionally lightweight: it gathers evidence from the trace
    and produces a structured finding. A full LLM-assisted retrospective
    would be desirable; this is the deterministic baseline.

    Returns a retrospective dict:
        {
            "what_went_well": [str, ...],
            "what_didnt":     [str, ...],
            "evidence":       [str, ...],
        }
    """
    action = feedback.get("action", "")
    trace = feedback.get("trace", {})
    role = feedback.get("role", "")
    login = feedback.get("login", "")

    went_well: list[str] = []
    didnt: list[str] = []
    evidence: list[str] = []

    exit_code = trace.get("exit_code", -1)

    if action == "install_service":
        package = trace.get("args", {}).get("package", "unknown")
        if exit_code == 0:
            went_well.append(f"service_install: '{package}' completed successfully (exit 0)")
            evidence.append(f"systemctl is-active {package}")
        else:
            didnt.append(f"service_install: '{package}' failed (exit {exit_code})")
            evidence.append(f"journalctl -u {package} -n 20 --no-pager")

    elif action == "linux_exec":
        cmd = trace.get("args", {}).get("command", "unknown")
        duration = trace.get("duration_ms", -1)
        if exit_code == 0 and 0 <= duration < 60_000:
            went_well.append(f"linux_exec completed in {duration}ms")
            evidence.append(f"echo \"cmd: {cmd} (exit {exit_code})\"")

    elif action == "gh_query":
        if exit_code == 0:
            went_well.append("gh_query returned successfully")
            evidence.append(f"echo \"query: {trace.get('args', {}).get('query', 'unknown')}\"")
        else:
            didnt.append("gh_query failed")
            evidence.append(f"echo \"exit: {exit_code}\"")

    # Always add role+login context
    evidence.append(f"echo 'actor: {login} @ role={role}, action={action}'")

    # If no findings were produced, add a generic entry.
    if not went_well and not didnt:
        went_well.append("action completed without errors")

    return {
        "what_went_well": went_well,
        "what_didnt": didnt,
        "evidence": evidence,
    }


def _shared_root() -> Path:
    """Return the shared storage root, matching the convention used by grounding.py."""
    return Path(os.environ.get("NEOHIRO_SHARED_ROOT", "/shared"))


# ── introspection ─────────────────────────────────────────────────────────────

def _run_introspective() -> dict:
    """
    Run an introspective check: does the system know itself?

    Loads the grounding.json (if present) and the last_run.yaml to
    produce a self-awareness score.

    Paths are resolved via NEOHIRO_SHARED_ROOT (same variable that grounding.py uses)
    so the introspection reads the same grounding.json that the grounding-audit
    dispatcher wrote.
    """
    shared_root = _shared_root()
    grounding_file = shared_root / "public" / "health" / "grounding.json"
    awareness_gaps: list[str] = []
    self_aware = True

    if grounding_file.is_file():
        try:
            data = json.loads(grounding_file.read_text(encoding="utf-8"))
            rate = float(data.get("grounding_rate", 1.0))
            if rate < 0.90:
                awareness_gaps.append(f"grounding_rate={rate:.2f} < 0.90")
                self_aware = False
        except Exception:
            awareness_gaps.append("grounding.json unreadable")

    last_run = _get_brain_path() / "heartbeat" / "last_run.yaml"
    if not last_run.is_file():
        awareness_gaps.append("last_run.yaml missing")
        self_aware = False

    return {
        "self_aware": self_aware,
        "awareness_gaps": awareness_gaps,
    }


# ── doctor escalation ──────────────────────────────────────────────────────────

_DOCTOR_SCRIPTS: dict[str, list[str]] = {
    "install_service": ["neohiro-doctor/monitor.sh", "--target", "brain-device"],
    "health_regression": ["neohiro-doctor/monitor.sh", "--target", "health"],
}


def _call_doctor(action: str, feedback: dict) -> bool:
    """
    Call neohiro-doctor if the action warrants it.
    Returns True if doctor was called, False otherwise.
    """
    script_key = None
    if action in ("install_service", "service_install"):
        script_key = "install_service"
    elif action in ("health_regression",):
        script_key = "health_regression"

    if script_key is None:
        return False

    scripts = _DOCTOR_SCRIPTS.get(script_key, [])
    if not scripts:
        return False

    # The monitor.sh path is relative to the workspace root.
    script_path = _ROOT / scripts[0]
    if not script_path.is_file():
        log.warning("doctor_script_missing", path=str(script_path))
        return False

    try:
        args = scripts[1:]  # remaining args after script path
        result = subprocess.run(
            ["bash", str(script_path)] + args,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(_ROOT),
        )
        log.info("doctor_called", action=action, returncode=result.returncode)
        return result.returncode == 0
    except Exception as e:
        log.warning("doctor_call_failed", action=action, error=str(e))
        return False


# ── godadmin poke ─────────────────────────────────────────────────────────────

def _poke_godadmin(
    feedback: dict,
    *,
    went_well: list[str],
    didnt: list[str],
    self_aware: bool,
    doctor_called: bool,
    reason: str,
) -> bool:
    """
    Write a godadmin poke file.
    Returns True if poke was written, False otherwise.
    """
    login = feedback.get("login", "")
    role = feedback.get("role", "")
    action = feedback.get("action", "")

    poke_queue_dir = _get_brain_path() / "heartbeat" / "poke_queue"
    poke_queue_dir.mkdir(parents=True, exist_ok=True)

    ts_safe = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    poke_file = poke_queue_dir / f"godadmin-poke-{ts_safe}.yaml"

    poke = {
        "schema_version": 1,
        "ts": _iso_now(),
        "kind": "brain_device_agency",
        "reason": reason,
        "actor_login": login,
        "actor_role": role,
        "action": action,
        "retrospective_summary": {
            "went_well": went_well,
            "didnt": didnt,
        },
        "self_aware": self_aware,
        "doctor_called": doctor_called,
    }

    try:
        from atomic import write_yaml
        write_yaml(poke_file, poke, prefix=".poke.")
        log.info("godadmin_poked", file=str(poke_file), reason=reason)
        return True
    except Exception as e:
        log.warning("godadmin_poke_failed", error=str(e))
        return False


# ── self-improvement audit ─────────────────────────────────────────────────────

def _write_self_improvement_audit(feedback: dict, retro: dict) -> None:
    """
    Append a self_improvement entry to /shared/heart/audit/self_improvement.yaml.
    This is the same path used by Heart/tools/self_improvement_sync.py.
    """
    brain_path = _get_brain_path()
    audit_file = brain_path / "audit" / "self_improvement.yaml"
    audit_file.parent.mkdir(parents=True, exist_ok=True)

    entry = {
        "schema_version": 1,
        "ts": _iso_now(),
        "source": "brain_device_agency_heart",
        "feedback_ts": feedback.get("ts", ""),
        "login": feedback.get("login", ""),
        "role": feedback.get("role", ""),
        "action": feedback.get("action", ""),
        "retrospective": retro.get("retrospective", {}),
        "introspective": retro.get("introspective", {}),
        "doctor_called": retro.get("doctor_called", False),
        "godadmin_notified": retro.get("godadmin_notified", False),
        "self_improvement_actions": retro.get("self_improvement_actions", []),
    }

    line = json.dumps(entry) + "\n"
    try:
        with open(audit_file, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        log.info("self_improvement_audit_written", action=feedback.get("action", ""))
    except OSError as e:
        log.warning("self_improvement_audit_write_failed", error=str(e))


# ── cross-privilege detection ─────────────────────────────────────────────────

def _is_cross_privilege(feedback: dict) -> bool:
    """
    Returns True if the action crossed a privilege boundary.
    Currently: dev performed a service_install is cross-privilege.
    """
    role = feedback.get("role", "")
    action = feedback.get("action", "")
    if role == "dev" and action in ("install_service", "service_install"):
        return True
    return False


# ── main dispatch ─────────────────────────────────────────────────────────────

@dataclass
class RetroResult:
    feedback_id: str
    action: str
    retrospective: dict
    introspective: dict
    doctor_called: bool
    godadmin_notified: bool
    self_improvement_actions: list[str]
    ok: bool
    error: str = ""


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _feedback_id(feedback: dict) -> str:
    ts = feedback.get("ts", "").replace(":", "-").replace(" ", "_")
    login = feedback.get("login", "unknown")
    return f"{ts}-{login}"


def _processed_marker(feedback_id: str) -> Path:
    return _get_feedback_dir() / f".{feedback_id}.processed"


def run_once(brain_path: Optional[Path] = None) -> dict[str, Any]:
    """
    Heart dispatcher entry point.

    Reads all feedback YAML files from the feedback directory that are not
    yet marked as processed, runs retrospective + introspective + doctor
    + godadmin poke for each, and marks them processed.

    Args:
        brain_path  — path to /Brain (for audit paths). Defaults to
                      $BRAIN_PATH or /brain.

    Returns:
        {
            "ok": bool,
            "processed": int,
            "errors": int,
            "skipped_over_batch_limit": int,
            "results": [RetroResult, ...],
        }
    """
    if brain_path is not None:
        os.environ["BRAIN_PATH"] = str(brain_path.resolve())

    feedback_dir = _get_feedback_dir()
    retro_dir = _get_retro_dir()

    retro_dir.mkdir(parents=True, exist_ok=True)
    feedback_dir.mkdir(parents=True, exist_ok=True)

    results: list[RetroResult] = []
    processed_count = 0
    error_count = 0
    skipped_over_limit = 0

    # Batch limit prevents a large backlog from locking the dispatcher.
    try:
        batch_limit = int(os.environ.get("HEART_BRAIN_DEVICE_RETRO_BATCH", "50"))
    except ValueError:
        batch_limit = 50
    batch_limit = max(1, batch_limit)

    try:
        feedback_files = sorted(feedback_dir.glob("*.yaml"))
    except OSError as e:
        log.warning("feedback_dir_unreadable", error=str(e))
        feedback_files = []

    # First pass: count how many would be skipped due to the batch limit.
    # This is informational only; we still process the first `batch_limit` files.
    total_unprocessed = 0
    for fb_path in feedback_files:
        if fb_path.name.startswith("."):
            continue
        if _processed_marker(fb_path.stem).is_file():
            continue
        total_unprocessed += 1

    if total_unprocessed > batch_limit:
        skipped_over_limit = total_unprocessed - batch_limit
        log.info("retro_batch_limit", total=total_unprocessed, limit=batch_limit,
                 skipped=skipped_over_limit)

    # Second pass: process at most `batch_limit` files.
    processed_this_run = 0

    for fb_path in feedback_files:
        feedback_id = fb_path.stem  # filename without .yaml

        # Skip files that are themselves named with a leading dot (markers).
        if fb_path.name.startswith("."):
            continue

        # Skip already-processed files.
        marker = _processed_marker(feedback_id)
        if marker.is_file():
            continue

        # Respect the batch limit so a large backlog doesn't lock the dispatcher.
        # (The first pass already computed `skipped_over_limit = total_unprocessed - batch_limit`
        #  so the remaining files are skipped without double-counting here.)
        if processed_this_run >= batch_limit:
            continue

        feedback = _read_feedback(fb_path)
        if feedback is None:
            log.warning("feedback_parse_failed", path=str(fb_path))
            error_count += 1
            continue

        action = feedback.get("action", "unknown")
        fid = _feedback_id(feedback)

        try:
            retro = _run_retrospective(feedback)
            intro = _run_introspective()

            doctor_called = _call_doctor(action, feedback)
            cross_priv = _is_cross_privilege(feedback)

            godadmin_notified = False
            if cross_priv:
                godadmin_reason = f"dev performed {action} (cross-privilege boundary)"
                godadmin_notified = _poke_godadmin(
                    feedback,
                    went_well=retro.get("what_went_well", []),
                    didnt=retro.get("what_didnt", []),
                    self_aware=intro.get("self_aware", True),
                    doctor_called=doctor_called,
                    reason=godadmin_reason,
                )

            # Derive self-improvement actions from the retrospective.
            self_improvement_actions: list[str] = []
            for item in retro.get("what_didnt", []):
                if "failed" in item.lower():
                    # Convert failure description to a self-improvement suggestion.
                    self_improvement_actions.append(f"investigate: {item}")

            # Write retro output.
            retro_output_path = retro_dir / f"{fid}.yaml"
            retro_output = {
                "schema_version": 1,
                "ts": _iso_now(),
                "feedback_id": fid,
                "action": action,
                "retrospective": retro,
                "introspective": intro,
                "doctor_called": doctor_called,
                "self_improvement_actions": self_improvement_actions,
                "godadmin_notified": godadmin_notified,
            }
            _write_yaml_atomic(retro_output_path, retro_output)

            # Write self-improvement audit.
            _write_self_improvement_audit(feedback, {
                "retrospective": retro,
                "introspective": intro,
                "doctor_called": doctor_called,
                "godadmin_notified": godadmin_notified,
                "self_improvement_actions": self_improvement_actions,
            })

            # Mark as processed.
            try:
                marker.write_text(_iso_now(), encoding="utf-8")
            except OSError:
                pass

            result = RetroResult(
                feedback_id=fid,
                action=action,
                retrospective=retro,
                introspective=intro,
                doctor_called=doctor_called,
                godadmin_notified=godadmin_notified,
                self_improvement_actions=self_improvement_actions,
                ok=True,
            )
            results.append(result)
            processed_count += 1
            processed_this_run += 1
            log.info("retro_complete", feedback_id=fid, action=action,
                     doctor_called=doctor_called, godadmin_notified=godadmin_notified)

        except Exception as e:
            error_count += 1
            log.error("retro_failed", feedback_id=fid, error=str(e))
            results.append(RetroResult(
                feedback_id=fid,
                action=action,
                retrospective={},
                introspective={},
                doctor_called=False,
                godadmin_notified=False,
                self_improvement_actions=[],
                ok=False,
                error=str(e),
            ))

    ok = error_count == 0
    return {
        "ok": ok,
        "processed": processed_count,
        "errors": error_count,
        "skipped_over_batch_limit": skipped_over_limit,
        "results": [r.__dict__ for r in results],
        "ts": _iso_now(),
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="brain_device_agency_heart — retro dispatcher")
    p.add_argument("--once", action="store_true", help="Run once and exit")
    p.add_argument("--brain-path", type=Path, help="Path to /Brain")
    p.add_argument("--feedback-dir", type=Path, help="Override feedback directory")
    p.add_argument("--retro-dir", type=Path, help="Override retro directory")
    args = p.parse_args()

    if args.brain_path:
        os.environ["BRAIN_PATH"] = str(args.brain_path.resolve())
    if args.feedback_dir:
        os.environ["NEOHIRO_BRAIN_DEVICE_FEEDBACK_DIR"] = str(args.feedback_dir.resolve())
    if args.retro_dir:
        os.environ["NEOHIRO_BRAIN_DEVICE_RETRO_DIR"] = str(args.retro_dir.resolve())

    if args.once:
        result = run_once()
        print(json.dumps(result, indent=2))
    else:
        import time
        log.info("dispatcher_started", mode="continuous")
        while True:
            run_once()
            time.sleep(300)  # 5 min

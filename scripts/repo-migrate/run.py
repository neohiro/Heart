"""
repo-migrate — Heart dispatcher: poll neohiro/repo-audit/migrations.json for
status: pending entries and dispatch the migration via the GitHub Actions
fallback workflow.

This is a SCAFFOLD scope.  The Python side reads the manifest, identifies
pending transfers, and emits a heartbeat event with the list.  The actual
transfer is executed by a GitHub Actions workflow (neohiro/voicemail
.actions/workflows/repo-migrate-fallback.yml) that calls
`scripts/execute-all.ps1 -Phase 2 -Execute` on a dispatch trigger.  The
Heart process does NOT directly invoke PowerShell (cross-platform
constraint; Heart runs on Alpine Linux).

Outputs (written to /shared/heart/repo_migrate/):
    pending.json      — current list of pending migrations
    heartbeat.jsonl   — append-only audit of dispatcher runs
    last_updated      — ISO timestamp

Run:
    python run.py --once
    python run.py --quiet --dry-run
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from urllib import error as url_error
from urllib import request as url_request

import structlog
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent / "_lib"))
from heart_dispatch import (
    atomic_write_json,
    atomic_write_text,
    setup_logging,
    run_scope,
    utcnow_iso,
    write_run_record,
)

SCRIPT_DIR = Path(__file__).resolve().parent
SHARED_OUT = Path(os.environ.get("NEOHIRO_SHARED_ROOT", "/shared")) / "heart" / "repo_migrate"

# Canonical manifest URL.  The Heart process must have network access to
# api.github.com (it does; the live-observer scope uses it).  Override with
# NEOHIRO_REPO_AUDIT_URL for testing against a fork.
MANIFEST_URL = os.environ.get(
    "NEOHIRO_REPO_AUDIT_URL",
    "https://raw.githubusercontent.com/neohiro/repo-audit/main/migrations.json",
)

# Org/admin endpoint for dispatching the fallback workflow.  Workflow_dispatch
# is the right tool for one-shot triggers; we POST to this endpoint with the
# pending source list as inputs.
FALLBACK_DISPATCH_URL = os.environ.get(
    "NEOHIRO_REPO_MIGRATE_DISPATCH_URL",
    "https://api.github.com/repos/neohiro/voicemail/actions/workflows/repo-migrate-fallback.yml/dispatches",
)

DEFAULT_TIMEOUT = 30
GITHUB_API_VERSION = "2022-11-28"


def _get_manifest(log: structlog.stdlib.BoundLogger) -> dict:
    """Fetch the canonical migrations.json from neohiro/repo-audit.

    Raises:
        RuntimeError: on any non-200 response or unparseable JSON.
    """
    req = url_request.Request(MANIFEST_URL, headers={"User-Agent": "neohiro-heart/repo-migrate"})
    try:
        with url_request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            if resp.status != 200:
                raise RuntimeError(f"manifest fetch returned HTTP {resp.status}")
            body = resp.read().decode("utf-8")
    except (url_error.URLError, url_error.HTTPError, TimeoutError) as e:
        raise RuntimeError(f"manifest fetch failed: {e}") from e
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"manifest is not valid JSON: {e}") from e


def _select_pending(manifest: dict) -> list[dict]:
    """Return the entries that the dispatcher is responsible for acting on.

    Filters:
        - status == "pending"     : not yet started
        - type == "transfer"      : skip "manual" (those are human-driven;
                                     see MANUAL_MIGRATION.md in the manifest repo)
    """
    out: list[dict] = []
    for entry in manifest.get("repos", []):
        if entry.get("status") == "pending" and entry.get("type") == "transfer":
            out.append(entry)
    return out


def _dispatch_workflow(log: structlog.stdlib.BoundLogger, pending: list[dict], dry_run: bool) -> int:
    """Trigger the GitHub Actions fallback workflow with the pending list.

    Returns 0 on successful dispatch, non-zero on failure.  dry_run=True
    short-circuits to logging-only.
    """
    if dry_run:
        log.info("dispatch.dry_run", count=len(pending), pending=[p["source"] for p in pending])
        return 0

    token = os.environ.get("GH_TOKEN", os.environ.get("NEOHIRO_GITHUB_TOKEN", ""))
    if not token:
        log.warning("dispatch.no_token", message="GH_TOKEN/NEOHIRO_GITHUB_TOKEN not set; cannot dispatch")
        return 2

    payload = {
        "ref": "master",
        "inputs": {
            "source": ",".join(p["source"] for p in pending),
            "target_org": pending[0].get("target_org", "frenzypenguin-media") if pending else "frenzypenguin-media",
        },
    }
    req = url_request.Request(
        FALLBACK_DISPATCH_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": "neohiro-heart/repo-migrate",
            "Content-Type": "application/json",
        },
    )
    try:
        with url_request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            # 204 = No Content; that's the documented success response.
            if resp.status not in (200, 201, 202, 204):
                body = resp.read().decode("utf-8", errors="replace")
                log.error("dispatch.failed", status=resp.status, body=body[:200])
                return 1
            log.info("dispatch.ok", count=len(pending), inputs=payload["inputs"])
            return 0
    except (url_error.URLError, url_error.HTTPError, TimeoutError) as e:
        log.error("dispatch.exception", error=str(e))
        return 1


def _heartbeat_path() -> Path:
    return SHARED_OUT / "heartbeat.jsonl"


def _append_heartbeat(log: structlog.stdlib.BoundLogger, pending: list[dict], rc: int) -> None:
    SHARED_OUT.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": utcnow_iso(),
        "pending_count": len(pending),
        "pending": [p["source"] for p in pending],
        "rc": rc,
    }
    line = json.dumps(entry, sort_keys=True)
    with _heartbeat_path().open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    log.info("heartbeat.appended", line=line)


def handler(log: structlog.stdlib.BoundLogger, config: dict) -> int:
    """Heart dispatcher entry point.

    Flow:
        1. Fetch migrations.json from neohiro/repo-audit (canonical SSOT).
        2. Filter to status=pending + type=transfer.
        3. Write the current pending list to /shared/heart/repo_migrate/pending.json.
        4. Dispatch the GitHub Actions fallback workflow with the list.
        5. Append to heartbeat.jsonl.
    """
    flags = config["flags"]
    dry_run = flags["dry_run"]
    SHARED_OUT.mkdir(parents=True, exist_ok=True)
    started = time.time()

    try:
        manifest = _get_manifest(log)
    except RuntimeError as e:
        log.error("manifest.error", error=str(e))
        return 1

    pending = _select_pending(manifest)
    log.info("pending.selected", count=len(pending), sources=[p["source"] for p in pending])

    # Write the current snapshot for downstream consumers (dashboard, audit).
    snapshot = {
        "ts": utcnow_iso(),
        "total": len(manifest.get("repos", [])),
        "pending": pending,
    }
    if not dry_run:
        atomic_write_json(SHARED_OUT / "pending.json", snapshot)
        atomic_write_text(SHARED_OUT / "last_updated", snapshot["ts"])

    # Dispatch the fallback workflow.  Even with an empty pending list we
    # emit the heartbeat so the cadence is visible in audit logs.
    rc = _dispatch_workflow(log, pending, dry_run=dry_run)
    _append_heartbeat(log, pending, rc)

    log.info("handler.done", duration_ms=int((time.time() - started) * 1000), rc=rc)
    return rc


if __name__ == "__main__":
    sys.exit(run_scope("repo-migrate", handler))

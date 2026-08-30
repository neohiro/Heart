"""
abuse_bridge.py — /Brain abuse filter integration for Heart

Consumes signals from:
  1. Heart OSINT pipeline (ip_observed, geo_drift, vpn_detected)
  2. GitHub event stream (mass_issue_open, fork_bomb_pattern)
  3. Auth failure log (from userdata/auth_pathway.py)
  4. External threat intel (VirusTotal, URLhaus, AbuseIPDB)

Writes verdicts into the shared Brain docker volume so /Brain can apply
them on the next cycle. Also writes an abuse_digest.json for the RT hub.

This bridge runs as a phase in Heart's cadence loop.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Add workspace paths for imports when running standalone
_WORKSPACE = Path(__file__).resolve().parent.parent.parent
for _p in (
    str(_WORKSPACE),                     # Brain, Heart, userdata packages
    str(_WORKSPACE / "userdata" / "src"),  # userdata.src.userdata.*
    str(_WORKSPACE / "Brain" / "src"),     # Brain.src.*
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import traceback as _tb
try:
    from userdata.ghosts import (
        create_from_ip_observation,
        create_from_third_party,
        ip_ghost_id,
    )
    from userdata.ghost_manager import (
        record_from_brain as ghost_record_from_brain,
        check_resurrection as ghost_check_resurrection,
        admin_briefing as ghost_admin_briefing,
    )
    from Brain.src.abuse_filter import AbuseSignal, evaluate, apply_verdicts

    ABUSE_FILTER_AVAILABLE = True
except ImportError as _e:
    print(f"[abuse_bridge] import failed: {_e}", file=sys.stderr)
    _tb.print_exc(file=sys.stderr)
    ABUSE_FILTER_AVAILABLE = False

BRAIN_PATH = Path(os.environ.get("BRAIN_PATH", "/brain"))
ABUSE_INBOX_DIR = BRAIN_PATH / "heartbeat" / "abuse_signals"
ABUSE_DIGEST  = BRAIN_PATH / "heartbeat" / "abuse_digest.json"
ABUSE_SIGNALS_INCOMING = BRAIN_PATH / "heartbeat" / "signals_incoming"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_filename() -> str:
    """Filesystem-safe timestamp: 2026-08-29T20-11-57-176514-00-00."""
    return _now().replace(":", "-").replace(".", "-").replace("+", "-")


def _safe_filename(s: str, fallback: str = "x") -> str:
    """Strip reserved characters for cross-platform filenames."""
    if not s:
        return fallback
    # Replace anything that's not [A-Za-z0-9_.-] with '_'
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in s) or fallback


def _log(msg: str, level: str = "info") -> None:
    ts = _now()
    print(json.dumps({"ts": ts, "level": level, "component": "abuse_bridge", "msg": msg}))


def run_phase(brain_path: Optional[Path] = None) -> dict:
    """
    Heart phase: consume inbox signals, evaluate through abuse_filter,
    write verdicts to Brain, and emit abuse_digest.json.

    Per signal, the bridge also:

      - Routes OSINT-style signals to /userdata/ghost_manager.record_from_brain
        (which writes the profile card).
      - Routes auth_pathway signals to /userdata/ghost_manager.check_resurrection
        if the auth event matches a known ghost_id (the entity is now a
        stranger or user, not a ghost).
      - Emits an admin briefing into /Brain/heartbeat/admin_briefing.json
        with the top resurrections, top social_boost ghosts, and pending
        admin alerts. Read by the RT healthcare hub and dashboard.

    Returns phase metadata dict.
    """
    start = time.time()
    bp = Path(brain_path) if brain_path else BRAIN_PATH

    verdicts_total = 0
    signals_processed = 0
    escalations = []
    resurrections = []

    signals_inbox = bp / "heartbeat" / "abuse_signals"
    signals_inbox.mkdir(parents=True, exist_ok=True)

    digest = {
        "generated_at": _now(),
        "cycle_verdicts": 0,
        "escalations": [],
        "tags": {},      # tag -> count
        "entities_at_risk": {},  # entity_id -> worst severity
        "ghosts_created": [],
        "resurrections": [],
    }

    if not ABUSE_FILTER_AVAILABLE:
        _log("abuse_filter not available, skipping", "warn")
        return {
            "phase": "abuse_filter",
            "duration_ms": int((time.time() - start) * 1000),
            "signals_processed": 0,
            "verdicts": 0,
            "escalations": 0,
        }

    for p in sorted(signals_inbox.glob("*.json")):
        try:
            raw = json.loads(p.read_text())
        except Exception:
            _log(f"corrupted signal file: {p.name}", "warn")
            p.unlink()
            continue

        signals_processed += 1
        source = raw.get("source", "unknown")
        raw_entity_id = raw.get("entity_id", "unknown")
        signal_type = raw.get("signal_type", "unknown")

        # Flatten fields.* up to top level so abuse_filter rule checks like
        # `field: "is_tor"` resolve against the raw signal.
        flat = {**raw}
        if isinstance(raw.get("fields"), dict):
            flat.update(raw["fields"])

        # Map incoming raw signal to AbuseSignal
        abuse_signal = AbuseSignal(
            source=source,
            entity_id=raw_entity_id,
            signal_type=signal_type,
            raw=flat,
        )

        verdicts = evaluate(abuse_signal)

        if verdicts:
            summary = apply_verdicts(verdicts)
            verdicts_total += len(verdicts)

            # `entity_id` here is the verdict subject (may differ from raw_entity_id)
            for entity_id, info in summary.items():
                worst = _worst_severity([v.severity for v in info["verdicts"]])
                digest["entities_at_risk"][entity_id] = worst
                for tag in info["tags"]:
                    digest["tags"][tag] = digest["tags"].get(tag, 0) + 1

                for v in info["verdicts"]:
                    if v.severity == "ESCALATE":
                        escalations.append({
                            "entity_id": entity_id,
                            "rule_id": v.rule_id,
                            "reason": v.reason,
                            "ts": _now(),
                        })
                        digest["escalations"].append(entity_id)

            # Also write per-entity verdict files for doctor
            for v in verdicts:
                if v.severity in ("ESCALATE", "FLAG"):
                    verdict_file = bp / "audit" / "abuse" / f"{_safe_filename(v.entity_id)}_{_safe_filename(v.rule_id)}.json"
                    verdict_file.parent.mkdir(parents=True, exist_ok=True)
                    verdict_file.write_text(json.dumps(v.__dict__, indent=2))

            # Ghost creation for ip_observed signals
            if source == "heartbeat_osint" and signal_type == "ip_observed":
                ip = flat.get("ip")
                if ip:
                    try:
                        ghost = create_from_ip_observation(ip, "heartbeat_osint")
                        digest["ghosts_created"].append(ghost.ghost_id)
                    except Exception as e:
                        _log(f"ghost creation failed: {e}", "warn")

            # Route OSINT-style signals to /userdata GhostManager
            # This is the canonical write path for ghost profile cards.
            if signal_type in (
                "github_mention",
                "third_party",
                "ip_observed",
                "contact_import",
            ):
                identity_kind = flat.get("identity_kind", "github_login")
                identity_raw = flat.get("identity_raw") or flat.get("ip") or flat.get("handle")
                if identity_raw:
                    try:
                        card = ghost_record_from_brain({
                            "kind": signal_type,
                            "identity": {"kind": identity_kind, "raw": identity_raw},
                            "observation": {
                                "source": source,
                                "kind": signal_type,
                                "body": flat.get("body", ""),
                                "context": flat.get("context", {}),
                            },
                            "occurred_at": raw.get("received_at", _now()),
                        }, authority=f"heartbeat:{source}")
                        if card and card.mentions == 1:
                            digest["ghosts_created"].append(card.profile_id)
                    except Exception as e:
                        _log(f"ghost_manager.record_from_brain failed: {e}", "warn")

            # Detect ghost resurrection from auth_pathway events
            # When a previously-observed ghost shows up as a real visitor
            # (voice, chat, sign-in, GitHub Copilot site interaction).
            if source == "auth_pathway":
                ghost_id = flat.get("ghost_id")
                observed_as = flat.get("observed_as", "visitor")
                if ghost_id:
                    try:
                        if ghost_check_resurrection(
                            ghost_id=ghost_id,
                            observed_as=observed_as,
                            authority=f"heartbeat:{source}",
                        ):
                            resurrections.append({
                                "ghost_id": ghost_id,
                                "observed_as": observed_as,
                                "ts": _now(),
                            })
                            digest["resurrections"].append(ghost_id)
                    except Exception as e:
                        _log(f"ghost resurrection check failed: {e}", "warn")

        # Move processed signal to archive
        try:
            archive_dir = signals_inbox / "processed"
            archive_dir.mkdir(parents=True, exist_ok=True)
            archive_name = f"{p.stem}_{_now_filename()}.json"
            p.rename(archive_dir / archive_name)
        except FileExistsError:
            # Stale archive with the same name — overwrite via copy+unlink
            target = archive_dir / archive_name
            target.write_text(p.read_text())
            p.unlink()
        except OSError as e:
            _log(f"archive failed for {p.name}: {e}", "warn")
            p.unlink()

    digest["cycle_verdicts"] = verdicts_total
    digest["signals_processed"] = signals_processed
    digest["resurrections"] = list(set(digest["resurrections"]))

    # Write digest for RT healthcare hub
    digest_path = bp / "heartbeat" / "abuse_digest.json"
    digest_path.parent.mkdir(parents=True, exist_ok=True)
    digest_path.write_text(json.dumps(digest, indent=2))

    # Emit admin briefing into Brain so RT hub and dashboard can read it
    if ABUSE_FILTER_AVAILABLE:
        try:
            brief = ghost_admin_briefing(top_n=10)
            brief["cycle_ts"] = _now()
            brief["resurrections_this_cycle"] = resurrections
            brief_path = bp / "heartbeat" / "admin_briefing.json"
            brief_path.parent.mkdir(parents=True, exist_ok=True)
            brief_path.write_text(json.dumps(brief, indent=2))
        except Exception as e:
            _log(f"admin_briefing write failed: {e}", "warn")

    duration_ms = int((time.time() - start) * 1000)
    result = {
        "phase": "abuse_filter",
        "duration_ms": duration_ms,
        "signals_processed": signals_processed,
        "verdicts": verdicts_total,
        "escalations": len(escalations),
        "ghosts_created": len(digest["ghosts_created"]),
        "resurrections": len(resurrections),
    }

    _log(
        f"abuse_filter: {signals_processed} signals, {verdicts_total} verdicts, "
        f"{len(escalations)} escalations, {len(digest['ghosts_created'])} ghosts, "
        f"{len(resurrections)} resurrections"
    )

    return result


def _worst_severity(severities: list[str]) -> str:
    order = ["ALLOW", "WATCH", "FLAG", "SUSPEND", "ESCALATE"]
    worst = 0
    for s in severities:
        idx = order.index(s) if s in order else 0
        if idx > worst:
            worst = idx
    return order[worst]


def enqueue_signal(
    signal_type: str,
    entity_id: str,
    source: str,
    fields: dict,
    brain_path: Optional[Path] = None,
) -> None:
    """
    Enqueue a raw signal for the abuse filter to process next cycle.
    Call this from Heart OSINT, auth failures, or GitHub event parser.
    """
    bp = Path(brain_path) if brain_path else BRAIN_PATH
    inbox = bp / "heartbeat" / "abuse_signals"
    inbox.mkdir(parents=True, exist_ok=True)

    signal = {
        "source": source,
        "entity_id": entity_id,
        "signal_type": signal_type,
        "received_at": _now(),
        "fields": fields,
    }

    fname = f"{_safe_filename(source)}_{_safe_filename(signal_type)}_{_safe_filename(entity_id)}_{_now_filename()}.json"
    (inbox / fname).write_text(json.dumps(signal))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Heart abuse filter bridge")
    parser.add_argument("--brain-path", type=Path, default=Path(os.environ.get("BRAIN_PATH", "Brain")))
    args = parser.parse_args()

    result = run_phase(args.brain_path)
    print(json.dumps(result, indent=2))

# /Heart — Cadence Engine Spec

> **Status: implementation skeleton.** Go reference + Python bridge. See
> `../Brain/RECONCILIATION.md` for live-system map (wingman-hub is the live
> /Heart; this spec is the planned future).

## Storage contract — `/activememory`

All three dockers (Heart, Brain, Mouth) read and write one device-root
bind mount: **`/activememory`**. It is the shared persistent store; it
is **not** a docker named volume. The bind is a directory on the
tailscale device, owned by the host operator.

```
device root
└── /activememory/                ← host-visible, operator-owned, persistent
    ├── brain/                    ← /Brain sub-tree (//mind + //intuition rw)
    │   ├── _entities/            ← org-*.md, user-*.md (per Brain/_schema)
    │   ├── heartbeat/            ← intent, last deliberation, integrity
    │   ├── memory.db             ← SQLite hearsay + activity cache
    │   └── config/privacy_rules.yaml
    ├── heart/                    ← /Heart sub-tree (Heart sole writer)
    │   ├── heartbeat/            ← last_run.yaml, health.yaml, mode.yaml
    │   │   └── poke.json         ← poke protocol target — see below
    │   ├── reminders/            ← timed tasks
    │   └── audit/heartbeat.yaml
    └── mouth/                    ← /Mouth sub-tree (Mouth rw; Heart/Brain ro)
        ├── outbox/               ← generated speech/text
        ├── errors/               ← phase-classified error explanations
        ├── replies/              ← LLM reply artifacts
        └── mouth.sock            ← unix socket for direct opencode attach
```

### Per-service mount contract

| Service  | /activememory | Notes |
|----------|---------------|-------|
| heart    | `:rw`         | Sole writer of `/heart/` sub-tree |
| brain    | `:rw`         | Writer of `/brain/` sub-tree (//mind + //intuition) |
| intuition| `:rw`         | Sub-role of brain (same container); writes deliberation log to `/activememory/brain/heartbeat/intuition.yaml` |
| mouth    | `:ro`         | Reader; only writes to its own `/mouth/` output (so `:rw` on `/mouth/` sub-tree if the operator wants strict isolation) |

### Path migration (old → new)

The original Heart spec used `Heart/data/` and `Brain/heartbeat/`. The
canonical /activememory model unifies these into a single host-root
mount. The mapping:

| Old path (original Heart) | New path (canonical /activememory) |
|---------------------------|------------------------------------|
| `Heart/data/last_run.yaml` | `/activememory/heart/heartbeat/last_run.yaml` |
| `Heart/data/health.yaml` | `/activememory/heart/heartbeat/health.yaml` |
| `Heart/data/repo_summary.json` | `/activememory/heart/heartbeat/repo_summary.json` |
| `Heart/data/repos.yaml` | `/activememory/heart/heartbeat/repos.yaml` |
| `Heart/data/mode.yaml` | `/activememory/heart/heartbeat/mode.yaml` |
| `Heart/data/stale.yaml` | `/activememory/heart/heartbeat/stale.yaml` |
| `Heart/data/audit.yaml` | `/activememory/heart/audit/heartbeat.yaml` |
| `Brain/heartbeat/intuition.yaml` | `/activememory/brain/heartbeat/intuition.yaml` |
| `Brain/heartbeat/poke_state.yaml` | `/activememory/heart/heartbeat/poke_state.yaml` |
| `Brain/_entities/*.md` | `/activememory/brain/_entities/*.md` |
| `Brain/memory.db` | `/activememory/brain/memory.db` |

The `Ownership` and `Phases` tables below still use the old paths for
historical continuity; the Go skeleton (`cmd/heart/main.go`) and the
canonical compose (`Heart/compose.yml`) use the new /activememory paths.
A scripted migration is planned via `Heart/migrate_paths.py`.

The "no leaks" guarantee: a writer of `/heart/` cannot read or write
`/brain/`, and vice versa, because the writer is a process — not the
container. The container process only opens paths it is supposed to.
The container-level `read_only: true` + bind mount gives defense in
depth: even if a process is compromised, it cannot `chmod` or `rm -rf`
above its mount.

### What goes in `/activememory` vs GitHub

| Lives in /activememory only | Lives in GitHub (mirrored) |
|------------------------------|----------------------------|
| Live hearsay cache (rolling 5k entries) in `/activememory/brain/memory.db` | Frozen hearsay snapshot (audit) |
| Heart cycle state: mode/health/last_run at `/activememory/heart/heartbeat/` | Append-only audit log at `/activememory/heart/audit/` |
| SQLite memory.db (hearsay + activity) | Per-user backup (encrypted) |
| Poke protocol state at `/activememory/heart/heartbeat/` | Doctor dispatch receipts (GitHub Actions logs) |

The `/activememory/heart/heartbeat/last_run.yaml` is the single source of
truth for "is the heart alive". Mouth and the dashboard poll this file
before deciding to wake up.

---

## //intuition — deterministic deliberation gate

`//intuition` is a sub-role of `//mind` (Brain). It lives in the **Brain
docker** as a sibling module to `//mind` (per the user's architecture
direction: "let its modules live in /Brain docker as well, just like
//mind"). It runs as a separate process inside the same container so it
can crash and be supervised without taking `//mind` down. It is
**deterministic**: given the same input state, it always reaches the
same decision. It is **not** an LLM call.

### Threshold (≥ 0.75)

For every Heart cycle, //intuition reads:
- the latest `/activememory/heart/heartbeat/health.yaml` (Heart)
- the latest `/activememory/heart/heartbeat/repo_summary.json` (Heart)
- the active `/activememory/heart/heartbeat/mode.yaml` (Heart)
- any active `/activememory/heart/reminders/*.yaml` due now
- any `/activememory/brain/_entities/org-*.md` whose `weight > 0.7`

It then computes a per-domain cross-score:

```
score = Σ (weight_i × freshness_i × confidence_i) / Σ weight_i
```

Where:
- `weight_i` is the per-entity importance (from `_entities/*.md`).
- `freshness_i` is 1.0 if the entity's data is fresher than the
  current mode's `stale_after`, else 0.0–0.5 decaying linearly.
- `confidence_i` is the entity's own `confidence` field (set by Heart
  during fetch based on ETag stability, rate-limit proximity, etc).

The deliberation also evaluates **event triggers**:
- new release tag in any tracked repo within the last cycle
- error fingerprint repeats ≥ 5 in 1h
- reminder due now and not yet acknowledged
- mode was bumped (active/sports) by user

Each trigger has a fixed weight. The total deliberation score is:

```
deliberation = base_score × (1 + Σ trigger_bonus)
```

The container emits a poke to Mouth **only if `deliberation ≥ 0.75`**.
Below threshold, the deliberation is **recorded** in
`/activememory/brain/heartbeat/intuition.yaml` (append-only, with `acted: false`),
but no poke is emitted. This is the "always thinking, only speaks
when certain" contract.

### Why deterministic, not LLM

- Reproducibility: same input → same output. Tests can pin behavior.
- Cost: zero LLM tokens per cycle; //intuition is pure arithmetic.
- Audit: every deliberation is replay-able from `intuition.yaml`.
- Latency: deliberation is sub-millisecond, not seconds.

The LLM cascade is invoked downstream by Mouth **after** //intuition
emits a poke. The deliberation is the gate, not the LLM.

### Multi-weight model

Each entity carries a vector of weights (not a single scalar):

```yaml
# /activememory/brain/_entities/org-neohiro.md (front matter excerpt)
id: org-neohiro
github_org: neohiro
weight: 0.9
weights:
  security: 1.0       # if a security advisory lands, this is critical
  releases: 0.6       # new release = nice to know
  ci_health: 0.7
  staleness: 0.5
confidence: 0.85
stale_after_seconds: 3600
```

//intuition uses the **most contextually relevant weight** for the
current mode:

- mode = dormant:     staleness × 0.3, releases × 0.2, security × 0.5
- mode = normal:      staleness × 0.4, releases × 0.3, security × 0.3
- mode = active:      staleness × 0.2, releases × 0.4, security × 0.4
- mode = sports:      staleness × 0.1, releases × 0.4, security × 0.5

This is the **multi-weight, multi-metric context** model the user
described: different metrics carry different importance in different
contexts, and the deliberation reflects that.

---

## Heart → Mouth poke protocol

When //intuition decides to speak, **Heart** (not //intuition directly)
writes the poke file. Heart is the writer; //intuition is the
gatekeeper; Mouth is the listener.

### `poke.json` schema

```json
{
  "id": "poke-<uuid>",
  "ts": "2026-08-29T21:30:00Z",
  "source": "intuition",                // "intuition" | "heart" | "doctor" | "reminder"
  "deliberation_id": "del-<uuid>",      // pointer to /activememory/brain/heartbeat/intuition.yaml
  "score": 0.83,
  "priority": "high",                   // "low" | "normal" | "high" | "critical"
  "subject": {
    "kind": "release",                  // "release" | "advisory" | "error" | "reminder" | "doctor_finding"
    "org": "neohiro",
    "repo": "windows",
    "ref": "v1.0.1",
    "summary": "Windows hardening release v1.0.1"
  },
  "context_refs": [
    "/activememory/brain/_entities/org-neohiro.md",
    "/activememory/brain/heartbeat/repo_summary.json"
  ],
  "audit_id": "<uuid>"                  // pointer into /activememory/heart/audit/heartbeat.yaml
}
```

### Write protocol

Heart writes atomically:
1. Stage the file as `poke.json.<pid>.staging`.
2. `fsync()`.
3. `rename()` to `poke.json` (atomic on POSIX).
4. Append the audit entry.

### Read protocol

Mouth's poke-watcher (a `fs.watch()` goroutine or inotify) reads the
file on every change. It processes in priority order
(critical > high > normal > low), and only one poke is in flight at a
time. If a new poke arrives while one is being processed, the new one
**supersedes** the old (the listener re-reads and re-evaluates).

### Failure mode

If Mouth crashes during poke processing, the poke is **left on disk**
and re-attempted on the next Mouth startup. //intuition is unaware of
Mouth's health; it only cares about the threshold. Heart tracks
unacked pokes in `/activememory/heart/heartbeat/poke_state.yaml`.

---

## Doctor dispatch chain

When Heart or //intuition encounters an error fingerprint repeating
≥ 5 in 1h, the dispatch chain is:

1. **Heart** increments fingerprint counter in
   `/activememory/heart/heartbeat/error_fingerprints.yaml`.
2. When ≥ 5: Heart `POST`s to
   `https://api.github.com/repos/neohiro/doctor/dispatches` with
   `{ "event_type": "diagnose", "client_payload": { "fingerprint": ..., "phase": ... } }`.
3. **neohiro/doctor** runs diagnostic scripts for that phase, writes
   findings to `/activememory/heart/audit/doctor/<run-id>.yaml`.
4. **Mouth** reads the doctor finding and, combined with //intuition's
   next deliberation, decides whether to surface it to the user.
5. If a self-heal recipe matches, Mouth asks the user (or auto-applies
   per `/userdata/<login>/self_heal.level`).

Doctor is a **GitHub repo**, not a container. It runs in GitHub
Actions. This is by design: doctor is firewalled from the device, can
be invoked from anywhere, and its state lives in GitHub, not
/activememory.

---

## Ownership

**Source of truth:** `Heart/` in this workspace is the source of truth.
`Brain/heartbeat/` is deprecated. All heartbeat state lives in `Heart/data/`.

| Path | Owner | Status |
|------|-------|--------|
| `Heart/data/` | Heart | Active — all heartbeat state |
| `Heart/cmd/heart/` | Heart | Go reference skeleton |
| `Heart/tools/heart.py` | Heart | Python bridge (GitHub Actions cron) |
| `Heart/tools/heartctl.py` | Heart | CLI control |
| `Heart/docker/` | Heart | Docker deployment artifacts |
| `Brain/heartbeat/` | Heart | **Deprecated** — migrates to `Heart/data/` |
| `Brain/_entities/` | Brain | Source of truth for org entities |

Migration: `Brain/heartbeat/` → `Heart/data/` is mechanical:
```
Heart/data/
├── last_run.yaml      ← was Brain/heartbeat/last_run.yaml
├── health.yaml         ← was Brain/heartbeat/health.yaml
├── repo_summary.json   ← was Brain/heartbeat/repo_summary.json
├── repos.yaml          ← was Brain/heartbeat/repos.yaml
├── mode.yaml           ← was Brain/heartbeat/mode.yaml
├── stale.yaml          ← was Brain/heartbeat/stale.yaml
└── audit.yaml         ← was Brain/audit/heartbeat.yaml
```

`Heart/tools/heart.py` reads entity discovery from `Brain/_entities/`.
Write targets currently go to `Brain/heartbeat/` and `Brain/audit/` (matching
live wingman-hub paths). The migration to `Heart/data/` is planned once the
Heart docker is live; use `Heart/migrate_paths.py` to perform it.

## Tier Model

Four tiers control what data a visitor can access. Each visitor has exactly
one **current_role** and an immutable **role_history** in `/userdata`.

| Tier | Trigger | Data access | Private briefings | Actions |
|------|---------|-------------|-------------------|---------|
| `stranger` | No github-login | Public SVG widgets, public dashboards | No | None |
| `login` | GitHub OAuth linked | Above + login-gated dashboards | No | None |
| `authorized` | GitHub readback verified (GitHub account with >0 public repos or org membership) | Above + private repos, private SVG layers | No | Trigger self-heal |
| `admin` | God-admin approval | Above + write to Heart, mode toggle, full access | Yes | Everything |
| `developer` | Admin-promoted (sidejob) | Health metrics, status logs, error logs, user-issue detected (no improvement possible), high-importance events only | **No** | Health monitoring, error triage |

### Role history (ghost promotion)

When a visitor's role changes, the transition is recorded in
`/userdata/<login>/role_history.yaml`:

```yaml
role_history:
  - role: stranger
    at: 2026-08-29T10:00:00Z
    reason: first visit
  - role: login
    at: 2026-08-29T11:00:00Z
    reason: github_oauth_linked
    ghost: true           # was a stranger; ghost label preserved
  - role: authorized
    at: 2026-08-29T12:00:00Z
    reason: github_readback_verified
    ghost: false
```

**Ghost promotion rule:** When a `stranger` is promoted to `login` via
GitHub OAuth link, the entity retains `ghost: true` in the history entry.
The current role is `login`, but the ghost label is preserved so that:
- The original anonymous footprint (IP hash, session data) is not merged into
  the authenticated identity without explicit confirmation.
- The `/Mouth` hearsay engine knows the user connected without being prompted.

**Developer subrole:** `developer` is a **sidejob** with strict constraints:
- No access to `/userdata/` — zero personalized data.
- No briefings, no recommendations, no per-user metrics.
- Only: raw health dashboards, error log streams, `user-issue detected` events.
- Triggered by: admin promotion or a `developer` label in `/userdata/<login>/`.

## Modes

| Mode | Default cycle | stale_after | when to use |
|------|---------------|-------------|-------------|
| `dormant` | 3600s (1h) | 24h | weekend / quiet |
| `normal` | 60s | 1h | default |
| `active` | 10s | 5m | user is online, /Mouth active |
| `sports` | 1s | 1m | dashboard active, debug session |

The dashboard's three-way toggle (normal / active / sports) is the only place
mode is set. Heart reads it from `Heart/data/mode.yaml` on every cycle.

## Repo awareness

Heart discovers which repos to monitor from two sources, merged by `org+repo` key:

1. **`Brain/_entities/org-*.md`** — every `org-*.md` file with a
   `github_org:` frontmatter + `repos:` list. Current: 4 org entities → 10 repos.
2. **`/activememory/heart/heartbeat/repos.yaml`** — additional per-repo overrides.
   Merged on top; used for `private` flag and alternate entity mapping.

**Path split note:** The Python bridge reads all paths from `BRAIN_PATH`. The Go
binary separates `HEART_BRAIN_PATH` (entity files, heartbeat output) from
`HEART_HEART_PATH` (heartbeat state). In GitHub Actions, both resolve to the same
directory. In `/activememory/` compose, they point to `/activememory/brain/` and
`/activememory/heart/` respectively. Consumers must handle both layouts.

## Phases (15, in execution order)

`tick` runs **before** the phase list in `run_cycle()` (sets mode, started_at).
The 14 phases below are what each cycle iterates.

| # | Phase | What it does | Writes to |
|---|-------|-------------|-----------|
| 0 | `tick` | Read mode, stamp started_at (pre-loop) | — |
| 1 | `discover_repos` | Load `/activememory/brain/_entities/org-*.md` + `heartbeat/repos.yaml` | — |
| 2 | `fetch_repos` | GitHub: `GET /orgs/{org}/repos` per org (if `GH_TOKEN`) | — |
| 3 | `fetch_issues` | GitHub: `GET /repos/{org}/{repo}/issues?state=open` | — |
| 4 | `fetch_prs` | GitHub: `GET /repos/{org}/{repo}/pulls?state=open` | — |
| 5 | `fetch_actions` | GitHub: `GET /repos/{org}/{repo}/actions/runs` | — |
| 6 | `ingest_news` | Read `neohiro/news/public/feeds/*/latest.json` | — |
| 7 | `ingest_content` | Read `frenzypenguin-media/Content-Creator/data/latest_posts*.{json,svg}` | — |
| 8 | `ingest_osint` | READ osint_cache.json → AMEND with new IP/geo/VPN → WRITE + enqueue abuse signals | `heartbeat/abuse_signals/` |
| 9 | `osint_userdata` | READ /userdata summaries → identify visitor roles + resurrections → backup write-back on organ failure | `heartbeat/admin_briefing.json` |
| 10 | `compute_health` | Derive metrics | `heartbeat/health.yaml` |
| 11 | `write_brain` | Write repo manifest | `heartbeat/repo_summary.json` |
| 12 | `fire_reminders` | Run due reminders from `reminders/*.yaml` (skip if `guard_until` in future) | — |
| 13 | `prune_stale` | Reject stale datapoints (per-mode `stale_after`) | `heartbeat/stale.yaml` |
| 14 | `self_heal` | Trigger doctor scripts if health degrades | — |
| 15 | `audit` | Append phase results; write `last_run.yaml` | `audit/heartbeat.yaml`, `heartbeat/last_run.yaml` |

**Code-vs-spec drift:**

- **Python bridge** (`tools/heart.py`): 14 phases in the list (excludes `tick`).
  Total 15 with `tick`. ✓ matches this table.
- **Go reference** (`cmd/heart/main.go`): 11 phases in the list
  (missing `discover_repos`, `ingest_osint`, `osint_userdata`).
  Total 12 with `tick`. **Out of sync — see follow-up below.**

### Follow-up: sync Go to match Python

The Go reference is an earlier skeleton; the Python bridge is canonical
because it is what runs in production (per `wingman-hub` cron / GitHub
Actions). To sync:

1. Add `phaseDiscoverRepos`, `phaseIngestOsint`, `phaseOsintUserdata`
   to `Heart/cmd/heart/main.go`.
2. Wire the imports for `osint_cache` (Go equivalent: see
   `Brain/src/osint_cache.py` for the data shape).
3. Re-test the binary compiles and writes matching `last_run.yaml` (15 phases).

Until then, the Go binary produces `last_run.yaml` with 12 phases; the
Python bridge produces 15. Consumers of `last_run.yaml` should treat
both as valid and handle missing phases.

All phases log via `structlog` (Python) or `log/slog` (Go): one structured
JSON object per line to stdout.

## Heart-docker alleviates GitHub Actions

The Heart docker instance (running on the tailscale device) is the **primary**
cadence engine. GitHub Actions workflows are the **fallback only**.

| Condition | Who runs the cycle |
|-----------|-------------------|
| Heart docker healthy | Heart (Go service) — runs continuously |
| Heart docker unhealthy | GitHub Actions cron workflow (emergency cadence: 1/hour) |
| Heart docker disk < 500 MB | GitHub Actions fallback triggered immediately |

**GitHub Actions minutes saved:** With Heart running on-device, all per-repo
cron workflows in `wingman-hub/.github/workflows/` that perform Heart-like
tasks (status checks, data fetches, health probes) can be consolidated into
a single GitHub Actions workflow that only runs when Heart is offline.

## Disk monitoring

`Heart/docker/monitor.sh` runs every minute via cron inside the container.
Thresholds:

| Free disk | Alert level | Recipients | Action |
|-----------|-------------|-----------|--------|
| > 2 GB | — | — | Normal |
| 500 MB – 2 GB | **Warning** | admin + developer | Log + warning |
| < 500 MB | **Critical** | admin + developer | Trigger GitHub Actions fallback immediately |

Alert mechanism: write to `Heart/data/alerts.yaml` (append-only), then POST
to `/Mouth` webhook for admin+developer roles.

## Output files

| Path | Format | Notes |
|------|--------|-------|
| `/activememory/heart/heartbeat/last_run.yaml` | YAML | mode, cycle, started_at, ended_at, phase_durations |
| `/activememory/heart/heartbeat/health.yaml` | YAML | ts, mode, repos_known, disk_free_mb, memory_free_mb, gh_errors_min, cycle_success, llm_fallbacks_h |
| `/activememory/heart/heartbeat/repo_summary.json` | JSON | Full org/repo manifest |
| `/activememory/heart/heartbeat/repos.yaml` | YAML | Per-repo overrides; managed by heartctl |
| `/activememory/heart/heartbeat/stale.yaml` | YAML | Append-only; rejected datapoints |
| `/activememory/heart/heartbeat/mode.yaml` | YAML | Written by heartctl mode command |
| `/activememory/heart/audit/heartbeat.yaml` | YAML | Append-only; ts, phase, outcome, elapsed_ms |
| `/activememory/heart/heartbeat/alerts.yaml` | YAML | Append-only; disk warnings + errors |
| `/activememory/heart/heartbeat/poke_state.yaml` | YAML | Unacked pokes; managed by Heart |
| `/activememory/heart/heartbeat/poke.json` | JSON | Atomic poke target; read by Mouth |
| `/activememory/heart/heartbeat/error_fingerprints.yaml` | YAML | Fingerprint counters for doctor dispatch |
| `/activememory/brain/heartbeat/intuition.yaml` | YAML | Deliberation log; appended by //intuition |
| `/activememory/brain/_entities/org-*.md` | Markdown | Org entity definitions |
| `/activememory/brain/memory.db` | SQLite | Hearsay + activity cache |

## Implementation files

| File | Type | Status |
|------|------|--------|
| `tools/heart.py` | Python bridge | **Live** (15 phases, structlog, --once/--continuous/--dry-run) |
| `tools/heartctl.py` | CLI control | **Live** (status/mode/repos/audit/health/phase/trigger/watch/doctor/doctor-deep/env-check) |
| `tools/requirements.txt` | Dependencies | **Live** (structlog>=24.1.0, PyYAML>=6.0) |
| `tools/abuse_bridge.py` | Heart→doctor bridge | **Live** |
| `tools/osint_cache.py` | OSINT cache | **Live** |
| `tools/osint_userdata.py` | Userdata OSINT | **Live** |
| `tools/self_improvement_sync.py` | Self-improvement sync | **Live** |
| `tools/monitor_shim.py` | monitor.sh shim | **Live** |
| `cmd/heart/main.go` | Go reference | **Live** (11 phases; missing 3 — see phase table above) |
| `docker/monitor.sh` | Disk watchdog | **Live** |
| `docker/role_history.py` | Ghost-promotion logic | **Live** |
| `compose.yml` | Docker compose | **Live** (heart-init, heart, brain, mouth) |
| `Dockerfile` | Container image | **Live** |
| `Makefile` | Build targets | **Live** |

## Quick start

```bash
# Python bridge — one cycle (GitHub Actions cron mode)
python Heart/tools/heart.py --once --brain-path Brain

# Python bridge — continuous loop
python Heart/tools/heart.py --continuous --brain-path Brain

# Dry run — phases execute but skip external calls + file writes
python Heart/tools/heart.py --once --brain-path Brain --dry-run

# Control interface
python Heart/tools/heartctl.py --brain-path Brain status
python Heart/tools/heartctl.py --brain-path Brain mode active
python Heart/tools/heartctl.py --brain-path Brain doctor
python Heart/tools/heartctl.py --brain-path Brain doctor-deep
python Heart/tools/heartctl.py --brain-path Brain doctor-deep --fix-heartbeat

# Go reference — one cycle, mode = active
# In the unified Brain tree layout, both envs point into Brain/ (Go still
# requires the two envs even if they share a parent). In the /activememory/
# compose layout, BRAIN→/activememory/brain/, HEART→/activememory/heart/.
HEART_BRAIN_PATH=Brain HEART_HEART_PATH=Brain/heartbeat go run ./Heart/cmd/heart
```

## Heartbeat file contract

The `/shared/.heartbeat` file is a sentinel marker that the Go sidecar (or any
heartbeat-producing process) must maintain.  The file must contain exactly:

```
heartbeat: OK
```

(15 bytes, with a trailing newline: `b"heartbeat: OK\n"`).  Any other content
— empty, whitespace-only, or different bytes — is treated as corruption by
`heartctl doctor-deep` and reported as a drift.

The file is also touched on every cycle to refresh mtime; the doctor
distinguishes "stale mtime" (touch didn't happen, file is old) from "corrupt
content" (mtime is fresh, content is wrong).  Both drift classes are reported
separately.

Note: `heart.py` uses a single `--brain-path` (not `--heart-path`).
The Python bridge reads everything (entities, mode, repos.yaml, audit,
heartbeat output) from the Brain tree; there is no separate Heart sub-tree
in the Python bridge. The Go binary separates `HEART_BRAIN_PATH` and
`HEART_HEART_PATH` per the `/activememory/` model.

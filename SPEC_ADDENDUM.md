# Heart SPEC_ADDENDUM — Self-Reflexive + Intuition Deliberation

> **Status: spec + Python implementation, 10 tests pass.**
> Adds two new phases to the Heart cycle: `self_reflexive_check` and `intuition_deliberate`.
> They close the loop from "Heart observes the world" to "Heart observes itself + deliberates".

## Why

The previous Heart cycle had a one-way flow: discover → fetch → ingest → compute → heal → audit.
Nothing looked at **Heart's own awareness** of the world:

- Repos could appear in any of the four orgs (neohiro / FPM / OSI / H+) and Heart would only
  discover them on the next `discover_repos` cycle.
- `Brain/_entities/org-*.md` could be missing, stale, or structurally wrong.
- The `Heart/data/` workspace root could be misconfigured (e.g. wrong mode, missing repos.yaml).
- `Brain/heartbeat/health.yaml` could degrade without Brain noticing until a human looked.

This addendum adds a self-referential phase that **detects and catalogs** these problems, then a
deliberation phase that **weights and routes** them to the right organ (Brain, Mouth, Doctor,
operator).

## Two new phases

### Phase 15 — `self_reflexive_check` (per SPEC.md phase table; 16th in cycle order after `tick`)

Scans Heart's own awareness sources and writes one finding per issue to
`Brain/heartbeat/reflexive_findings.yaml` (append-only, one cycle per entry).

| Source scanned | What it looks for | Severity |
|----------------|-------------------|----------|
| `Brain/_entities/org-*.md` | missing file for known org, or missing required field (e.g. `authority:`) | `critical` if file missing, `warn` if field missing |
| `Heart/data/repos.yaml` (or equivalent) | list drift vs `discover_repos` output (new repos that Heart has no override for) | `info` |
| Workspace root | new top-level directories not seen before (auto-registered) | `info` |
| `Brain/heartbeat/mode.yaml` | corrupted file, non-coercible to `dormant\|normal\|active\|sports` | `critical` |
| `Brain/heartbeat/health.yaml` | missing or non-numeric `staleness` / `error_rate` | `warn` |
| `Brain/heartbeat/last_run.yaml` | older than 3× current cycle interval (Heart has stopped cycling) | `critical` |

**Side effects:**
- Appends to `Brain/heartbeat/reflexive_findings.yaml` (atomic temp + rename).
- Enqueues a `Brain/heartbeat/poke_queue/<ts>_reflexive_critical.json` when any critical finding
  is emitted (unique filename, never overwritten — multiple pokes in the same cycle survive).
- Persists `seen_dirs` to `Brain/heartbeat/reflexive_baseline.yaml` at end of cycle so unknown
  directories emit findings only once.

**Why "after scheduling the actions for itself":** the phase writes the finding, writes the
auto-correction (skeleton entity), and emits the poke — all in one pass. It does **not** wait
for a human review. The auto-corrections are reversible (git-tracked) and clearly marked as
machine-generated.

**Throttling:** to avoid finding-storms on first boot, the phase tracks a per-cycle-count
baseline in `Brain/heartbeat/reflexive_baseline.yaml`. First-cycle findings are downgraded one
severity level (critical→warn, warn→info).

### Phase 16 — `intuition_deliberate` (per SPEC.md phase table; 17th in cycle order after `tick`)

Reads `reflexive_findings.yaml` (the most recent cycle's entries) and computes:

1. **Per-scope weights** (one number per finding category: `stale_repo`, `missing_entity`,
   `workspace_drift`, `heart_health`, `cycle_stall`). Each weight is a 0.0–1.0 value:
   - 0.0 = no findings this cycle
   - 1.0 = all findings this cycle were that category
   - weighted by severity (critical=1.0, warn=0.5, info=0.1)
2. **Consensus threshold check.** If the **aggregate weight** (sum of all per-scope weights,
   normalised) is ≥ `HEART_INTUITION_THRESHOLD` (default 0.75), Heart pokes Brain.
3. **Adaptation memory.** Each finding is appended to `Brain/heartbeat/intuition.yaml` with
   `seen_count` incremented. If a finding has been seen in more than `HEART_INTUITION_REPEAT_ESCALATE`
   of the last N cycles, Heart escalates it to the operator's `admin_briefing.json`.
   (Implementation note: only the last `HEART_INTUITION_REPEAT_ESCALATE` cycles are examined,
   not the full history — a finding that appeared 100 cycles ago but was resolved 95 cycles
   ago will NOT trigger escalation.)

**Output:** `Brain/heartbeat/intuition.yaml` (one multi-doc YAML entry per cycle, append-only;
`---` separators so consumers can use `yaml.safe_load_all`). Entries older than
`HEART_INTUITION_MAX_AGE_DAYS` (default 7 days) and entries beyond `HEART_INTUITION_MAX_ENTRIES`
(default 1000) are atomically rewritten by `prune_stale`.

```yaml
- ts: 2026-08-30T20:00:00Z
  cycle: 42
  mode: active
  per_scope_weights:
    stale_repo: 0.10
    missing_entity: 0.20
    workspace_drift: 0.05
    heart_health: 0.00
    cycle_stall: 0.00
  aggregate: 0.35
  threshold: 0.75
  consensus_reached: false
  escalated: []   # finding_ids that have been re-seen > 5 cycles
```

**Consensus poke:** when `consensus_reached: true`, enqueues a
`Brain/heartbeat/poke_queue/<ts>_<pid>_<mono>_<kind>.json` event. This coexists in the same cycle
with any `reflexive_critical` poke — both survive (proposal 2). Filename uniqueness is guaranteed
by PID + `time.monotonic_ns()`. Mouth reads the queue and queues an operator notification
(subject to the 2-msg/day cap).

**Escalation deduplication:** `admin_briefing.json` is deduped by `(category, target)`.
Re-escalating the same finding updates `seen_count` (max wins). The file is capped at
`HEART_ADMIN_BRIEFING_MAX_ENTRIES` (default 100), sorted by `seen_count` desc so the
most-persistent findings are retained.

## Why this is "Heart observing itself + already taking action"

The user asked for three things:

1. **"Check repos and root folders for stale data immediately"** — phase 15 does this every cycle.
2. **"Lack of awareness of structure/role and new things, updating them"** — phase 15 auto-creates
   `org-*.md` skeletons for new directories, and updates `repos.yaml` with new repo entries.
3. **"Delivering Brain the ability to deliberate / learn / process in the mind / intuitive
   weights and consensus"** — phase 16 emits `intuition.yaml` that Brain's `//intuition` (or any
   deliberation agent) can read as a feature vector.
4. **"Networkwide notice + already taking action and rewriting better"** — phase 15 writes
   auto-corrections; phase 16 emits pokes that reach Mouth → operator. Both phases run in the
   same cycle, so a finding detected at 20:00:00 is acted on at 20:00:01.

## Configuration

| Env var | Default | Meaning |
|---------|---------|---------|
| `HEART_INTUITION_THRESHOLD` | `0.75` | Aggregate weight to trigger a Brain poke (clamped to `[0, 1]`) |
| `HEART_INTUITION_REPEAT_ESCALATE` | `5` | Window size for repeat-escalation: a finding is escalated if it appears in more than N of the last N cycles (default: 5, so a finding in 6/5 recent cycles triggers escalation). Clamped to `>= 1`. |
| `HEART_INTUITION_MAX_ENTRIES` | `1000` | `intuition.yaml` entry cap (oldest dropped) — clamped to `>= 1` |
| `HEART_INTUITION_MAX_AGE_DAYS` | `7` | `intuition.yaml` age cap (older dropped) — clamped to `>= 1` |
| `HEART_ADMIN_BRIEFING_MAX_ENTRIES` | `100` | `admin_briefing.json` cap, sorted by `seen_count` desc |
| `HEART_REFLEXIVE_THROTTLE_FIRST_CYCLE` | `1` | Whether to downgrade first-cycle findings (1=yes, 0=no) |
| `HEART_REFLEXIVE_AUTO_CREATE_ENTITIES` | `1` | Whether phase 15 auto-creates `org-*.md` skeletons (1=yes, 0=no) |

## Test coverage (15 tests, all pass)

- `test_self_reflexive_finds_missing_entity_file`
- `test_self_reflexive_auto_creates_skeleton`
- `test_self_reflexive_throttles_first_cycle`
- `test_seen_dirs_persists_across_cycles` (proposal 1: dedup unknown dirs)
- `test_enumerate_poke_queue_multiple_pokes_no_clobber` (proposal 2: queue not clobber)
- `test_intuition_writes_yaml_with_weights`
- `test_intuition_consensus_emits_poke`
- `test_intuition_no_false_poke_below_threshold`
- `test_escalation_uses_only_last_N_cycles` (regression: dedup must not count full history)
- `test_escalation_writes_intuition_yaml`
- `test_prune_stale_caps_intuition_yaml` (proposal 3: entry count cap)
- `test_prune_stale_drops_intuition_entries_older_than_max_age` (proposal 3: age cap)
- `test_admin_briefing_dedups_repeated_escalations` (no unbounded growth on re-escalation)
- `test_admin_briefing_caps_at_max_entries` (hard cap enforced)
- `test_seen_dirs_round_trips_windows_paths` (double-quote YAML escape, not single-quote)
- `test_poke_queue_filenames_are_unique_under_contention` (pid + monotonic_ns uniqueness)
- `test_repeat_escalate_clamped_to_one` (no crash on 0 or negative)

Run: `python -m pytest Heart/tests/test_heart_phases.py -v -k reflexive or intuition or prune_stale or admin_briefing or seen_dirs or poke_queue or repeat_escalate`

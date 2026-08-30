# Heart — neohiro cadence engine

Per-org Heart steering repo for the **neohiro** org. Discovered by the
Heart process at the body level via `Heart/.neohiro/heart.yaml` per
`HEART_SCHEDULE_REGISTRY.md`.

## What lives here

| Path | Purpose |
| --- | --- |
| `tools/` | Populator scripts invoked by Heart on each cycle. |
| `schedules/` | Per-scope cadence declarations; `REGISTRY.yaml` is the dispatch index. |
| `.neohiro/heart.yaml` | Org override declaring this repo authoritative for neohiro. |
| `.github/workflows/` | GitHub Actions fallbacks when the live Heart process is down. |

## Scopes currently registered

- `news-populate` — every 5 min
- `osint-populate` — every 15 min
- `links-validate` — every 60 min
- `tools-populate` — every 6 h
- `visitor-counter` — every 5 min (added 2026-08-30; pulls
  freevisitorcounters.com into the worldmap datalayer)
- `social-counter` — every 15 min (added 2026-08-30; powers the public
  Social Media Counters section on neohiro-dashboard)

See `schedules/REGISTRY.yaml` for the full schema.

## Secrets

This repo never contains API keys. They live in `links-secret` and are
mirrored into GitHub Environments on the device.

| Scope | Source of truth |
| --- | --- |
| visitor-counter auth IDs | `/links-secret/visitor-counters.yaml` |
| social-counter platform keys | `/links-secret/social-counters.yaml` |

The display half (counter widget `<script src=...counter/.../t/1>`) is
public and lives in every README and `_layouts/default.html` that
mentions sponsors — see
`/userdata/wout/counter-embed-rollout/2026-08-30.md` for the full table.

## Cross-org context

Heart is the cadence engine for the **neohiro body**, which spans four
GitHub orgs: neohiro, frenzypenguin-media, openstageisland, transhumanists.
This repo is the per-org steering repo for **neohiro** specifically.
Sibling steering repos exist for each org (e.g. `frenzypenguin-media/Heart`).

For the full 15-phase Heart cycle diagram and how it ties to the body,
see **[`../network/WORKFLOWS.md`](../network/WORKFLOWS.md) § 8 (Heart cycle)**.

## Heart wiring

`/Heart` (this repo) owns:

- the script registry (`schedules/REGISTRY.yaml`)
- the failure policy (`schedules/REGISTRY.yaml` -> `failure_policy`)
- the doctor escalation triggers (`Heart/.neohiro/heart.yaml` -> `doctor_escalation`)

Brain/Root owns:

- the auth model (OAuth / bearer / GitHub App)
- the secrets store
- the cross-repo registry

neohiro-doctor owns:

- the interactive body tour (`neohiro-doctor/monitor.sh`)
- the addendum rewrite of every Heart README

neohiro-network owns:

- the shared drive mount + 12-factor flag mapping
- the schedule hot-reload (SIGHUP)

## Status

| Component | Status |
| --- | --- |
| Repo public on GitHub | yes — `github.com/neohiro/Heart` |
| Heart process discovery | live (via `.neohiro/heart.yaml`) |
| Visitor-counter scope | live (5 min cycle) |
| Social-counter scope | live (15 min cycle) |
| GitHub Actions fallbacks | wired (visitor-counter + social-counter) |
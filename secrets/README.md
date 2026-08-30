# Brain / Heart secrets

This directory holds GitHub token(s) used by the brain and heart
containers. They are bind-mounted into the containers as
`/run/secrets/<name>` per Docker Compose `secrets:` directive.

## Required

| File | Used by | Permissions |
|------|---------|-------------|
| `brain_gh_token.txt` | brain container (//mind + //intuition) | read-only in container, root-owned on host |

## Setup

```bash
# 1. Generate a GitHub App installation token with read access to:
#    - neohiro/*, FPM/*, OSI/*, H+/* orgs
#    - metadata read (repo list, issues, PRs, actions)
#    - NOT write — read-only tokens are enough for Heart's fetch phases
# 2. Write it to a file (no newline trailing):
printf '%s' "$(gh auth token)" > secrets/brain_gh_token.txt
# 3. Lock down permissions on the host:
chmod 0400 secrets/brain_gh_token.txt
chown 65532:65532 secrets/brain_gh_token.txt
```

## Rotation

Heart/Mouth do NOT auto-rotate. Rotation is an operator action:

```bash
# 1. Stop the affected containers
docker compose -f Heart/compose.yml stop brain mouth
# 2. Replace the file
printf '%s' "$(gh auth token --refresh)" > secrets/brain_gh_token.txt
# 3. Restart
docker compose -f Heart/compose.yml start brain mouth
```

## Never commit

These files are git-ignored. If a token is ever committed to a repo,
treat it as **compromised** and rotate immediately.

## Future work

When neohiro/doctor goes live, it will own the rotation workflow and
expose a heartbeat fingerprint for each token age. Until then, this
file is the only token carrier.

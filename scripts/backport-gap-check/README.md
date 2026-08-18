# Backport gap check

Compares branches by **patch content** (`git cherry` / patch-id), not commit SHAs — correct for cherry-pick backports. Resolves the current maintenance and DU/release branches dynamically so you do not hardcode monthly branch names.

## Quick start

```bash
# from repo root
pip install pyyaml
python scripts/backport-gap-check/check_backport_gaps.py
```

Reports land in `scripts/backport-gap-check/out/`:

- `backport-gaps-latest.md`
- `backport-gaps-latest.json`

## Default comparisons

Configured in [`config.yaml`](config.yaml):

| Name | Source (auto) | Target |
|------|---------------|--------|
| `maintenance-to-master` | latest `bugfix/maintenance/{N}` | `master` |
| `du-to-maintenance` | latest `releases/{X.Y.Z}` | latest maintenance |

### Discovery rules

- **Maintenance**: highest integer matching `^bugfix/maintenance/([0-9]+)$` (e.g. `46`, not `45.1.0` / `_v2` / `-pr*`).
- **DU/release**: highest semver matching `^releases/([0-9]+)\.([0-9]+)\.([0-9]+)$`.

### Pin a branch for one month (optional)

In `config.yaml`:

```yaml
discovery:
  overrides:
    maintenance: bugfix/maintenance/46
    release_du: releases/45.3.1
    master: master
```

Clear or omit `overrides` to go back to auto.

## Ad-hoc comparison

```bash
python scripts/backport-gap-check/check_backport_gaps.py \
  --source bugfix/maintenance/45 \
  --target master \
  --name maint45-to-master
```

## Flags

| Flag | Meaning |
|------|---------|
| `--no-fetch` | Skip `git fetch` |
| `--fail-on-gaps` | Exit `1` if any missing patches |
| `--config PATH` | Alternate config |

Set `report.fail_on_gaps: true` in YAML to fail the job by default.

## GitHub Actions

Workflow: [`.github/workflows/backport-gap-check.yml`](../../.github/workflows/backport-gap-check.yml)

- **workflow_dispatch** (manual) with optional source/target overrides
- **schedule** (weekly Monday 09:00 UTC)

Open the run → job summary shows the Markdown report.

## Interpreting results

- **Missing (+)**: patch from source not found on target → likely needs backport (or an open PR).
- **Equivalent (-)**: same patch already on target under a different SHA → OK.
- Open PRs targeting the target branch whose titles mention related tickets are listed when `gh` auth is available.

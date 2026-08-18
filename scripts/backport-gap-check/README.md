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
| `du-to-maintenance` | latest `releases/{N}` / `{N.Y}` / `{N.Y.Z}` on the same major as maintenance | latest maintenance |

### Discovery rules

- **Maintenance**: highest integer matching `^bugfix/maintenance/([0-9]+)$` (e.g. `46`, not `45.1.0` / `_v2` / `-pr*`).
- **DU/release**: matches `releases/46`, `releases/46.1`, or `releases/46.1.0`. Picks the highest version on the **same major** as maintenance (so `bugfix/maintenance/46` pairs with `releases/46.1.0`, not `releases/45.3.1`). If no DU branch matches, **DU verification is skipped** and `maintenance-to-master` still runs.

### Pin a branch for one month (optional)

In `config.yaml`:

```yaml
discovery:
  overrides:
    maintenance: bugfix/maintenance/46
    release_du: releases/46.1.0
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

## Slack

The job posts a **compact** summary (ticket counts, authors, open PRs, link back to the Actions run). Full tables stay in the Markdown/JSON artifacts.

### 1. Create an incoming webhook

1. In Slack: **Apps** → **Incoming Webhooks** (or create a Slack app with Incoming Webhooks enabled).
2. Add it to the channel (for example `#cra-release`).
3. Copy the `https://hooks.slack.com/services/...` URL.

### 2. Store it as a GitHub Actions secret

Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**:

| Name | Value |
|------|--------|
| `BACKPORT_GAP_SLACK_WEBHOOK` | the webhook URL |

Do not put the URL in `config.yaml` or commit it.

Weekly schedule runs post automatically once the secret exists. Manual runs have a **notify_slack** checkbox (on by default). If the secret is missing, Slack is skipped and the check still succeeds.

### 3. Post locally (optional)

```bash
python scripts/backport-gap-check/check_backport_gaps.py --no-fetch \
  --slack-webhook "$SLACK_WEBHOOK_URL"
```

Or POST the generated payload:

```bash
curl -sS -X POST -H "Content-type: application/json" \
  --data-binary @scripts/backport-gap-check/out/backport-gaps-latest.slack.json \
  "$SLACK_WEBHOOK_URL"
```

## Interpreting results

- **Missing (+)**: patch from source not found on target → likely needs backport (or an open PR).
- **Equivalent (-)**: same patch already on target under a different SHA → OK.
- Open PRs targeting the target branch whose titles mention related tickets are listed when `gh` auth is available.

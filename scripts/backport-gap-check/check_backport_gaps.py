#!/usr/bin/env python3
"""
Backport gap checker — compares branches by patch-id (git cherry), not commit SHA.

Resolves monthly-moving maintenance / DU release branches from config.yaml,
runs each comparison, and writes Markdown + JSON reports with authors and PRs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


TICKET_RE = re.compile(
    r"(?i)\b((?:JZ|RSKDEV|RSKDEC|TPMDEV|TPMP)-\d+)\b"
)


def run(cmd: List[str], check: bool = True) -> str:
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(cmd)}\n{result.stderr}"
        )
    return result.stdout


def run_lines(cmd: List[str], check: bool = True) -> List[str]:
    out = run(cmd, check=check).strip()
    return [ln for ln in out.splitlines() if ln.strip()]


def load_yaml(path: Path) -> Dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError:
        print(
            "PyYAML is required. Install with: pip install pyyaml",
            file=sys.stderr,
        )
        sys.exit(2)
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def remote_branches(remote: str) -> List[str]:
    # refs/remotes/origin/foo -> foo
    lines = run_lines(["git", "branch", "-r", "--format=%(refname:short)"])
    prefix = f"{remote}/"
    names = []
    for ln in lines:
        if ln.startswith(prefix):
            names.append(ln[len(prefix) :])
    return names


def _match_version(m: "re.Match[str]") -> Tuple[int, int, int]:
    major = int(m.group(1))
    minor = int(m.group(2)) if m.lastindex and m.lastindex >= 2 and m.group(2) else 0
    patch = int(m.group(3)) if m.lastindex and m.lastindex >= 3 and m.group(3) else 0
    return (major, minor, patch)


def resolve_discovery(
    branches: List[str],
    rule: Dict[str, Any],
    overrides: Dict[str, Any],
    key: str,
    major_filter: Optional[int] = None,
) -> str:
    if overrides and overrides.get(key):
        return str(overrides[key])

    pattern = re.compile(rule["pattern"])
    strategy = rule.get("strategy", "highest_int_group_1")

    def collect(filter_major: Optional[int]) -> List[Tuple[Any, str]]:
        found: List[Tuple[Any, str]] = []
        for name in branches:
            m = pattern.match(name)
            if not m:
                continue
            if strategy == "highest_int_group_1":
                major = int(m.group(1))
                if filter_major is not None and major != filter_major:
                    continue
                found.append((major, name))
            elif strategy == "highest_semver":
                ver = _match_version(m)
                if filter_major is not None and ver[0] != filter_major:
                    continue
                found.append((ver, name))
            else:
                raise ValueError(f"Unknown discovery strategy: {strategy}")
        return found

    matches = collect(major_filter)
    if not matches and major_filter is not None:
        matches = collect(None)

    if not matches:
        raise RuntimeError(
            f"No branches matched discovery for '{key}' "
            f"(pattern={rule['pattern']!r}). Pin an override in config.yaml."
        )
    matches.sort(key=lambda x: x[0])
    return matches[-1][1]


def resolve_ref(
    token: str,
    remote: str,
    resolved: Dict[str, str],
) -> str:
    """Map 'auto:maintenance' / 'master' / explicit branch to origin/branch."""
    if token.startswith("auto:"):
        key = token.split(":", 1)[1]
        if key not in resolved:
            raise RuntimeError(f"Unknown auto alias '{token}'. Known: {list(resolved)}")
        branch = resolved[key]
    else:
        branch = token
    return f"{remote}/{branch}", branch


@dataclass
class MissingCommit:
    sha: str
    short_sha: str
    author: str
    email: str
    date: str
    subject: str
    tickets: List[str] = field(default_factory=list)


@dataclass
class ComparisonResult:
    name: str
    description: str
    source_branch: str
    target_branch: str
    missing: List[MissingCommit]
    equivalent_count: int
    open_prs: List[Dict[str, Any]] = field(default_factory=list)


def git_cherry(target_ref: str, source_ref: str) -> Tuple[List[str], List[str]]:
    """
    Returns (missing_shas, equivalent_shas) from `git cherry -v target source`.
    '+' = patch not on target; '-' = equivalent patch already on target.
    """
    lines = run_lines(["git", "cherry", "-v", target_ref, source_ref], check=True)
    missing: List[str] = []
    equivalent: List[str] = []
    for ln in lines:
        if ln.startswith("+ "):
            missing.append(ln[2:].split()[0])
        elif ln.startswith("- "):
            equivalent.append(ln[2:].split()[0])
    return missing, equivalent


def commit_details(sha: str) -> MissingCommit:
    fmt = "%H|%h|%an|%ae|%ad|%s"
    line = run(
        ["git", "log", "-1", f"--pretty=format:{fmt}", "--date=short", sha]
    ).strip()
    parts = line.split("|", 5)
    subject = parts[5] if len(parts) > 5 else ""
    tickets = sorted(set(TICKET_RE.findall(subject)))
    return MissingCommit(
        sha=parts[0],
        short_sha=parts[1],
        author=parts[2],
        email=parts[3],
        date=parts[4],
        subject=subject,
        tickets=tickets,
    )


def should_ignore(subject: str, patterns: List[str]) -> bool:
    for p in patterns:
        if re.search(p, subject):
            return True
    return False


def find_open_prs(repo: str, base_branch: str, ticket_hints: List[str]) -> List[Dict[str, Any]]:
    if not ticket_hints:
        # Still search by base branch for open PRs (limited)
        search = f'base:"{base_branch}" is:open'
    else:
        # OR tickets; keep query short
        unique = sorted(set(ticket_hints))[:8]
        ticket_q = " OR ".join(unique)
        search = f'base:"{base_branch}" is:open ({ticket_q})'

    try:
        out = run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--base",
                base_branch,
                "--limit",
                "30",
                "--json",
                "number,title,url,author,createdAt,updatedAt",
            ],
            check=False,
        )
        if not out.strip():
            return []
        prs = json.loads(out)
    except Exception:
        return []

    if not ticket_hints:
        return prs[:10]

    hints_upper = {t.upper() for t in ticket_hints}
    filtered = []
    for pr in prs:
        title = (pr.get("title") or "").upper()
        if any(h in title for h in hints_upper):
            filtered.append(pr)
    return filtered


def detect_repo() -> str:
    try:
        url = run(["git", "remote", "get-url", "origin"]).strip()
    except RuntimeError:
        return "coupa/cra"
    # https://github.com/coupa/cra.git or git@github.com:coupa/cra.git
    m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
    return m.group(1) if m else "coupa/cra"


def group_by_ticket(commits: List[MissingCommit]) -> Dict[str, List[MissingCommit]]:
    groups: Dict[str, List[MissingCommit]] = defaultdict(list)
    for c in commits:
        if c.tickets:
            for t in c.tickets:
                groups[t].append(c)
        else:
            groups["(no ticket)"].append(c)
    return dict(sorted(groups.items(), key=lambda kv: kv[0]))


def render_markdown(
    results: List[ComparisonResult],
    resolved: Dict[str, str],
    generated_at: str,
) -> str:
    lines: List[str] = []
    lines.append("# Backport gap report")
    lines.append("")
    lines.append(f"Generated: `{generated_at}`")
    lines.append("")
    lines.append("## Resolved branches")
    lines.append("")
    for k, v in resolved.items():
        lines.append(f"- **{k}**: `{v}`")
    lines.append("")
    lines.append("Method: `git cherry` (patch-id). Cherry-picked commits with different SHAs count as present.")
    lines.append("")

    for r in results:
        lines.append(f"## {r.name}")
        lines.append("")
        lines.append(f"- **Source**: `{r.source_branch}`")
        lines.append(f"- **Target**: `{r.target_branch}`")
        lines.append(f"- **Description**: {r.description}")
        lines.append(f"- **Missing patches**: **{len(r.missing)}**")
        lines.append(f"- **Already equivalent on target**: {r.equivalent_count}")
        lines.append("")

        if not r.missing:
            lines.append("No missing patches.")
            lines.append("")
            continue

        groups = group_by_ticket(r.missing)
        lines.append("### By ticket")
        lines.append("")
        for ticket, commits in groups.items():
            authors = sorted({c.author for c in commits})
            lines.append(f"#### {ticket}")
            lines.append("")
            lines.append(f"- Authors: {', '.join(authors)}")
            lines.append(f"- Commits: {len(commits)}")
            lines.append("")
            lines.append("| SHA | Date | Author | Subject |")
            lines.append("|-----|------|--------|---------|")
            for c in commits:
                subj = c.subject.replace("|", "\\|")
                lines.append(
                    f"| `{c.short_sha}` | {c.date} | {c.author} | {subj} |"
                )
            lines.append("")

        if r.open_prs:
            lines.append("### Related open PRs targeting target branch")
            lines.append("")
            for pr in r.open_prs:
                login = (pr.get("author") or {}).get("login", "?")
                lines.append(
                    f"- [#{pr['number']}]({pr['url']}) — {pr['title']} (@{login})"
                )
            lines.append("")

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Check cherry-pick backport gaps")
    parser.add_argument(
        "--config",
        default=str(
            Path(__file__).resolve().parent / "config.yaml"
        ),
        help="Path to config.yaml",
    )
    parser.add_argument("--source", help="Override single comparison source branch")
    parser.add_argument("--target", help="Override single comparison target branch")
    parser.add_argument("--name", default="adhoc", help="Name for ad-hoc comparison")
    parser.add_argument("--no-fetch", action="store_true", help="Skip git fetch")
    parser.add_argument(
        "--fail-on-gaps",
        action="store_true",
        help="Exit 1 if any missing patches (overrides config)",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    cfg = load_yaml(config_path)
    remote = cfg.get("remote", "origin")
    discovery = cfg.get("discovery", {})
    overrides = discovery.get("overrides") or {}
    report_cfg = cfg.get("report", {})
    ignore_regexes = (cfg.get("ignore") or {}).get("commit_message_regexes") or []

    if not args.no_fetch:
        print(f"Fetching {remote} (maintenance + releases + master)...")
        # Best-effort fetch of patterns; full remote fetch if needed
        subprocess.run(
            ["git", "fetch", remote, "--prune"],
            check=False,
        )

    # Ad-hoc --source/--target skips discovery so repos without
    # bugfix/maintenance/* or releases/* still work (e.g. a test repo).
    skip_discovery = bool(args.source and args.target)

    branches = remote_branches(remote)
    resolved: Dict[str, str] = {}
    resolved["master"] = overrides.get("master") or "master"

    if not skip_discovery:
        if "maintenance" in discovery:
            resolved["maintenance"] = resolve_discovery(
                branches, discovery["maintenance"], overrides, "maintenance"
            )
        if "release_du" in discovery:
            maint_major = None
            rule = discovery["release_du"]
            if rule.get("align_major_with") == "maintenance" and "maintenance" in resolved:
                maj = re.search(r"/([0-9]+)$", resolved["maintenance"])
                if maj:
                    maint_major = int(maj.group(1))
            resolved["release_du"] = resolve_discovery(
                branches, rule, overrides, "release_du", major_filter=maint_major
            )

    print("Resolved branches:")
    for k, v in resolved.items():
        print(f"  {k}: {v}")

    comparisons: List[Dict[str, Any]]
    if args.source and args.target:
        comparisons = [
            {
                "name": args.name,
                "description": "Ad-hoc comparison",
                "source": args.source,
                "target": args.target,
            }
        ]
    else:
        comparisons = cfg.get("comparisons") or []
        if not comparisons:
            print("No comparisons configured.", file=sys.stderr)
            return 2

    repo = detect_repo()
    results: List[ComparisonResult] = []
    max_listed = int(report_cfg.get("max_commits_listed", 50))

    for cmp in comparisons:
        source_ref, source_branch = resolve_ref(cmp["source"], remote, resolved)
        target_ref, target_branch = resolve_ref(cmp["target"], remote, resolved)
        print(f"\n=== {cmp['name']}: {source_branch} -> {target_branch} ===")

        # Ensure refs exist
        run(["git", "rev-parse", "--verify", source_ref])
        run(["git", "rev-parse", "--verify", target_ref])

        missing_shas, equivalent_shas = git_cherry(target_ref, source_ref)
        missing_commits: List[MissingCommit] = []
        for sha in missing_shas:
            detail = commit_details(sha)
            if should_ignore(detail.subject, ignore_regexes):
                continue
            missing_commits.append(detail)

        missing_commits = missing_commits[:max_listed]
        tickets = [t for c in missing_commits for t in c.tickets]

        open_prs: List[Dict[str, Any]] = []
        if report_cfg.get("include_open_prs", True) and missing_commits:
            open_prs = find_open_prs(repo, target_branch, tickets)

        result = ComparisonResult(
            name=cmp["name"],
            description=cmp.get("description", ""),
            source_branch=source_branch,
            target_branch=target_branch,
            missing=missing_commits,
            equivalent_count=len(equivalent_shas),
            open_prs=open_prs,
        )
        results.append(result)
        print(
            f"Missing: {len(missing_commits)} | Equivalent on target: {len(equivalent_shas)}"
        )

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    md = render_markdown(results, resolved, generated_at)

    out_dir = Path(report_cfg.get("output_dir", "scripts/backport-gap-check/out"))
    if not out_dir.is_absolute():
        # relative to repo root (cwd expected at repo root)
        out_dir = Path.cwd() / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    if report_cfg.get("write_markdown", True):
        md_path = out_dir / f"backport-gaps-{stamp}.md"
        md_path.write_text(md, encoding="utf-8")
        latest_md = out_dir / "backport-gaps-latest.md"
        latest_md.write_text(md, encoding="utf-8")
        print(f"\nWrote {md_path}")
        print(f"Wrote {latest_md}")

    if report_cfg.get("write_json", True):
        payload = {
            "generated_at": generated_at,
            "resolved_branches": resolved,
            "comparisons": [
                {
                    "name": r.name,
                    "description": r.description,
                    "source_branch": r.source_branch,
                    "target_branch": r.target_branch,
                    "equivalent_count": r.equivalent_count,
                    "missing_count": len(r.missing),
                    "missing": [asdict(c) for c in r.missing],
                    "open_prs": r.open_prs,
                }
                for r in results
            ],
        }
        json_path = out_dir / f"backport-gaps-{stamp}.json"
        json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        (out_dir / "backport-gaps-latest.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
        print(f"Wrote {json_path}")

    # Always print summary markdown to stdout for Actions step summary
    print("\n" + md)

    gha_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if gha_summary:
        with open(gha_summary, "a", encoding="utf-8") as f:
            f.write(md)

    fail = args.fail_on_gaps or report_cfg.get("fail_on_gaps", False)
    if fail and any(r.missing for r in results):
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)

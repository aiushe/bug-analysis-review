#!/usr/bin/env python3
"""
write_back.py — apply a review plan back to Jira.

The skill produces a plan_<version>.json file describing two actions per bug:
  1. Set PM RCA Type to one of the allowed values
  2. Link the bug to a feature issue via an Issue Link

This script discovers the PM RCA Type custom field ID, validates the values,
discovers the issue link type, then applies the plan. Always do --dry-run
first.

PLAN FILE SCHEMA
    {
      "meta": { "version": "1.2.3.0", "generated_at": "...", ... },
      "link_type": "Requirement Link",   // optional; default "Requirement Link"
      "actions": [
        {
          "bug_key": "PROJ-123",
          "set_pm_rca_type": "Incomplete requirements",  // or null to skip
          "link_to_feature": "PROJ-93467",               // or null to skip
          "skip_reason": null
        },
        ...
      ]
    }

USAGE
    python3 write_back.py --plan plan_1.2.2.json --dry-run
    python3 write_back.py --plan plan_1.2.2.json          # actually applies

SAFETY RAILS
    - Dry-run prints every intended change and writes nothing.
    - Never overwrites an existing non-empty PM RCA Type value.
    - Never creates a duplicate link if one already exists.
    - Per-bug failures are logged but don't abort the whole run.
    - --limit N applies only the first N actions (for canary runs).
    - A plan with meta.tier == "review" is refused unless --allow-review-tier
      is passed. run_review.py never passes that flag anywhere in its own
      source, so applying the review tier (medium/low-confidence RCA calls,
      PRD-content feature matches) structurally requires a human to type the
      flag by hand — this is what makes "PM confirms before write-back" a
      code-enforced precondition instead of a documentation convention.
    - set_pm_analysis actions are skipped unless --allow-pm-analysis is
      passed, independent of who calls this script — PM Analysis free text
      is drafted for review, never auto-written (SKILL.md §5b).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib import error, parse, request

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from pull_bugs import load_dotenv, resolve_base_url, resolve_token  # type: ignore

# Display names of the two custom fields this script writes; both are looked up
# by name at runtime, so only the labels have to match your Jira. Override with
# JIRA_RCA_FIELD_NAME / JIRA_ANALYSIS_FIELD_NAME if yours are labelled
# differently.
PM_RCA_TYPE_FIELD_NAME = os.environ.get("JIRA_RCA_FIELD_NAME", "PM RCA Type")
PM_ANALYSIS_FIELD_NAME = os.environ.get("JIRA_ANALYSIS_FIELD_NAME", "PM Analysis")
# Exact strings from the RCA dropdown. NOTE the EN-DASH in
# "Requirement–expectation mismatch" (not a hyphen); the field is expected to
# have NO "None" option, so "None" is never written.
ALLOWED_RCA_VALUES = {
    "Incomplete requirements",
    "Requirement–expectation mismatch",
    "Missed review by PM/QE",
    "Feature gap",
    "Invalid defect",
}
# Link type used for bug→feature links. Not every Jira has "Relates" — the
# script lists the site's real link types when the configured one is missing.
DEFAULT_LINK_TYPE = os.environ.get("JIRA_LINK_TYPE", "Requirement Link")


def http_request(
    method: str,
    url: str,
    token: str,
    body: dict[str, Any] | None = None,
) -> tuple[int, str]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "bug-review/1.0",
        },
    )
    try:
        with request.urlopen(req, timeout=60) as resp:
            return resp.getcode(), resp.read().decode("utf-8", errors="replace")
    except error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")[:2000]
    except error.URLError as e:
        return 0, str(e)


def discover_field_id(base_url: str, token: str, field_name: str) -> str | None:
    code, body = http_request("GET", f"{base_url.rstrip('/')}/rest/api/2/field", token)
    if code != 200:
        print(f"  field discovery failed: HTTP {code}", file=sys.stderr)
        return None
    fields = json.loads(body)
    for f in fields:
        if f.get("name", "").strip().lower() == field_name.strip().lower():
            return f.get("id")
    return None


def get_issue(
    base_url: str, token: str, key: str, extra_fields: list[str] | None = None
) -> dict[str, Any] | None:
    fields = ["issuelinks", "summary", "issuetype"] + (extra_fields or [])
    code, body = http_request(
        "GET",
        f"{base_url.rstrip('/')}/rest/api/2/issue/{key}?fields={','.join(fields)}",
        token,
    )
    if code != 200:
        return None
    return json.loads(body)


def has_existing_link(issue: dict[str, Any], other_key: str) -> bool:
    for link in (issue.get("fields", {}) or {}).get("issuelinks", []) or []:
        other = link.get("outwardIssue") or link.get("inwardIssue") or {}
        if other.get("key") == other_key:
            return True
    return False


def existing_pm_rca_value(issue: dict[str, Any], field_id: str) -> str | None:
    fields = issue.get("fields", {}) or {}
    v = fields.get(field_id)
    if isinstance(v, dict):
        return v.get("value") or v.get("name")
    if isinstance(v, str) and v.strip():
        return v
    return None


def set_pm_rca_type(
    base_url: str,
    token: str,
    key: str,
    field_id: str,
    value: str,
    dry_run: bool,
) -> tuple[bool, str]:
    if dry_run:
        return True, f"DRY: would set {field_id} ({PM_RCA_TYPE_FIELD_NAME}) = {value!r}"
    payload = {"fields": {field_id: {"value": value}}}
    code, body = http_request(
        "PUT",
        f"{base_url.rstrip('/')}/rest/api/2/issue/{key}",
        token,
        body=payload,
    )
    if code in (200, 204):
        return True, f"set {PM_RCA_TYPE_FIELD_NAME} = {value!r}"
    return False, f"HTTP {code}: {body[:300]}"


def existing_text_value(issue: dict[str, Any], field_id: str) -> str | None:
    v = (issue.get("fields", {}) or {}).get(field_id)
    if isinstance(v, str) and v.strip():
        return v
    return None


def set_pm_analysis(
    base_url: str,
    token: str,
    key: str,
    field_id: str,
    value: str,
    dry_run: bool,
) -> tuple[bool, str]:
    if dry_run:
        preview = value if len(value) <= 80 else value[:77] + "..."
        return True, f"DRY: would set {field_id} ({PM_ANALYSIS_FIELD_NAME}) = {preview!r}"
    payload = {"fields": {field_id: value}}
    code, body = http_request(
        "PUT",
        f"{base_url.rstrip('/')}/rest/api/2/issue/{key}",
        token,
        body=payload,
    )
    if code in (200, 204):
        return True, f"set {PM_ANALYSIS_FIELD_NAME} ({len(value)} chars)"
    return False, f"HTTP {code}: {body[:300]}"


def create_link(
    base_url: str,
    token: str,
    bug_key: str,
    feature_key: str,
    link_type: str,
    dry_run: bool,
) -> tuple[bool, str]:
    if dry_run:
        return True, f"DRY: would link {bug_key} --[{link_type}]--> {feature_key}"
    payload = {
        "type": {"name": link_type},
        "inwardIssue": {"key": bug_key},
        "outwardIssue": {"key": feature_key},
    }
    code, body = http_request(
        "POST",
        f"{base_url.rstrip('/')}/rest/api/2/issueLink",
        token,
        body=payload,
    )
    if code in (200, 201, 204):
        return True, f"linked {bug_key} -> {feature_key} ({link_type})"
    return False, f"HTTP {code}: {body[:300]}"


def list_link_types(base_url: str, token: str) -> list[str]:
    code, body = http_request(
        "GET", f"{base_url.rstrip('/')}/rest/api/2/issueLinkType", token
    )
    if code != 200:
        return []
    data = json.loads(body)
    return [t.get("name", "") for t in data.get("issueLinkTypes", []) or []]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Apply review plan to Jira.")
    p.add_argument("--plan", required=True, help="Path to plan_<version>.json.")
    p.add_argument("--dry-run", action="store_true", help="Print intended changes only.")
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Apply only the first N actions (canary run).",
    )
    p.add_argument(
        "--base-url",
        default=None,
        help="Jira base URL. Default: $JIRA_BASE_URL (env or scripts/.env).",
    )
    p.add_argument(
        "--allow-review-tier",
        action="store_true",
        help=(
            "Required to apply a plan whose meta.tier == 'review' (medium/low-confidence "
            "RCA calls, PRD-content feature matches). A PM must have actually looked at "
            "these per SKILL.md before this flag is passed."
        ),
    )
    p.add_argument(
        "--allow-pm-analysis",
        action="store_true",
        help="Required for any action carrying set_pm_analysis to actually write. Off by default.",
    )
    p.add_argument(
        "--summary-out",
        default=None,
        help="Optional path to write the summary counters as JSON (for orchestration).",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv(SCRIPT_DIR)

    token = resolve_token(SCRIPT_DIR)
    base_url = resolve_base_url(args.base_url)

    plan_path = Path(args.plan)
    plan = json.loads(plan_path.read_text())
    actions = plan.get("actions", []) or []
    link_type = plan.get("link_type", DEFAULT_LINK_TYPE)

    tier = (plan.get("meta") or {}).get("tier")
    if tier == "review" and not args.allow_review_tier:
        print(
            f"\n  {plan_path} is tier='review' — medium/low-confidence RCA calls and/or "
            "PRD-content feature links that SKILL.md requires a PM to confirm before "
            "write-back. Re-run with --allow-review-tier once you have reviewed "
            f"{plan_path.stem.replace('.review', '')}'s NEEDS_REVIEW doc.\n",
            file=sys.stderr,
        )
        return 5

    if args.limit:
        actions = actions[: args.limit]

    mode = "DRY RUN" if args.dry_run else "APPLY"
    print(f"=== {mode}: {len(actions)} actions from {plan_path} ===")
    print(f"  link type: {link_type}")
    print(f"  base url:  {base_url}")

    # Discover field ID for PM RCA Type once.
    field_id = discover_field_id(base_url, token, PM_RCA_TYPE_FIELD_NAME)
    if not field_id:
        print(
            f"\n  could not find a JIRA field named {PM_RCA_TYPE_FIELD_NAME!r}.\n"
            "  Available link types: " + ", ".join(list_link_types(base_url, token)),
            file=sys.stderr,
        )
        return 3
    print(f"  field id:  {field_id} ({PM_RCA_TYPE_FIELD_NAME})")

    # Discover field ID for PM Analysis (free-text). Optional — only needed if any
    # action carries set_pm_analysis AND --allow-pm-analysis was passed. Without the
    # flag, PM Analysis writes are skipped regardless of what the plan requests —
    # this field is drafted for review, never auto-written (SKILL.md §5b), and that
    # invariant lives here rather than depending on every caller remembering it.
    analysis_requested = sum(1 for a in actions if a.get("set_pm_analysis"))
    wants_analysis = analysis_requested > 0 and args.allow_pm_analysis
    analysis_field_id = None
    if analysis_requested and not args.allow_pm_analysis:
        print(
            f"\n  {analysis_requested} action(s) request PM Analysis writes — skipped "
            "(pass --allow-pm-analysis to enable; RCA/link actions still apply).",
            file=sys.stderr,
        )
    if wants_analysis:
        analysis_field_id = discover_field_id(base_url, token, PM_ANALYSIS_FIELD_NAME)
        if not analysis_field_id:
            print(
                f"\n  could not find a JIRA field named {PM_ANALYSIS_FIELD_NAME!r}; "
                "PM Analysis will be skipped.",
                file=sys.stderr,
            )
        else:
            print(f"  field id:  {analysis_field_id} ({PM_ANALYSIS_FIELD_NAME})")

    # Sanity-check link type exists.
    known_link_types = list_link_types(base_url, token)
    if known_link_types and link_type not in known_link_types:
        print(
            f"\n  link type {link_type!r} not found in Jira. Available: "
            f"{known_link_types}\n  Edit plan.link_type and retry.",
            file=sys.stderr,
        )
        return 4

    summary = {"applied": 0, "skipped": 0, "failed": 0}

    for i, action in enumerate(actions, 1):
        bug_key = action.get("bug_key")
        rca_value = action.get("set_pm_rca_type")
        analysis_value = action.get("set_pm_analysis")
        feature_key = action.get("link_to_feature")
        skip_reason = action.get("skip_reason")
        prefix = f"[{i}/{len(actions)}] {bug_key}"

        if skip_reason:
            print(f"{prefix}  SKIP: {skip_reason}")
            summary["skipped"] += 1
            continue
        if not bug_key:
            print(f"{prefix}  SKIP: no bug_key")
            summary["skipped"] += 1
            continue

        extra = [field_id] + ([analysis_field_id] if analysis_field_id else [])
        issue = get_issue(base_url, token, bug_key, extra_fields=extra)
        if not issue:
            print(f"{prefix}  FAIL: could not fetch issue (PAT permission?)")
            summary["failed"] += 1
            continue

        applied_any = False

        # 1. PM RCA Type
        if rca_value:
            if rca_value not in ALLOWED_RCA_VALUES:
                print(f"{prefix}  SKIP: invalid RCA value {rca_value!r}")
                summary["skipped"] += 1
                continue
            existing = existing_pm_rca_value(issue, field_id)
            if existing:
                print(
                    f"{prefix}  SKIP RCA: already set to {existing!r}, "
                    "not overwriting"
                )
            else:
                ok, msg = set_pm_rca_type(
                    base_url, token, bug_key, field_id, rca_value, args.dry_run
                )
                print(f"{prefix}  {'OK' if ok else 'FAIL'} RCA: {msg}")
                applied_any |= ok
                if not ok:
                    summary["failed"] += 1
                    continue

        # 2. PM Analysis (free text) — never overwrite an existing value.
        if analysis_value and analysis_field_id:
            existing_a = existing_text_value(issue, analysis_field_id)
            if existing_a:
                print(
                    f"{prefix}  SKIP PM Analysis: already populated "
                    f"({len(existing_a)} chars), not overwriting"
                )
            else:
                ok, msg = set_pm_analysis(
                    base_url, token, bug_key, analysis_field_id, analysis_value, args.dry_run
                )
                print(f"{prefix}  {'OK' if ok else 'FAIL'} PM Analysis: {msg}")
                applied_any |= ok
                if not ok:
                    summary["failed"] += 1
                    continue
        elif analysis_value and not analysis_field_id:
            reason = "not discovered" if args.allow_pm_analysis else "--allow-pm-analysis not passed"
            print(f"{prefix}  SKIP PM Analysis: field {reason}")

        # 3. Feature link
        if feature_key:
            if has_existing_link(issue, feature_key):
                print(f"{prefix}  SKIP LINK: {bug_key}->{feature_key} already exists")
            else:
                ok, msg = create_link(
                    base_url, token, bug_key, feature_key, link_type, args.dry_run
                )
                print(f"{prefix}  {'OK' if ok else 'FAIL'} LINK: {msg}")
                applied_any |= ok
                if not ok:
                    summary["failed"] += 1
                    continue

        if applied_any:
            summary["applied"] += 1
        elif not (rca_value or analysis_value or feature_key):
            summary["skipped"] += 1

    print("\n=== SUMMARY ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    if args.dry_run:
        print("\n  This was a dry run. Re-run without --dry-run to apply.")

    if args.summary_out:
        Path(args.summary_out).write_text(json.dumps(summary, indent=2) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())

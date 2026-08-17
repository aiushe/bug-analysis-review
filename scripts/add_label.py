#!/usr/bin/env python3
"""
add_label.py — add a label to every Jira issue matching a JQL.

Reuses pull_bugs.py machinery (token, base URL, retrying http_get). Appends the
label via the Jira REST "update" verb with {"add": ...} so existing labels are
never overwritten and re-running is idempotent (Jira ignores an add of a label
already present, and we skip it client-side too).

USAGE
    python3 add_label.py --jql '<jql>' --label Must_Have_1.2.4 --dry-run
    python3 add_label.py --jql '<jql>' --label Must_Have_1.2.4 --limit 5
    python3 add_label.py --jql '<jql>' --label Must_Have_1.2.4
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib import error, parse, request

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from pull_bugs import http_get, load_dotenv, resolve_base_url, resolve_token  # type: ignore


def search_keys_and_labels(base_url: str, token: str, jql: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    start_at = 0
    total = None
    while True:
        params = parse.urlencode({
            "jql": jql,
            "startAt": start_at,
            "maxResults": 100,
            "fields": "summary,labels,issuetype,status",
        })
        url = f"{base_url.rstrip('/')}/rest/api/2/search?{params}"
        page = http_get(url, token)
        issues = page.get("issues", []) or []
        if total is None:
            total = page.get("total", 0)
            print(f"  total matched: {total}")
        for i in issues:
            f = i.get("fields", {}) or {}
            out.append({
                "key": i.get("key"),
                "summary": f.get("summary"),
                "labels": f.get("labels", []) or [],
                "issuetype": (f.get("issuetype") or {}).get("name"),
                "status": (f.get("status") or {}).get("name"),
            })
        start_at += len(issues)
        if start_at >= total or not issues:
            break
    return out


def add_label(base_url: str, token: str, key: str, label: str) -> tuple[bool, str]:
    payload = {"update": {"labels": [{"add": label}]}}
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(
        f"{base_url.rstrip('/')}/rest/api/2/issue/{key}",
        data=data,
        method="PUT",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "bug-review-label/1.0",
        },
    )
    try:
        with request.urlopen(req, timeout=60) as resp:
            return resp.getcode() in (200, 204), f"HTTP {resp.getcode()}"
    except error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
    except error.URLError as e:
        return False, str(e)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Add a label to all issues matching a JQL.")
    p.add_argument("--jql", required=True)
    p.add_argument("--label", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=None, help="Apply only first N.")
    p.add_argument("--reference", default=None, help="Issue key to print labels for (sanity check).")
    p.add_argument("--base-url", default=None,
                   help="Jira base URL. Default: $JIRA_BASE_URL (env or scripts/.env).")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv(SCRIPT_DIR)
    token = resolve_token(SCRIPT_DIR)
    base_url = resolve_base_url(args.base_url)

    if args.reference:
        ref_url = f"{base_url.rstrip('/')}/rest/api/2/issue/{args.reference}?fields=labels,summary"
        ref = http_get(ref_url, token)
        print(f"  REFERENCE {args.reference}: labels = {(ref.get('fields') or {}).get('labels')}")
        print()

    print(f"  JQL: {args.jql}")
    issues = search_keys_and_labels(base_url, token, args.jql)
    if args.limit:
        issues = issues[: args.limit]

    mode = "DRY RUN" if args.dry_run else "APPLY"
    print(f"\n=== {mode}: add label {args.label!r} to {len(issues)} issue(s) ===\n")

    summary = {"added": 0, "already": 0, "failed": 0}
    for i, it in enumerate(issues, 1):
        key = it["key"]
        prefix = f"[{i}/{len(issues)}] {key:<12} ({it['issuetype']})"
        if args.label in it["labels"]:
            print(f"{prefix}  SKIP: already has label. labels={it['labels']}")
            summary["already"] += 1
            continue
        if args.dry_run:
            print(f"{prefix}  DRY: would add. current labels={it['labels']}")
            continue
        ok, msg = add_label(base_url, token, key, args.label)
        if ok:
            print(f"{prefix}  OK: added (was {it['labels']})")
            summary["added"] += 1
        else:
            print(f"{prefix}  FAIL: {msg}")
            summary["failed"] += 1

    print("\n=== SUMMARY ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    if args.dry_run:
        print("\n  Dry run. Re-run without --dry-run to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

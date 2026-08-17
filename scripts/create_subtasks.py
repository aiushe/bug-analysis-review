#!/usr/bin/env python3
"""
create_subtasks.py — create sub-tasks under a parent issue in Jira.

Reuses pull_bugs.py machinery (token, base URL, retrying http_get). Reads a JSON
spec ({meta, subtasks}) like subtasks_<PARENT>.json and POSTs one sub-task per
entry via the Jira REST v2 /issue endpoint.

Idempotent: before creating, it fetches the parent's existing sub-tasks and skips
any whose summary already exists, so re-running after a partial apply is safe.

USAGE
    python3 create_subtasks.py --spec subtasks_PROJ-93631.json --dry-run
    python3 create_subtasks.py --spec subtasks_PROJ-93631.json --limit 3
    python3 create_subtasks.py --spec subtasks_PROJ-93631.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib import error, request

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from pull_bugs import http_get, load_dotenv, resolve_base_url, resolve_token  # type: ignore


def existing_subtask_summaries(base_url: str, token: str, parent: str) -> set[str]:
    url = f"{base_url.rstrip('/')}/rest/api/2/issue/{parent}?fields=subtasks"
    data = http_get(url, token)
    subs = (data.get("fields") or {}).get("subtasks", []) or []
    return {(s.get("fields") or {}).get("summary", "").strip() for s in subs}


def create_subtask(base_url: str, token: str, fields: dict[str, Any]) -> tuple[str | None, str]:
    payload = {"fields": fields}
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(
        f"{base_url.rstrip('/')}/rest/api/2/issue",
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "bug-review-subtask/1.0",
        },
    )
    try:
        with request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body.get("key"), f"HTTP {resp.getcode()}"
    except error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:400]}"
    except error.URLError as e:
        return None, str(e)


def build_fields(meta: dict[str, Any], st: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "project": {"key": meta["project"]},
        "parent": {"key": meta["parent"]},
        "issuetype": {"id": str(meta["issue_type_id"])},
        "summary": st["summary"],
        "description": st.get("description", ""),
    }
    comps = meta.get("components") or []
    if comps:
        fields["components"] = [{"name": c} for c in comps]
    affects = meta.get("affects_versions") or []
    if affects:
        fields["versions"] = [{"name": v} for v in affects]
    # Project/issue-type-mandated custom fields (Regression Type, Reporting Mode,
    # Severity, Customer References, ...). Per-subtask values win over meta.
    for fid, val in (meta.get("required_fields") or {}).items():
        fields.setdefault(fid, val)
    for fid, val in (st.get("fields") or {}).items():
        fields[fid] = val
    return fields


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Create sub-tasks under a parent from a JSON spec.")
    p.add_argument("--spec", required=True, help="Path to the JSON spec (meta + subtasks).")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=None, help="Create only the first N.")
    p.add_argument("--base-url", default=None,
                   help="Jira base URL. Default: $JIRA_BASE_URL (env or scripts/.env).")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv(SCRIPT_DIR)
    token = resolve_token(SCRIPT_DIR)
    base_url = resolve_base_url(args.base_url)

    spec = json.loads(Path(args.spec).read_text())
    meta = spec["meta"]
    subtasks = spec["subtasks"]
    if args.limit:
        subtasks = subtasks[: args.limit]

    parent = meta["parent"]

    existing = existing_subtask_summaries(base_url, token, parent)
    print(f"  Parent {parent}: {len(existing)} existing sub-task(s).")

    mode = "DRY RUN" if args.dry_run else "APPLY"
    print(f"\n=== {mode}: create {len(subtasks)} sub-task(s) under {parent} "
          f"as type id {meta['issue_type_id']} ({meta.get('issue_type_name','')}) ===\n")

    summary = {"created": 0, "skipped": 0, "failed": 0}
    created_keys: list[str] = []
    for i, st in enumerate(subtasks, 1):
        ref = st.get("ref", "")
        label = f"[{i}/{len(subtasks)}] §{ref:<4} {st['summary'][:70]}"
        if st["summary"].strip() in existing:
            print(f"{label}\n    SKIP: a sub-task with this summary already exists.")
            summary["skipped"] += 1
            continue
        if args.dry_run:
            print(f"{label}\n    DRY: would create ({len(st.get('description',''))} chars desc).")
            continue
        fields = build_fields(meta, st)
        key, msg = create_subtask(base_url, token, fields)
        if key:
            print(f"{label}\n    OK: created {key}")
            summary["created"] += 1
            created_keys.append(key)
        else:
            print(f"{label}\n    FAIL: {msg}")
            summary["failed"] += 1

    print("\n=== SUMMARY ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    if created_keys:
        print(f"  created keys: {', '.join(created_keys)}")
    if args.dry_run:
        print("\n  Dry run. Re-run without --dry-run to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
pull_features.py — pull candidate features from Jira for a release.

Run alongside pull_bugs.py. The skill uses both files together: it matches
each bug to a candidate feature, surfaces the mapping for the PM's
confirmation, then write_back.py creates the bug→feature links.

Features live in their own project (set JIRA_FEATURE_PROJECT, or pass
--project) and are usually a dedicated issue type (set JIRA_FEATURE_ISSUETYPES,
or pass --issuetypes). The default JQL pulls those issue types from that
project matching the same fixVersion. Override with --jql if needed.

USAGE
    python3 pull_features.py --version 1.2.2
    python3 pull_features.py --version 1.2.2 \
        --project PROJ --issuetypes "Story,Feature,Epic" \
        --out features_1.2.2.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# Reuse the machinery from pull_bugs.py rather than duplicating it.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from pull_bugs import (  # type: ignore
    load_dotenv,
    resolve_base_url,
    resolve_token,
    search_all,
)


def build_jql(version: str, project: str, issuetypes: list[str], extra: str | None) -> str:
    types = ", ".join(f'"{t}"' for t in issuetypes)
    jql = (
        f'project = "{project}" '
        f'AND fixVersion = "{version}" '
        f"AND issuetype in ({types})"
    )
    if extra:
        jql = f"({jql}) AND ({extra})"
    return jql


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Pull candidate features for a release.")
    p.add_argument("--version", required=True, help='Fix version, e.g. "1.2.2".')
    p.add_argument(
        "--project",
        default=None,
        help="Project key features live in. Default: $JIRA_FEATURE_PROJECT.",
    )
    p.add_argument(
        "--issuetypes",
        # Whatever your Jira calls a feature — an issue type that doesn't exist
        # in the project returns HTTP 400, not an empty result.
        default=None,
        help=('Comma-separated issue types to treat as features. Default: '
              '$JIRA_FEATURE_ISSUETYPES, else "New Feature Request".'),
    )
    p.add_argument(
        "--jql",
        default=None,
        help="Extra JQL appended with AND.",
    )
    p.add_argument("--out", default=None, help="Output JSON. Default: features_<version>.json.")
    p.add_argument(
        "--base-url",
        default=None,
        help="Jira base URL. Default: $JIRA_BASE_URL (env or scripts/.env).",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv(SCRIPT_DIR)

    token = resolve_token(SCRIPT_DIR)
    base_url = resolve_base_url(args.base_url)

    project = args.project or os.environ.get("JIRA_FEATURE_PROJECT", "").strip()
    if not project:
        print(
            "\n  Feature project not set. Pass --project, or set\n"
            "    JIRA_FEATURE_PROJECT=<KEY>\n"
            "  in the environment or scripts/.env.\n",
            file=sys.stderr,
        )
        return 2

    issuetypes_raw = (
        args.issuetypes
        or os.environ.get("JIRA_FEATURE_ISSUETYPES")
        or "New Feature Request"
    )
    issuetypes = [t.strip() for t in issuetypes_raw.split(",") if t.strip()]
    jql = build_jql(args.version, project, issuetypes, args.jql)

    out_path = (
        Path(args.out) if args.out else SCRIPT_DIR / f"features_{args.version}.json"
    )

    print(f"Pulling candidate features for {args.version}")
    print(f"  project: {project}")
    print(f"  issuetypes: {issuetypes}")
    print(f"  JQL: {jql}")
    print(f"  output: {out_path}")

    features = search_all(base_url, token, jql)

    output = {
        "meta": {
            "version": args.version,
            "project": project,
            "issuetypes": issuetypes,
            "jql": jql,
            "base_url": base_url,
            "pulled_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "total": len(features),
        },
        "features": features,
    }
    out_path.write_text(json.dumps(output, indent=2, default=str))
    print(f"\n  Wrote {len(features)} features to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

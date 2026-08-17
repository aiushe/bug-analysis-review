#!/usr/bin/env python3
"""
needs_review.py — writes NEEDS_REVIEW_<version>.md: the single place a human
checks after run_review.py finishes. Covers what was auto-applied, the
full review-tier queue grouped by why it's gated, low-confidence bugs (with
the rubric's >25% documentation-gap threshold called out if breached), and
a copy-pasteable next command for the review tier.

USAGE
    python3 needs_review.py --version 1.2.6.0 [--apply-result apply_result.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from pull_bugs import browse_base_url, load_dotenv  # type: ignore

load_dotenv(SCRIPT_DIR)
BASE_BROWSE_URL = browse_base_url()


def browse_base_from_pull(version: str) -> str:
    """Fall back to the site URL recorded in the pulled bugs JSON."""
    if BASE_BROWSE_URL:
        return BASE_BROWSE_URL
    path = SCRIPT_DIR / f"bugs_{version}.json"
    if not path.exists():
        return ""
    base = (json.loads(path.read_text()).get("meta") or {}).get("base_url") or ""
    return f"{base.rstrip('/')}/browse/" if base else ""


def render(
    version: str,
    analysis: dict[str, Any],
    auto_plan: dict[str, Any],
    review_plan: dict[str, Any],
    apply_summary: dict[str, Any] | None,
) -> str:
    bugs_by_key = {b["key"]: b for b in analysis["bugs"]}
    total = len(analysis["bugs"])
    low_conf = [b for b in analysis["bugs"] if b["confidence"] == "low"]
    low_pct = round(100 * len(low_conf) / total) if total else 0

    L: list[str] = []

    def w(s: str = "") -> None:
        L.append(s)

    w(f"# Needs review — {version}")
    w()
    w(f"Run summary: {total} bugs classified · {len(auto_plan['actions'])} auto-tier actions · "
      f"{len(review_plan['actions'])} review-tier actions.")
    w()

    if apply_summary is not None:
        w("## Auto-tier apply result")
        w()
        for k, v in apply_summary.items():
            w(f"- **{k}**: {v}")
        if apply_summary.get("failed", 0):
            w()
            w("**Auto-apply had failures — check the run log before trusting any auto-tier write.**")
        w()

    w("## Auto-applied (high-confidence RCA and/or existing-link feature matches)")
    w()
    if auto_plan["actions"]:
        w("| Bug | RCA set | Feature linked |")
        w("|---|---|---|")
        for a in sorted(auto_plan["actions"], key=lambda x: x["bug_key"]):
            w(f"| [{a['bug_key']}]({BASE_BROWSE_URL}{a['bug_key']}) | "
              f"{a.get('set_pm_rca_type') or '—'} | {a.get('link_to_feature') or '—'} |")
    else:
        w("*(none this run)*")
    w()

    w("## Needs your review before write-back")
    w()
    if review_plan["actions"]:
        w("| Bug | RCA (proposed) | Feature (proposed) | Why it's gated |")
        w("|---|---|---|---|")
        for a in sorted(review_plan["actions"], key=lambda x: x["bug_key"]):
            w(f"| [{a['bug_key']}]({BASE_BROWSE_URL}{a['bug_key']}) | "
              f"{a.get('set_pm_rca_type') or '—'} | {a.get('link_to_feature') or '—'} | "
              f"{a.get('skip_reason') or ''} |")
    else:
        w("*(nothing pending — everything was either auto-applied or had no proposal)*")
    w()

    w("## Low-confidence bugs")
    w()
    w(f"{len(low_conf)}/{total} bugs ({low_pct}%) — ")
    if low_pct > 25:
        w("**exceeds the rubric's 25% threshold: the JIRA tickets aren't carrying enough "
          "RCA evidence, treat this as a process finding, not just individual bugs to review.**")
    else:
        w("within the rubric's 25% threshold.")
    w()
    for b in low_conf:
        w(f"- [{b['key']}]({BASE_BROWSE_URL}{b['key']}) — {b['summary']}")
    w()

    w("## Next step")
    w()
    w("Review the table above, correct anything wrong directly in "
      f"`plan_{version}.review.json`, then apply it through the normal ladder:")
    w()
    w("```")
    w(f"python3 write_back.py --plan plan_{version}.review.json --allow-review-tier --dry-run")
    w(f"python3 write_back.py --plan plan_{version}.review.json --allow-review-tier --limit 5")
    w(f"python3 write_back.py --plan plan_{version}.review.json --allow-review-tier")
    w("```")
    w()

    return "\n".join(L) + "\n"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Write NEEDS_REVIEW_<version>.md.")
    p.add_argument("--version", required=True)
    p.add_argument("--out-dir", default=None)
    p.add_argument("--apply-result", default=None, help="Optional JSON file with the auto-apply summary counts.")
    return p.parse_args()


def main() -> int:
    global BASE_BROWSE_URL
    args = parse_args()
    out_dir = Path(args.out_dir) if args.out_dir else SCRIPT_DIR
    BASE_BROWSE_URL = browse_base_from_pull(args.version)

    analysis = json.loads((out_dir / f"analysis_{args.version}.json").read_text())
    auto_plan = json.loads((out_dir / f"plan_{args.version}.auto.json").read_text())
    review_plan = json.loads((out_dir / f"plan_{args.version}.review.json").read_text())
    apply_summary = None
    if args.apply_result and Path(args.apply_result).exists():
        apply_summary = json.loads(Path(args.apply_result).read_text())

    md = render(args.version, analysis, auto_plan, review_plan, apply_summary)
    out_path = out_dir / f"NEEDS_REVIEW_{args.version}.md"
    out_path.write_text(md)
    print(f"  wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
split_plan.py — split a full plan_<version>.json into an auto-apply tier and
a human-review tier, per the auto/review gating rules in SKILL.md §5b:

  - set_pm_rca_type goes in the AUTO plan iff the bug's classification
    confidence is "high" (the rubric's own definition: explicit RCA in
    comments, a dispositive label, or PM Analysis already filled in).
  - link_to_feature goes in the AUTO plan iff feature_match_basis is
    "linked_issues" — a fact already present in the bug's own JIRA data,
    never an LLM's PRD-reading inference. PRD-content matches
    (feature_match_basis == "prd_content") always stay in REVIEW regardless
    of confidence, because SKILL.md is explicit that PRD-reading is a PM
    judgment call ("the PM's mapping wins").
  - set_pm_analysis is NEVER emitted in either plan — PM Analysis free text
    is drafted for review but not auto-written (SKILL.md §5b).
  - The two fields are gated independently: a bug can get its RCA auto-set
    while its feature link waits for review, or vice versa. In that case
    the bug's action is split across both files, each carrying only the
    field it's responsible for.
  - Actions with a skip_reason (nothing to apply) go to REVIEW for audit
    visibility but never AUTO.

This is a pure function (testable without Jira access) plus a thin CLI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent


def split_plan(
    analysis: dict[str, Any], plan: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    bugs_by_key = {b["key"]: b for b in analysis.get("bugs", [])}

    auto_actions: list[dict[str, Any]] = []
    review_actions: list[dict[str, Any]] = []

    for action in plan.get("actions", []):
        bug_key = action.get("bug_key")
        bug = bugs_by_key.get(bug_key, {})

        if action.get("skip_reason"):
            review_actions.append(dict(action))
            continue

        rca_value = action.get("set_pm_rca_type")
        feature_key = action.get("link_to_feature")
        confidence = bug.get("confidence")
        feature_match_basis = bug.get("feature_match_basis")

        rca_auto = bool(rca_value) and confidence == "high"
        link_auto = bool(feature_key) and feature_match_basis == "linked_issues"

        base = {
            "bug_key": bug_key,
            "confidence": confidence,
            "feature_match_basis": feature_match_basis,
        }

        if rca_auto or link_auto:
            auto_actions.append({
                **base,
                "set_pm_rca_type": rca_value if rca_auto else None,
                "link_to_feature": feature_key if link_auto else None,
                # set_pm_analysis is deliberately never populated here.
                "skip_reason": None,
                "tier": "auto",
            })

        rca_leftover = rca_value if (rca_value and not rca_auto) else None
        link_leftover = feature_key if (feature_key and not link_auto) else None
        if rca_leftover or link_leftover:
            reasons = []
            if rca_leftover:
                reasons.append(f"RCA confidence={confidence!r} — human review required")
            if link_leftover:
                reasons.append(
                    f"feature match basis={feature_match_basis!r} — PRD judgment call, "
                    "PM must confirm"
                )
            review_actions.append({
                **base,
                "set_pm_rca_type": rca_leftover,
                "link_to_feature": link_leftover,
                "skip_reason": "; ".join(reasons),
                "tier": "review",
            })
        elif not (rca_auto or link_auto) and not (rca_value or feature_key):
            # Nothing to do at all (e.g. no RCA, no feature) — keep for visibility.
            review_actions.append({
                **base,
                "set_pm_rca_type": None,
                "link_to_feature": None,
                "skip_reason": action.get("skip_reason") or "no RCA value or feature link proposed",
                "tier": "review",
            })

    meta = dict(plan.get("meta", {}))
    link_type = plan.get("link_type")

    auto_plan = {
        "meta": {**meta, "tier": "auto"},
        **({"link_type": link_type} if link_type else {}),
        "actions": auto_actions,
    }
    review_plan = {
        "meta": {**meta, "tier": "review"},
        **({"link_type": link_type} if link_type else {}),
        "actions": review_actions,
    }
    return auto_plan, review_plan


def parse_args() -> Any:
    import argparse

    p = argparse.ArgumentParser(description="Split a plan into auto/review tiers.")
    p.add_argument("--version", required=True)
    p.add_argument("--analysis", default=None, help="Path to analysis_<version>.json.")
    p.add_argument("--plan", default=None, help="Path to plan_<version>.json.")
    p.add_argument("--out-dir", default=None)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir) if args.out_dir else SCRIPT_DIR
    analysis_path = Path(args.analysis) if args.analysis else out_dir / f"analysis_{args.version}.json"
    plan_path = Path(args.plan) if args.plan else out_dir / f"plan_{args.version}.json"

    analysis = json.loads(analysis_path.read_text())
    plan = json.loads(plan_path.read_text())

    auto_plan, review_plan = split_plan(analysis, plan)

    auto_path = out_dir / f"plan_{args.version}.auto.json"
    review_path = out_dir / f"plan_{args.version}.review.json"
    auto_path.write_text(json.dumps(auto_plan, indent=2) + "\n")
    review_path.write_text(json.dumps(review_plan, indent=2) + "\n")

    print(f"  auto tier:   {len(auto_plan['actions'])} actions -> {auto_path}")
    print(f"  review tier: {len(review_plan['actions'])} actions -> {review_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

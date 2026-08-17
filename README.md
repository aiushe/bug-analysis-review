# Release Bug Review — agentic RCA automation

This skill automates the post-release bug review: classifies each bug with the **PM RCA Type** field, links each bug to its feature in your feature project, and splits actions into high-confidence (auto-apply) and judgment-call (review) tiers.

**The agentic automation insight:** Most of this work is mechanical (pulling bugs, classifying against a rubric, building docs, splitting by confidence), but the PM's mapping judgment must stay human. The pipeline is "agentic" because the orchestrator (`run_review.py`) chains deterministic tools together and **stops at decision points** where human judgment matters:

```
        ┌─────────────────────────────────────────────────────────────┐
        │  run_review.py — THE ORCHESTRATOR                           │
        └─────────────────────────────────────────────────────────────┘
                                    │
        ┌───────────────────────────┴───────────────────────────────┐
        │  MECHANICAL TOOLS (deterministic, automated)               │
        ├───────────────────────────────────────────────────────────┤
        │                                                             │
        │  1. pull_bugs.py + pull_features.py                        │
        │     └→ JIRA data (JSON)                                    │
        │                                                             │
        │  2. classify_headless.py (claude -p)                       │
        │     └→ read rubric + bugs + PRD                            │
        │     └→ assign RCA Type + confidence level                  │
        │     └→ analysis_<version>.json                             │
        │                                                             │
        │  3. build_deliverables.py                                  │
        │     └→ doc + xlsx + full plan_<version>.json              │
        │                                                             │
        │  4. split_plan.py                                          │
        │     └→ auto-tier: high-confidence + linked_issues only     │
        │     └→ review-tier: everything else                        │
        │                                                             │
        │  5. write_back.py (dry-run)                                │
        │     └→ preview: "here's what would change"                 │
        │                                                             │
        ├───────────────────────────────────────────────────────────┤
        │  SAFETY GATE 1: Dry-run completes successfully?            │
        ├───────────────────────────────────────────────────────────┤
        │                                                             │
        │  6. write_back.py (canary + full apply)                    │
        │     └→ auto-tier ONLY — no human judgment needed           │
        │     └→ high-confidence RCA values are safe to write        │
        │     └→ linked_issues feature links are facts, not guesses  │
        │                                                             │
        │  7. write_back.py (verify)                                 │
        │     └→ re-fetch from Jira, confirm changes landed          │
        │                                                             │
        │  8. needs_review.py                                        │
        │     └→ NEEDS_REVIEW_<version>.md                          │
        │                                                             │
        └───────────────────────────────────────────────────────────┘
                                    │
        ┌───────────────────────────┴───────────────────────────────┐
        │  HUMAN JUDGMENT GATE                                       │
        ├───────────────────────────────────────────────────────────┤
        │                                                             │
        │  You read NEEDS_REVIEW and decide:                         │
        │  • Is each medium-confidence RCA call correct?             │
        │  • Are the PRD-content feature links right?                │
        │  • Should any review-tier bugs get different handling?     │
        │                                                             │
        │  Then you apply the review tier yourself:                  │
        │  python3 write_back.py --plan plan_*.review.json \         │
        │                         --allow-review-tier ...            │
        │                                                             │
        │  (--allow-review-tier flag REQUIRED; code enforces this)   │
        │                                                             │
        └───────────────────────────────────────────────────────────┘
```

**Why this is "agentic"**: The orchestrator chains independent tools intelligently, collecting facts along the way (bugs, features, rubric match results), and uses those facts to make deterministic decisions (confidence-based tier split, feature matching based on existing links). It never skips a decision—it just pushes judgment calls to the human at the right moment. Each tool is a "capability" the agent uses; the orchestrator is the "reasoning loop" that decides which tools to run and when to pause.

Built for **Jira Server / Data Center**. Self-hosted sites are often only reachable from inside the corporate network, so the data pulls and write-backs run from a machine that can reach yours, and the LLM-based classification happens headless via `claude -p`. Nothing about the site, project, or field names is hard-coded — it all comes from `scripts/.env`.

## One-time setup

### 1. Generate a Personal Access Token in Jira

1. Log in to your Jira site.
2. Avatar (top-right) → **Profile** → **Personal Access Tokens** → **Create token**.
3. Name it anything (`bug review`). Pick the longest expiry your policy allows.
4. Copy the token — you won't see it again.

### 2. Configure the site and token

In the `scripts/` folder, create a `.env` file (copy `.env.example` and fill it in):

```
JIRA_BASE_URL=https://jira.example.com
JIRA_PAT=paste-your-token-here
JIRA_FEATURE_PROJECT=PROJ
```

Everything else is optional — see `.env.example` for the full list. Any of these can be exported in your shell instead; the scripts pick up either.

| Variable | Required | Default |
|---|---|---|
| `JIRA_BASE_URL` | yes | — |
| `JIRA_PAT` | yes | — |
| `JIRA_FEATURE_PROJECT` | yes (for feature pulls) | — |
| `JIRA_FEATURE_ISSUETYPES` | no | `New Feature Request` |
| `JIRA_COMPONENTS` | no | unset = all components on the release |
| `JIRA_LINK_TYPE` | no | `Requirement Link` |
| `JIRA_RCA_FIELD_NAME` | no | `PM RCA Type` |
| `JIRA_ANALYSIS_FIELD_NAME` | no | `PM Analysis` |
| `JIRA_RCA_FIELD_ID` | no | `customfield_19330` |

### 3. Install the skill

Drop the `bug-analysis-review/` folder into your skills directory. If you rename the folder, update `name:` in `SKILL.md` to match.

## Running a review for a release

### The one-command way (the normal flow)

Everything runs from a single command on a machine that can reach your Jira site:

```
cd bug-analysis-review/scripts
python3 run_review.py --version 1.2.6.0 --prev-versions 1.2.5.0,1.2.4.0
```

This is the **agentic orchestration**. The pipeline chains:

1. **Pull** bugs + features (current + prior releases) from Jira
2. **Classify** (headless `claude -p`) — reads the rubric, reads the bugs and PRD feature pool, assigns PM RCA Type + confidence level to each bug (no hand-typed dicts anymore)
3. **Build** deliverables — doc + xlsx + full action plan from the analysis JSON
4. **Split** into auto and review tiers based on confidence and feature-match type:
   - **Auto tier**: high-confidence RCA calls + feature links that were already in JIRA (deterministic facts)
   - **Review tier**: medium/low-confidence calls, PRD-content feature matches, anything needing your judgment
5. **Dry-run** the auto tier (show what would happen, no actual changes)
6. **Apply** the auto tier: canary (5 bugs) → full → verify
7. **Write** `NEEDS_REVIEW_<version>.md` — shows what was auto-applied, what's waiting for you, and next steps

**Human judgment preserved at every decision point:**
- `NEEDS_REVIEW_<version>.md` lists all review-tier bugs; edit `plan_<version>.review.json` directly if you disagree
- Run the same apply ladder yourself when ready:

```
python3 write_back.py --plan plan_1.2.6.0.review.json --allow-review-tier --dry-run
python3 write_back.py --plan plan_1.2.6.0.review.json --allow-review-tier --limit 5
python3 write_back.py --plan plan_1.2.6.0.review.json --allow-review-tier
```

**Useful flags:**
- `--dry-run-only` — run through the whole pipeline read-only, never apply anything (good for smoke tests)
- `--reuse-pulls` — skip re-pulling if bugs/features JSON already exist
- `--bug-limit N` — cap classification to N bugs for a fast test run
- `--components` — passed through to pull_bugs.py

**Audit trail:** Every run appends one line to `scripts/runs.jsonl` — the durable local record of what was proposed, auto-applied, and left pending. Per-run logs in `scripts/logs/`. Both stay on your machine; neither is committed.

### Workflow after the orchestrator finishes

After `run_review.py` completes, you'll have:
- `analysis_<version>.json` — all bugs classified with confidence levels
- `plan_<version>.auto.json` — already applied (high-confidence RCA only)
- `plan_<version>.review.json` — waiting for you (medium/low confidence, PRD matches)
- `NEEDS_REVIEW_<version>.md` — summary of what got auto-applied and what needs your review

**To apply the review-tier plan:**

1. Read `NEEDS_REVIEW_<version>.md` — it lists everything that's waiting for you and why
2. (Optional) Edit `plan_<version>.review.json` directly if you disagree with a proposed RCA or want to change a feature link
3. Run the apply ladder with `--allow-review-tier`:

```
python3 write_back.py --plan plan_1.2.6.0.review.json --allow-review-tier --dry-run
python3 write_back.py --plan plan_1.2.6.0.review.json --allow-review-tier --limit 5
python3 write_back.py --plan plan_1.2.6.0.review.json --allow-review-tier
```

The `--allow-review-tier` flag is required; without it, `write_back.py` refuses to apply any review-tier plan (this is a code-level safety gate to enforce the PM review step).

### Emergency: manual pulls only (if the full pipeline can't run)

If you need to pull bugs and features without running the full pipeline:

```
cd bug-analysis-review/scripts
python3 pull_bugs.py --version 1.2.2.0
python3 pull_features.py --version 1.2.2.0
python3 pull_features.py --version 1.2.1.0    # also pull prior release
```

This produces `bugs_*.json` and `features_*.json` files that can be checked in or shared. Then the full pipeline can run from those cached files with `--reuse-pulls`.

## Files in this skill

```
bug-analysis-review/
├── SKILL.md                                # workflow, RCA rubric definitions, feature-matching rules
├── README.md                               # this file
├── .claude/
│   ├── settings.json                       # scoped Read/Write allowlist (main session)
│   └── headless-settings.json              # scoped allowlist for headless classify subprocess
├── reference/
│   └── classification_rubric.md            # the 5 PM RCA Type values + confidence definitions
└── scripts/
    ├── .env.example                        # every supported setting, no secrets
    ├── .gitignore                          # keeps generated output + .env out of git
    │
    ├── THE ORCHESTRATOR:
    ├── run_review.py                       # ONE COMMAND: chains all 9 steps in the agentic pipeline
    │
    ├── PIPELINE STEPS:
    ├── pull_bugs.py                        # step 1: release bugs → JSON
    ├── pull_features.py                    # step 1: candidate features → JSON (current + prior)
    ├── classify_headless.py                # step 2: headless `claude -p` → analysis_*.json
    ├── build_deliverables.py               # step 3: analysis JSON → doc + xlsx + plan
    ├── split_plan.py                       # step 4: plan → auto-tier / review-tier split
    ├── needs_review.py                     # step 9: write NEEDS_REVIEW_<version>.md
    ├── write_back.py                       # step 5/6/7: dry-run → canary → full apply → verify
    ├── validate_schema.py                  # schema checks for analysis/plan JSON
    ├── lib_run.py                          # run logger + runs.jsonl ledger helpers
    │
    ├── HELPERS (rarely called directly):
    ├── make_report_docx.py                 # markdown report → styled Word doc (python-docx)
    ├── add_label.py                        # bulk-label issues matching a JQL
    └── create_subtasks.py                  # create sub-tasks under a parent issue from a JSON spec
```

## Generated files (not committed)

**This repo tracks source only.** Everything a pipeline run produces — pulled Jira data, analysis, plans, deliverables, logs, the run ledger — is ignored and must never be committed. Two layers enforce it: the repo-root `.gitignore` ignores whole classes of file by extension (`*.json`, `*.jsonl`, `*.xlsx`, `*.docx`, `*.csv`, `*.log`, `.env*`), so an artifact with an unforeseen name is ignored by default; `scripts/.gitignore` adds by-name patterns on top. Ignored artifacts:

**Data pulls:**
- `.env` — your Jira site URL + PAT (**secret — never commit**)
- `bugs_*.json`, `features_*.json` — pulled JIRA data (safe to re-pull)

**Analysis & plan outputs:**
- `analysis_*.json` — classified bugs with confidence levels and edge cases (one per run)
- `plan_*.json` — full action plan (all bugs)
- `plan_*.auto.json` — auto-tier plan (high-confidence only, safe to auto-apply)
- `plan_*.review.json` — review-tier plan (needs your judgment)
- `plan_*.canary_summary.json`, `plan_*.apply_summary.json` — write-back results

**Human-facing outputs:**
- `NEEDS_REVIEW_*.md` — summary of what was auto-applied vs. what's waiting for you
- `Bug_Review_*.md` — analysis doc (markdown)
- `Bug_Review_*.xlsx` — rollup sheet with bug → RCA → feature mappings
- `*.docx` — if exported from markdown

**Logs & intermediate:**
- `logs/run_*.log` — per-run execution log (verbose, includes all output)
- `_*.txt` — intermediate digest text dumps
- `__pycache__/` — Python bytecode cache

**Local audit trail:**
- `runs.jsonl` — one line per pipeline run (what was proposed, auto-applied, left pending) across all releases

These are all safe to delete between runs; keep `runs.jsonl` if you want the run history. If you ever need to add a file one of the ignore rules catches, un-ignore it explicitly in `.gitignore` rather than `git add -f` — a forced add is invisible to the next person.

## Troubleshooting

**`Jira site URL not set` / `JIRA_PAT not set`** — the scripts read both from the environment or `scripts/.env`. Copy `.env.example` to `.env` and fill it in.

**`HTTP 401` / `HTTP 403` on pull or write-back** — PAT expired, was revoked, or lacks Browse Projects on the project. Regenerate the token and check your project permissions.

**`could not find a JIRA field named 'PM RCA Type'`** — the field exists but under a different display name. Open one of the bugs in Jira, read the field label exactly, and set `JIRA_RCA_FIELD_NAME` to it.

**`link type 'X' not found`** — your site doesn't have that link type (many have no "Relates"). The script lists the available types — pick one and set `JIRA_LINK_TYPE`, or edit `link_type` in the plan JSON, then retry.

**`invalid RCA value`** — the value didn't match the dropdown exactly. The mismatch category uses an **en-dash**: `Requirement–expectation mismatch` (not a hyphen). The field has no "None" option — bugs with null RCA are never written, only skipped.

**`run_review.py` exits with "Jira is not reachable"** — by design. A self-hosted site usually can't be reached from a cloud sandbox, so run the pipeline on a laptop or on-premises machine (VPN on) that can.

**`write_back.py` exits with "is tier='review' ... re-run with --allow-review-tier"** — this is expected behavior. The orchestrator only auto-applies the high-confidence tier; the review tier (medium/low-confidence, PRD-content matches, anything needing judgment) requires you to read `NEEDS_REVIEW_<version>.md` and explicitly pass `--allow-review-tier` to confirm you've reviewed it.

**`claude` CLI not found** (from `run_review.py` preflight) — the headless classification step shells out to `claude -p` to run the LLM classification. Make sure Claude Code is installed on PATH on the machine running the pipeline.

**Classification ran but `analysis_*.json` wasn't written** — the `claude -p` subprocess may have been denied a Read or Write tool call. Check `.claude/headless-settings.json` permissions allowlist; it needs: `Read(scripts/bugs_*.json)`, `Read(scripts/features_*.json)`, `Read(reference/classification_rubric.md)`, `Read(SKILL.md)`, and `Write(scripts/analysis_*.json)`.

**Low-confidence bugs exceed 25% threshold** — the rubric defines 25% as the acceptable low-confidence ratio. When a release exceeds this, `NEEDS_REVIEW_*.md` flags it as a process finding, not just individual bugs. Check the NEEDS_REVIEW doc; if many bugs lack clear RCA evidence in JIRA comments/labels, this may indicate your process needs tightening (e.g., require RCA comments on all bugs).

<!-- dgc-policy-v11 -->
# Dual-Graph Context Policy

This project uses a local dual-graph MCP server for efficient context retrieval.

## MANDATORY: Adaptive graph_continue rule

**Call ``graph_continue`` ONLY when you do NOT already know the relevant files.**

### Call ``graph_continue`` when:
- This is the first message of a new task / conversation
- The task shifts to a completely different area of the codebase
- You need files you haven't read yet in this session

### SKIP ``graph_continue`` when:
- You already identified the relevant files earlier in this conversation
- You are doing follow-up work on files already read (verify, refactor, test, docs, cleanup, commit)
- The task is pure text (writing a commit message, summarising, explaining)

**If skipping, go directly to ``graph_read`` on the already-known ``file::symbol``.**

## When you DO call graph_continue

1. **If ``graph_continue`` returns ``needs_project=true``**: call ``graph_scan`` with ``pwd``. Do NOT ask the user.

2. **If ``graph_continue`` returns ``skip=true``**: fewer than 5 files  -  read only specifically named files.

3. **Read ``recommended_files``** using ``graph_read``.
   - Always use ``file::symbol`` notation (e.g. ``src/auth.ts::handleLogin``)  -  never read whole files.
   - ``recommended_files`` entries that already contain ``::`` must be passed verbatim.

4. **Obey confidence caps:**
   - ``confidence=high`` -> Stop. Do NOT grep or explore further.
   - ``confidence=medium`` -> ``fallback_rg`` at most ``max_supplementary_greps`` times, then ``graph_read`` at most ``max_supplementary_files`` more symbols. Stop.
   - ``confidence=low`` -> same as medium. Stop.

## Session State (compact, update after every turn)

Maintain a short JSON block in your working memory. Update it after each turn:

``````json
{
  "files_identified": ["path/to/file.py"],
  "symbols_changed": ["module::function"],
  "fix_applied": true,
  "features_added": ["description"],
  "open_issues": ["one-line note"]
}
``````

Use this state  -  not prose summaries  -  to remember what's been done across turns.

## Token Usage

A ``token-counter`` MCP is available for tracking live token usage.

- Before reading a large file: ``count_tokens({text: "<content>"})`` to check cost first.
- To show running session cost: ``get_session_stats()``
- To log completed task: ``log_usage({input_tokens: N, output_tokens: N, description: "task"})``

## Rules

- Do NOT use ``rg``, ``grep``, or bash file exploration before calling ``graph_continue`` (when required).
- Do NOT do broad/recursive exploration at any confidence level.
- ``max_supplementary_greps`` and ``max_supplementary_files`` are hard caps  -  never exceed them.
- Do NOT call ``graph_continue`` more than once per turn.
- Always use ``file::symbol`` notation with ``graph_read``  -  never bare filenames.
- After edits, call ``graph_register_edit`` with changed files using ``file::symbol`` notation.

## Context Store

Whenever you make a decision, identify a task, note a next step, fact, or blocker during a conversation, append it to ``.dual-graph/context-store.json``.

**Entry format:**
``````json
{"type": "decision|task|next|fact|blocker", "content": "one sentence max 15 words", "tags": ["topic"], "files": ["relevant/file.ts"], "date": "YYYY-MM-DD"}
``````

**To append:** Read the file -> add the new entry to the array -> Write it back -> call ``graph_register_edit`` on ``.dual-graph/context-store.json``.

**Rules:**
- Only log things worth remembering across sessions (not every minor detail)
- ``content`` must be under 15 words
- ``files`` lists the files this decision/task relates to (can be empty)
- Log immediately when the item arises  -  not at session end

## Session End

When the user signals they are done (e.g. "bye", "done", "wrap up", "end session"), proactively update ``CONTEXT.md`` in the project root with:
- **Current Task**: one sentence on what was being worked on
- **Key Decisions**: bullet list, max 3 items
- **Next Steps**: bullet list, max 3 items

Keep ``CONTEXT.md`` under 20 lines total. Do NOT summarize the full conversation  -  only what's needed to resume next session.


---

# Project: Hiring Intelligence

Everything below is project guidance. The dual-graph policy above is tooling config — leave it alone.

## What this is

An internal tool for a small recruiting team. Role requirements in, ranked evidence-backed
shortlist out. Engineering/data/ML roles first. India-primary. A handful of users, ~30 role
searches a month.

**Read before working:**

- `docs/PRD.md` — what we're building, for whom, and how success is measured. Start here.
- `docs/ARCHITECTURE.md` — the design. Authoritative. Supersedes `Project-Doc.md`.
- `docs/IMPLEMENTATION.md` — dependency-ordered build plan with definition-of-done per step.
- `AGENTS.md` — where LLMs may and may not act. Read before touching any LLM call-site.
- `Project-Doc.md` — the original AI-generated proposal. **Historical only.** Where it disagrees
  with `docs/ARCHITECTURE.md`, the architecture doc wins. Do not implement from it.

## The five things that matter most

1. **Evidence is the primary entity.** Every claim about a person is an `evidence` row with a
   `source_url`, a verbatim `snippet`, and a `tier`. Candidate attributes are derived from
   evidence, not stored alongside it. If you cannot quote the source, there is no claim.
2. **Three tiers never merge.** `artifact_backed` (code, packages, ratings) >
   `third_party_stated` > `self_reported`. "Lists Python on a profile" and "has 40 Python repos"
   are different facts and must stay visibly different all the way to the UI.
3. **Scoring is deterministic.** A weighted sum of five allowlisted components. Reproducible,
   byte-identical across runs. An LLM writes the rationale prose; it never writes the number.
4. **No source is fetched without an enabled `source_policy` row.** The fetch layer is the single
   chokepoint for robots.txt, rate limits, and caps. Nothing bypasses it.
5. **Quality over quantity.** 20 candidates a recruiter wants to contact beats 5,000 rows.
   Coverage is deliberately narrower than LinkedIn.

## Stack

Python 3.13 · FastAPI · Postgres 16 · httpx · selectolax · Playwright (lazy fallback only) ·
Jinja2 + HTMX · `anthropic` SDK · pydantic v2 · pytest. One Docker Compose, one VM. The job queue
is a Postgres table with `FOR UPDATE SKIP LOCKED` and one worker process.

## Development workflow

```bash
uv sync                          # deps
docker compose up -d             # Postgres only
python -m hi.db migrate          # apply migrations/*.sql in order
uvicorn hi.web:app --reload      # web + job queue (the queue runs in-process)
python -m hi.worker              # optional: a second drainer, safe to run alongside
pytest                           # no live network; adapters run against fixtures
```

Conventions:

- Migrations are append-only numbered SQL files. Never edit an applied migration.
- Every adapter ships with recorded fixtures in `tests/fixtures/<adapter>/`. A test that needs the
  internet is a broken test.
- Prompts live in `src/hi/prompts/` and are versioned. Changing a prompt requires bumping its
  version, because `(fetch_id, extractor_version)` is unique and a silent change mixes outputs
  from two extractors in one table.
- `scoring.py` is pure — no I/O, no clock, no DB. Keep it that way; it is why the tests are cheap.

## Hard rules — do not violate without an explicit decision recorded in ARCHITECTURE.md §2

- Do not add Redis, Celery, Kafka, Elasticsearch, a vector DB, or an SPA. All were considered and
  rejected for this size of team.
- Do not use an LLM to compute a score, merge two candidates, or decide whether to fetch a URL.
- Do not add a residential proxy pool, a captcha solver, or fingerprint spoofing. A target that
  needs those is a target to drop. **Exception, scoped and recorded:** LinkedIn mode E
  (`ARCHITECTURE.md` §7.5), authorised as a contingency by §2.1. It is **not built**, it is
  trigger-gated, and it grants nothing to any other adapter. Captcha *solving* stays banned there
  too. Do not implement it without a dated go decision in §7.5.
- Do not build a continuous crawler. Searches are role-triggered and cached.
- Do not extract, infer, or store gender, caste, religion, age, marital status, or photographs.
  Not "collect but don't rank" — do not collect.
- Do not add a scoring feature without adding it to `FEATURE_ALLOWLIST` in the same commit.
- Do not store plaintext candidate emails — `sha256` only. We identify people; recruiters contact
  them through a channel the person opened.
- Do not enable a `source_policy` row without filling `reviewed_at`.
- LinkedIn mode C (`attached_browser`): never headless, never a burner or fabricated or shared
  account, never stored credentials, concurrency 1, daily cap per account, and it disables itself
  on the first challenge. These limits live in code, not config. A separate *browser* for automation
  is fine and encouraged; a separate *identity* is not (`ARCHITECTURE.md` §7.4).
- Do not port `linkedin_scraper/core/auth.py`. It reads `LINKEDIN_EMAIL`/`LINKEDIN_PASSWORD` from
  `.env`; stored credentials are banned. Mode B needs no auth, mode C is a CDP attach.

## Finalised 2026-08-25 — read before touching sourcing or scoring

- **LinkedIn is the spine; artefacts verify.** Discovery is `linkedin_serp` (mode A), enrichment is
  `linkedin_profile` mode B (logged out), verification is `github` and friends. Recorded as a
  reversal in `ARCHITECTURE.md` §2.1 — do not re-litigate it in code.
- **The delivered product uses no LinkedIn account.** Modes A and B only. Mode C
  (`attached_browser`) is implemented-but-disabled and belongs to whoever operates the tool after
  hand-off, not to the author. Mode D and mode E are not built.
- **Measure before building the enrichment adapter.** `IMPLEMENTATION.md` §1.4a — the share of
  logged-out profiles carrying dated `experiences[]`. The architecture forks on that number.
- **The snippet gate runs before any fetch** and its verdict plus reason goes in `candidate_ref`. A
  `failed` ref with a null `gate_reason` is refused by a database constraint.
- **Nothing may treat missing enrichment as fatal.** `ARCHITECTURE.md` §9a.2 — the degraded path is
  the survival strategy, and `IMPLEMENTATION.md` §1.11b is the test that keeps it real.
- **A green health light means "produced rows recently", never "did not throw."** The characteristic
  LinkedIn failure is silent success.
- **Operability is M1, not M4.** Health page, degraded mode, runbook, backups, one selectors file.
  `ARCHITECTURE.md` §9a.5 is the hand-off checklist and it gates done.
- **No LLM call on candidate data in M1.** The scraper returns structured pydantic; parsing it is a
  parse, not an inference. JD parsing is the only call-site.

## Definition of done for any change

1. It works against fixtures, and `pytest` is green.
2. Any new claim it produces carries a real `source_url` and a verbatim `snippet`.
3. Any new scoring input is in `FEATURE_ALLOWLIST`.
4. A recruiter can still answer "why is this candidate ranked here" from the candidate card.
5. The relevant `docs/` section is updated in the same change.

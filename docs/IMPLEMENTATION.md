# Implementation Plan

Derived from `docs/ARCHITECTURE.md`. Read that first, and `AGENTS.md` before touching an LLM call-site.

**Ordering principle:** Milestone 1 is a thin vertical slice that produces a real shortlist from a single source. Everything after it broadens. Do not start Milestone 2 until a recruiter has used Milestone 1 on a real open role.

**Layout** (create as you go, not up front):

```
src/hi/
  config.py        settings (pydantic-settings, env-driven)
  db.py            connection pool, migration runner
  models.py        pydantic domain types (RoleSpec, Evidence, CandidateRef, Query, RawDoc)
  fetcher.py       source_policy gate, robots, token bucket, cache
  llm.py           the only place an LLM is called
  prompts/         versioned prompt templates
  adapters/        one module per source, all implementing Adapter
  extract/         deterministic parsers + LLM extraction wrapper
  identity.py      strong-key resolution
  scoring.py       gates + components (pure functions)
  worker.py        job loop
  web/             FastAPI app, Jinja templates
migrations/        NNN_name.sql, applied in order
data/              skills.yaml, regions.yaml
tests/
  fixtures/        recorded API responses and saved HTML
```

---

# Milestone 1 — spine end to end

Goal: paste a JD, get a ranked shortlist of engineers **discovered from LinkedIn and verified against
public artefacts**, every claim clickable and tier-badged.

**Revised 2026-08-25** for the source reversal in `ARCHITECTURE.md` §2.1. LinkedIn discovery and
enrichment (modes A and B) are M1, not M3. GitHub stays in M1 but changes job: it verifies claims
rather than discovering people. Steps 1.4a–1.4c are new and insert *before* the GitHub step; nothing
downstream was renumbered, so every `Deps:` reference below still points where it did.

## 1.1 Skeleton and config

**Why:** everything else needs settings and a DB handle.

**Deps:** none.

**Files:** `pyproject.toml`, `src/hi/config.py`, `src/hi/db.py`, `docker-compose.yml`, `migrations/`.

**Build:** deps (`fastapi`, `uvicorn`, `psycopg[binary,pool]`, `httpx`, `selectolax`, `pydantic`, `pydantic-settings`, `jinja2`, `anthropic`, `pyyaml`, `pytest`). Compose runs Postgres 16 only. `db.py` exposes a pool and a migration runner that applies `migrations/*.sql` in filename order, tracked in a `schema_migrations` table.

**Edge cases:** migrations must be idempotent to re-run; a partially applied migration must fail loudly, not half-apply — one transaction per file.

**Tests:** `test_migrations.py` — run all migrations against a scratch DB twice; second run is a no-op.

**DoD:** `docker compose up` then `python -m hi.db migrate` produces the full schema on an empty database.

## 1.2 Schema

**Why:** the shape of the data is the design; get it in before code depends on guesses.

**Deps:** 1.1.

**Files:** `migrations/001_init.sql`.

**Build:** every table in ARCHITECTURE §4. Constraints that matter and must be in the DDL, not just in code:

- `identity`: `unique (kind, value)` — the whole of identity resolution rests on this.
- `evidence`: `check (snippet <> '')`, `check (tier in (...))`, `check (claim_type in (...))`.
- `match`: `unique (role_id, candidate_id, scorer_version, weights_version)`.
- `evidence.candidate_id` and `identity.candidate_id`: `on delete cascade` — this is the erasure mechanism.
- `source_policy`: seed with **zero enabled rows**. A source is switched on deliberately.
- `candidate_ref`: `unique (role_id, ref_kind, ref_value)` — makes discovery idempotent, so a retried
  role does not duplicate people. `check (gate_state <> 'failed' or gate_reason is not null)` — a
  failed ref with no reason is what makes the excluded list useless, so the database refuses it.
  `candidate_id` nullable and `on delete cascade`.

**Edge cases:** `location_region` is nullable (unknown location is common and must not break inserts). `score` is `numeric`, not float — it is displayed and compared.

**Tests:** `test_schema.py` — assert the cascade actually deletes evidence; assert an empty snippet is rejected; assert a duplicate `(kind, value)` is rejected.

**DoD:** deleting a candidate row leaves zero orphaned evidence or identity rows.

## 1.3 Fetch layer

**Why:** every scraping adapter goes through this, so the compliance rules live in exactly one place. Built before any adapter so no adapter can bypass it.

**Deps:** 1.2.

**Files:** `src/hi/fetcher.py`.

**Interface:**

```python
async def fetch(url: str, *, adapter: str, max_age_hours: int = 24) -> Fetched
# Fetched: (fetch_id, url, status, text, from_cache, body_path)
# raises PolicyDenied | RobotsDenied | FetchFailed
```

**Build, in this order (each is a gate before the request goes out):**

1. Resolve domain → `source_policy`. No row, or `enabled=false` → `PolicyDenied`. **No fallback, no default-allow.**
2. Cache lookup by `url_hash` within `max_age_hours` → return with `from_cache=True`.
3. `robots.txt` check when `respect_robots` (cache the parsed file 24h) → `RobotsDenied`.
4. Per-host token bucket at `rate_limit_rps`, plus jitter. Check `daily_fetch_cap`.
5. `httpx` GET with descriptive UA including a contact address, 20s timeout, 3 retries with exponential backoff on 5xx/timeout only.
6. Write body to `var/cache/<sha256[:2]>/<sha256>` and a `fetch` row.

**Edge cases:** 4xx is never retried. Non-HTML content types (PDF) are stored raw for a separate parser. A body over `MAX_BODY_BYTES` (default 5 MB) is truncated and flagged. A redirect to a different domain re-enters the policy check from step 1 — this is the hole through which an off-policy domain would otherwise get fetched.

**Tests:** `test_fetcher.py` against a local stub server — policy denial, robots denial, cache hit, retry-then-succeed, 4xx-no-retry, cross-domain redirect re-checks policy.

**DoD:** a domain with no enabled `source_policy` row cannot be fetched by any code path. Prove it with a test that walks every adapter.

## 1.4 Canon data

**Why:** skills and locations must be normalised before matching means anything (B5.3, B5.5).

**Deps:** 1.2.

**Files:** `data/skills.yaml`, `data/regions.yaml`, `src/hi/canon.py`.

**Build:** `skills.yaml` maps canonical name → kind + aliases. Seed ~150 entries covering your actual stack; do not attempt completeness. `regions.yaml` maps region code → member city/area names (`IN-DL-NCR`: Delhi, New Delhi, Gurgaon, Gurugram, Noida, Greater Noida, Faridabad, Ghaziabad). `canon.py` loads both into the DB on migrate and exposes `canonical_skill(text) -> str | None` and `region_of(text) -> str | None`.

**Edge cases:** case and punctuation insensitive (`Node.js`, `nodejs`, `node js`). Unknown skill returns `None` and is recorded as a `skill_alias_proposal`, never guessed. A city in two regions resolves to the more specific one.

**Tests:** table-driven — `reactjs`/`React.js`/`REACT` → `React`; `Gurgaon` → `IN-DL-NCR`; unknown input → `None` plus a proposal row.

**DoD:** no code anywhere compares a raw skill string.

## 1.4a Mode B yield spike — do this before writing any adapter

**Why:** the entire architecture forks on one unmeasured number, and measuring it is a day's work.
See `ARCHITECTURE.md` §7.4 and `PRD.md` §13.1.

**Deps:** none. Run it before 1.4b.

**Files:** `spikes/mode_b_yield.py` (throwaway — not shipped, not tested, deleted after).

**Build:** take ~50 LinkedIn profile URLs of Indian engineers, gathered by hand or from a mode-A
query. Fetch each **logged out** — no session, no cookies, no account — at human pace with a real
User-Agent. For each, record: did the page render a profile at all; is there an experience section;
how many entries; **how many carry `from_date` and `to_date`**.

**The output is one number:** the share of profiles yielding dated employment history. That number
decides whether the product ships with zero account risk (mode B alone) or needs mode C.

**Stop conditions:** if a challenge or block appears, stop and record it — that is also a result, and
it is the one that argues for a Recruiter seat over mode C. Do not add proxies to push through; that
is mode E and it is not authorised without §7.5's trigger.

**DoD:** a number, written into `PRD.md` §13.1, and a go/no-go on mode C recorded with it.

## 1.4b `linkedin_serp` discovery + the snippet gate

**Why:** the only discovery route that exists — the scraper in `linkedin_scraper/` has no
people-search — and the only one with zero LinkedIn-side risk.

**Deps:** 1.3, 1.4.

**Files:** `src/hi/adapters/linkedin_serp.py`, `src/hi/gating.py`.

**Build:**

- `plan(spec)` → `site:linkedin.com/in/` queries combining canonical titles, skills and region member
  names. One query per (title × region), plus skill variants. Write every query to `role_query`.
- `discover(q)` → `CandidateRef(kind="linkedin_url", value=url)` **plus the snippet**: name, headline
  (title + employer), location. Parse from SERP metadata; never fetch the profile here.
- `snippet_gate(ref, spec)` in `gating.py` — pure function, no I/O. Tests title, employer and region
  against the spec using the snippet alone. Returns pass/fail **and the reason**, which is stored so
  the excluded list can show it.

**Why the gate is its own module:** it runs before any fetch and decides how much of the enrichment
budget a role spends. It is pure and therefore cheap to test exhaustively, like `scoring.py`.

**Edge cases:** SERP returns `/pub/` and localised (`in.linkedin.com`) URL forms — normalise to a
canonical `linkedin.com/in/<slug>` before dedup, or the same person enters twice. Headlines are free
text and often aspirational ("Ex-Google | Building the future") — the employer parser must fail to
`None` rather than guess. Non-person pages (company, school, jobs) must be dropped by URL shape.

**Tests:** recorded SERP fixtures. Assert: URL normalisation dedups the three URL forms; a company
URL is rejected; the gate's reason string is populated on every failure; gate is deterministic.

**DoD:** for a typical Python/backend Bangalore role, ≥200 refs discovered and ≥40 surviving the
snippet gate, with every rejection carrying a reason (PRD R2).

## 1.4c `linkedin_profile` enrichment — built 2026-08-27, mode C, ships disabled ✅

**Built as mode C, not mode B.** Mode B is blocked by `robots.txt` and stays blocked (`PRD.md`
§13.1). Everything below still holds — the evidence shape, the bans, the edge cases — with three
changes of record:

- The page is reached by **CDP-attaching to a browser the operator started and signed into**
  (`attached_page`), never by launching one and never with a credential. `core/auth.py` is still
  unported, and a test asserts the adapter contains no path to it.
- **Mode C is M1 after all**, not M3, because without it a LinkedIn-discovered person has no
  strong key, no artefact evidence, and no score at all — which was the largest hole left in the
  spine. It ships **disabled**, and enabling it requires a named human who accepts the account
  risk.
- 1.4a's measurement moved with it: `python -m hi.adapters.linkedin_profile measure` reports the
  dated-experience yield while storing nothing about anyone.

Beyond the spec below, it also extracts **strong keys** (a linked `github.com` account, a hashed
email) — which is what makes the GitHub verifier reachable for these people, and is the actual
reason the step matters.

**It parses page text, not CSS selectors.** Verified live 2026-08-27: LinkedIn profile pages ship
obfuscated build-generated class names (`cc605e5a`), entries are not list items, and every selector
in the vendored scraper matches zero elements — a run reported five profiles enriched and wrote no
evidence at all. Hashed classes change on each LinkedIn rebuild, so the rendered text of
`details/experience/` and `details/education/` is parsed instead; both share one shape (two lines
above a date-range line). Fixtures captured from a real profile live in
`tests/fixtures/linkedin_profile/`. **Accomplishments were authorised and then dropped the same
day** — their page has no date line to anchor on, and nothing consumed them.

Limits live in code (`MAX_PROFILES_PER_DAY = 15`, `MIN_DELAY_SECONDS = 20`, serial, self-disabling)
with a test that fails if any of them becomes a setting.

**Deps:** 1.4a (the number), 1.4b, 1.6.

**Files:** `src/hi/adapters/linkedin_profile.py`.

**Build:** wrap the vendored scraper's `Person` model. Runs **only** on refs that passed the snippet
gate. Mode B (logged out) is the default and the only mode M1 ships. Emit one `evidence` row per
`Experience` — `position_title`, `institution_name`, `from_date`, `to_date`, `location` — all tier
`self_reported`, each with the profile URL as `source_url` and a verbatim snippet.

**Do not port `core/auth.py`.** It loads `LINKEDIN_EMAIL`/`LINKEDIN_PASSWORD` from `.env`, which is
banned by `ARCHITECTURE.md` §7.4. Mode B needs no auth at all; mode C, if it is ever needed, is a CDP
attach to a running browser and is M3.

**Drop at extraction, never store:** graduation dates, photographs, age, interests. A LinkedIn
profile hands these over freely and `BANNED_SIGNALS` forbids them (§5.3). Assert this in a test — it
is the easiest rule in the system to violate by accident here.

**Education is the one exception, added 2026-08-27** (`ARCHITECTURE.md` §2.2): the institution and
degree are stored and shown on the card, because some JDs specify IIT/IIM. It is still absent from
`FEATURE_ALLOWLIST` and cannot reach the score — a test asserts an education row moves `score`,
`components` and `gates` by nothing. Graduation dates remain refused.

**Edge cases:** overlapping employment spans must not double-count years; concurrent roles are
common. "Present" as `to_date`. Dates given only as years. Missing dates entirely — emit the
experience without a computed span and let `seniority_fit` fall back, flagged so the card can say so.

**Tests:** fixtures for a full profile, a dateless profile, overlapping spans, and a profile with
education present (assert nothing from it is written).

**DoD:** a gated ref becomes dated employment evidence, and `seniority_fit` reads a computed number
with the fallback path exercised by a test.

## 1.5 GitHub adapter

**Why:** the verifier. Not the discovery route any more (`ARCHITECTURE.md` §2.1) — its job is to test
the claims mode B produced. "Says 8 years of Python" against "has 8 years of public Python commits"
is the product's whole differentiator, and this is the adapter that produces the right-hand side.

**Deps:** 1.3, 1.4, 1.4c (it verifies what enrichment claimed).

**Files:** `src/hi/adapters/github.py`.

**Build:**

- `resolve(candidate)` → find the GitHub login for an already-discovered person: a github.com link in
  the LinkedIn profile, a matching personal domain, or a hashed-email match. **Strong keys only** — if
  none resolves, the candidate simply has no artefact evidence, which is a valid and visible outcome.
  Never name-match; a wrong resolve attributes someone else's work to a candidate and is the worst
  bug this system can produce.
- `plan(spec)` → retained as a *supplementary* discovery route only, off by default. Repo-contributor
  search finds people whose profile is thin but whose work is not, which is genuinely valuable; turn
  it on once the spine is working, and measure it as its own adapter yield.
- `collect(ref)` → user object, repo list (owned + significantly contributed), per-repo language bytes, recent commit dates, profile README.

**Rate limits — the real constraint:** 5,000 req/hr overall but **30 req/min on search endpoints and 10 req/min on code search**. Use a token bucket sized on the *search* limit, read `X-RateLimit-Remaining` and back off before exhaustion, and prefer GraphQL to batch profile+repo fetches into one request. Cap repos per user (default 30, most-recently-pushed first).

**Edge cases:** organisation accounts must be skipped (`type != "User"`). Forks with no commits by this user prove nothing — exclude unless the user has commits. A repo whose only language is Markdown is not skill evidence. Deleted or renamed logins 404 — record and move on. `location` is free text and frequently a joke ("localhost") — unresolvable → `None`, never a bad guess.

**Tests:** recorded fixtures in `tests/fixtures/github/`, no live network in CI. Assert: org skipped, fork-without-commits produces no skill evidence, rate-limit header triggers backoff.

**DoD:** for a shortlist of ~40 enriched candidates, resolves a GitHub login for a measurable share
of them and writes `artifact_backed` evidence with real `source_url`s. The number that matters is
**verified share** (`PRD.md` §9), not refs discovered.

## 1.6 Evidence writer and extraction framework

**Why:** the invariant that every claim is quotable has to be enforced in one chokepoint or it will erode.

**Deps:** 1.2.

**Files:** `src/hi/extract/__init__.py`, `src/hi/extract/writer.py`, `src/hi/extract/llm_extract.py`, `src/hi/llm.py`, `src/hi/prompts/extract_profile.md`.

**Interface:**

```python
def write_evidence(candidate_id, rows: list[EvidenceRow], *, source_text: str | None) -> int
# returns rows written; drops any row whose snippet is not a substring of source_text
```

**Build:** deterministic extractors for JSON APIs (GitHub language bytes → skill evidence, tier `artifact_backed`). `llm_extract` handles unstructured text per `AGENTS.md` §2.3: strict pydantic schema, temperature 0, snippet-substring validation, one retry, then quarantine to `extraction_failure`.

**Edge cases:** snippet whitespace is normalised before the substring check (models reflow whitespace) but not otherwise altered. A malformed LLM response writes **zero** rows. Duplicate `(candidate, claim_type, claim_key, source_url)` is an upsert on `observed_at`, not a second row. Reject any row whose `claim_type` touches `BANNED_SIGNALS`.

**Tests:** a fabricated snippet not present in the source is dropped; a malformed response writes nothing; whitespace-reflowed snippet is accepted; banned claim type is rejected.

**DoD:** every row in `evidence` has a snippet verifiably present in its source document. Assert it as a repo-wide invariant test over the fixture corpus.

## 1.7 Identity resolution

**Why:** the same person arrives via user search and via a contributor list, and a false merge is worse than a duplicate.

**Deps:** 1.2.

**Files:** `src/hi/identity.py`.

**Interface:**

```python
def resolve(refs: list[CandidateRef]) -> uuid   # existing candidate or a new one
def propose_merge(a: uuid, b: uuid, reason: str, evidence: dict) -> None
```

**Build:** look up each strong key in `identity`. Exactly one candidate matched → attach. Zero → create. **More than one → attach to neither; write `identity_review` and keep both.** Weak signals (same display name + same city, same avatar hash) only ever *propose*.

**Edge cases:** two candidates each holding a different strong key that now provably belong to one person is the multi-match case — the review queue, never an auto-merge. A merge must be reversible: record which identity/evidence rows moved. Never merge on name alone, however unusual the name.

**Tests:** single-key attach; new-candidate create; multi-match creates a review row and merges nothing; merge-then-unmerge restores the prior state.

**DoD:** no code path merges two candidates without a `decided_by` value.

## 1.8 Role intake and query planning

**Why:** the entry point, and Human Gate 1.

**Deps:** 1.4, `llm.py`.

**Files:** `src/hi/prompts/parse_jd.md`, `src/hi/roles.py`.

**Build:** JD text → LLM → draft `RoleSpec` → recruiter edits in a form → `role.spec_json` with `status='open'`. Then `plan_queries(spec)` fans out to each enabled **discovery** adapter's `plan()` and writes `role_query` rows.

**Model:** `claude-haiku-4-5` via the `anthropic` SDK, structured outputs (`output_config.format`)
against the `RoleSpec` schema so the response validates rather than needing a parser. ~30 calls a
month, ~3k tokens each — **about ₹1/month**, which is why there is no cheaper option worth the
complexity. A JD contains no candidate personal data, so this is the one call-site where a free tier
whose terms allow training on inputs is acceptable if zero spend is required (`ARCHITECTURE.md` §10).

**Fail-safe:** an unparseable JD produces an empty form beside the raw model output. Never a
fabricated spec — a plausible wrong spec silently searches for the wrong person for the whole role. Enrich and verify adapters are not planned — they are driven by refs that survived the snippet gate.

**Edge cases:** a JD listing 20 must-haves produces zero results — warn above 6 must-haves and suggest demoting some to nice-to-have. A JD with no location and `remote='onsite'` is contradictory — block confirmation. Unparseable JD → empty form plus raw output, never a fabricated spec.

**Tests:** golden JD fixtures → expected spec fields; the >6 must-have warning fires; contradictory spec is rejected at confirm.

**DoD:** a recruiter can paste a real JD and confirm a spec they agree with in under two minutes.

## 1.9 Scoring

**Why:** the product's actual judgement, and the part that must be defensible.

**Deps:** 1.6, 1.8.

**Files:** `src/hi/scoring.py`, `migrations/002_seed_weights.sql`.

**Interface:**

```python
def evaluate(spec: RoleSpec, ev: list[Evidence], weights: Weights) -> MatchResult
# MatchResult: (score, components: dict, gates: dict)  -- pure, no I/O
```

**Build:** gates then the **five** components exactly as ARCHITECTURE §5.1–5.3. `FEATURE_ALLOWLIST` and `BANNED_SIGNALS` as module constants. Assert `components.keys() == FEATURE_ALLOWLIST` before returning. Seed a `v1` weights row; all weights equal until there is evidence to change them. `domain_proximity` is deliberately **not** a component in v1 — domain evidence is displayed, not scored.

**Edge cases:** a candidate with no evidence at all scores 0 and fails gates — must not divide by zero. `availability` unknown ⇒ 0, never negative. A candidate failing a gate is still stored with `gates_json` recording which one, so "show excluded" works. `skill_depth` must be log-scaled or one prolific committer dominates every shortlist.

**Tests:** pure-function tests are cheap, so be thorough here. Property: adding an evidence row never lowers the score. Property: `components.keys() == FEATURE_ALLOWLIST` for every input. Explicit: a banned signal present in evidence changes nothing about the score. Explicit: gate failures are recorded, not silently dropped. Golden: a hand-built candidate scores exactly the expected value (catches accidental weight drift).

**DoD:** two runs over identical inputs produce byte-identical `components_json`.

## 1.10 Job queue and worker

**Why:** a role search takes minutes; it cannot block an HTTP request, and it must survive a restart.

**Deps:** 1.2.

**Files:** `src/hi/worker.py`.

**Build:**

```sql
update job set state='running', locked_at=now(), locked_by=$1
where id = (select id from job where state='queued'
            order by created_at for update skip locked limit 1)
returning *;
```

One worker process, sequential. Job kinds: `discover`, `collect`, `score`. A job may enqueue children.

**Edge cases:** a worker dying mid-job leaves `state='running'` — a reaper requeues jobs whose `locked_at` is older than the lease (default 15 min) and increments `attempts`. `attempts >= 3` → `failed`, never an infinite loop. Enqueuing must be idempotent: `unique (kind, payload_hash)` on unfinished jobs, or a double-clicked button doubles the API spend.

**Tests:** two concurrent workers never claim the same job; a stale lease is requeued; 3 failures mark it failed; duplicate enqueue is a no-op.

**DoD:** kill -9 the worker mid-search, restart, and the search completes without duplicate fetches.

## 1.11 Recruiter UI — minimal, built for non-technical users

**Why:** the users are recruiters, not engineers. Without a UI there is no product, and without plain language there is no adoption. This is also where explainability either lands or doesn't.

**Deps:** 1.8, 1.9, 1.10.

**Files:** `src/hi/web/`, `src/hi/web/templates/`.

**Scope: four screens. Not five, not eight.** Server-rendered Jinja + HTMX. No build step, no npm, no JavaScript framework.

### Screen 1 — Roles (home)

A list, and one primary button. Columns: role title · status · candidates found · shortlisted · last run. Row click opens the shortlist. Button: **New role**.

### Screen 2 — New role (two steps, one page)

**Step 1:** one large textarea, "Paste the job description". One button, **Read it**.

**Step 2:** the parsed spec as an *editable form*, not JSON. Must-have skills and nice-to-have skills as removable chips with an add box. Experience as two number inputs. Location as a multi-select of region names ("Delhi NCR", "Bangalore") — never region codes. Remote as four radio buttons in plain words: *In office · Hybrid · Fully remote · Remote, will relocate*. Then **Find candidates**.

Above the button, one line of plain-language expectation setting: *"We search public work — code, projects, portfolios. We don't search LinkedIn or resume databases."* This sentence prevents the single most likely disappointment (ARCHITECTURE §9.1).

### Screen 3 — Shortlist (the main screen)

A ranked list of rows. Per row: name · **match label** · top three proven skills · location · last active · three action buttons.

**Score presentation is the critical non-technical decision.** Do not show `0.82`. Show a label plus a five-segment bar: **Strong match** / **Good match** / **Possible match** / **Weak**. Thresholds live in config, not in templates. The raw number is available on hover for whoever wants it.

Actions are three buttons with plain labels: **Shortlist · Maybe · Not a fit.** They write `recruiter_action` and grey the row without a page reload.

While a search runs, the page shows a progress line ("Searched 140 profiles, found 22 so far") and streams results in via HTMX polling every 3s. **Never an empty page and never a spinner with no number** — a recruiter watching a blank screen for four minutes will close the tab and not come back.

Top of the list: a count, and one toggle, **Show candidates we ruled out** — revealing gate failures with plain reasons ("No public evidence of PostgreSQL", "Located in Pune, outside Delhi NCR"). Hidden exclusions are indistinguishable from bugs.

### Screen 4 — Candidate detail (a side panel, not a page)

Opens over the shortlist so the recruiter never loses their place. This screen is the entire point of the product.

- Header: name · location · estimated experience shown as a **floor** ("5+ years of public work") · links out to their public profiles.
- **Why this ranking** — the score components as a short labelled bar chart in plain words: *Skill match · Depth of evidence · Experience fit · Recent activity · Availability signal*.
- **What we found** — every claim as a row, grouped by type. Two badges only, and they must be unmistakable at a glance:
  - **Proven** (green) — `artifact_backed`. "Python — 14 repositories, 2.1k commits, most recent June 2026"
  - **Says so** (grey) — `self_reported` / `third_party_stated`. "Kubernetes — listed on personal site"

  Each row carries a source link and the verbatim snippet, shown inline, not on hover — hover text is invisible on touch devices and to anyone who doesn't know to try it.
- **Previously seen** — prior `recruiter_action` on other roles ("Not a fit — Backend II, March 2026"). Prevents the most annoying possible failure: re-presenting someone already rejected.
- Same three action buttons as the row, plus a free-text note.

### Admin (engineer-facing, unlisted)

Adapter yield table, per-adapter enable toggle, cost per role search, weight proposals, identity-review queue. Not linked from the recruiter navigation. Deliberately ugly — it is a control panel, not a product surface.

**Language rules, enforced in review:** no `artifact_backed`, `tier`, `adapter`, `evidence`, `gate`, `spec`, or `score` in any recruiter-facing string. The words are *proven*, *says so*, *source*, *ruled out*, *match*.

**Edge cases:** a search still running shows partial results, always with a number. A candidate whose source page has since been deleted still shows the stored snippet with the link marked "page no longer available" — the snippet is exactly why we store it. Zero results shows what was searched and which gate eliminated the most people, never a bare "no results". A role search that fails shows what partially succeeded.

**Tests:** smoke tests asserting 200 on all four screens; a test asserting Proven and Says-so render with distinct CSS classes; a test asserting an excluded candidate is absent from the default list and present behind the toggle; a lint test asserting no banned jargon string appears in any template.

**DoD:** a recruiter who has never seen the system runs a role and answers "why is this person first?" from the panel without asking anyone. That is the acceptance test — run it with a real recruiter, not a developer.

## 1.11a Health page — `/admin/health`

**Why:** `ARCHITECTURE.md` §9a.3.2. Every failure mode in §9a.1 is silent. After hand-off there is no
engineer reading logs, so a silent failure is a permanent one. This page is the difference between a
tool that breaks visibly and one that quietly returns nothing for a month.

**Deps:** 1.10, 1.11.

**Files:** `src/hi/web/admin.py`, `templates/admin/health.html`.

**Build:** one table, one row per adapter, in plain words a recruiter can read:

| Adapter | Light | Last success | Rows last 3 runs | Last error |
|---|---|---|---|---|

- **Green means it produced rows recently. Not "it did not throw."** This distinction is the entire
  point of the page: an adapter that runs cleanly and returns zero rows is the exact failure mode of
  a LinkedIn markup change, and a naive health check reports it as healthy.
- Amber: ran without error but returned zero rows on its last run. Red: last run raised, or
  `source_policy.enabled = false` with a `disabled_reason`.
- Also show: page-cache disk usage with an amber at 80% (§9a.3.6), and token spend this month.

**Tests:** an adapter with a successful run and zero rows renders amber, not green. A disabled adapter
renders red with its `disabled_reason` visible.

**DoD:** disable an adapter by hand, run a role, and the page goes red within one run.

## 1.11b Degraded mode — the product works without enrichment

**Why:** `ARCHITECTURE.md` §9a.2. This is the survival strategy, and it is only real if there is a
test for it. Mode B *will* break; the question is whether that ends the product or reduces it.

**Deps:** 1.4b, 1.9, 1.11.

**Build:**

- No stage treats missing enrichment as fatal. With `linkedin_profile` disabled, a role runs
  discovery → snippet gate → GitHub verification → score, using snippet title/employer/city for the
  gates and falling back to the artefact-derived seniority floor.
- The shortlist screen shows a banner naming what is unavailable and what that costs: *"Employment
  history unavailable — seniority is estimated from public work. [why]"*
- `manual_paste` is offered in the banner, because that is the next rung down.

**Tests:** the one that matters most in the suite — run the end-to-end fixture role with
`linkedin_profile` disabled and assert a **non-empty ranked shortlist**, the banner present, and
`seniority_fit` flagged as estimated rather than computed.

**DoD:** flipping one `source_policy` row degrades the product instead of breaking it.

## 1.11c Runbook, backups, secrets

**Why:** `ARCHITECTURE.md` §9a.3.5–8. Cheap, boring, and the reason the tool is still running in six
months.

**Files:** `RUNBOOK.md`, `.env.example`, `scripts/refresh_fixture.py`, `scripts/backup.sh`.

**Build:**

- `RUNBOOK.md` — symptom → check → fix, for a competent generalist who has never read the code. The
  eight required entries are listed in §9a.3.8. First diagnostic in every scraper entry is
  `python scripts/refresh_fixture.py`, which re-records one live profile and fails loudly with the
  parse error when markup has changed.
- All LinkedIn CSS selectors in `src/hi/adapters/linkedin_selectors.py` and nowhere else, so that
  fix is a one-file diff.
- `.env.example` lists every secret with what it is for and **where to get a new one**. Rotation must
  be possible by someone who has never opened the codebase.
- `scripts/backup.sh` — nightly `pg_dump` to a second location, in cron. Restore it once before
  hand-off; an unrestored backup is a rumour.

**DoD:** every box in `ARCHITECTURE.md` §9a.5 is ticked.

## 1.11d Starting a profile read from the role page — panel 2026-08-31, button 2026-09-02 ✅

**Why:** discovery is cheap and effectively unlimited; reading a profile is rationed at 30 a day
across every role. So the moment a role finishes discovery, the recruiter is holding a list of 40
people and a budget that will not cover them, and nothing on screen said so. Enrichment was
CLI-only and the role id had to be copied out of a URL by hand.

**Built: the panel that prints the command.** Not a button — deliberately. Mode C attaches over CDP
to a signed-in browser on the machine running the worker, so a click in a recruiter's tab cannot
start one until that machine is known to have the window open. The panel is the honest half of the
job and it needs no such promise.

**Files:** `web/app.py::_collect_context`, `web/templates/shortlist.html`,
`adapters/linkedin_profile.py::waiting_for_role` / `_by_promise` / `budget_remaining` /
`filter_to_slugs`.

**What it shows,** on the role page below the results:

- how many people this role found and has not read, and how many it has,
- **the budget as one shared pot** — "5 of 30 reads left today, shared across every role" — because
  10 spent on this role are 10 unavailable to the next one, and the window is rolling, not a
  calendar day,
- a count to read, defaulting to `min(waiting, budget)` and capped there, so the printed command can
  never ask for more than the run can pay for,
- the exact commands: the Edge line, then `run --role <8-char prefix> --limit N`, then the worker,
- the queue itself, in `targets_for_role` order, with the first N marked `next run`.

**The ordering had to be extracted, not duplicated.** `targets_for_role` sorts by "does the SERP
snippet already name a must-have skill", resolves identities, and *writes* to `candidate_ref`. A
page render must not write, so `waiting_for_role` is a read-only twin — but it shares `_by_promise`
and it applies the same `only_missing_keys` skip, because a queue whose order differs from the run
whose command it prints is worse than no queue. A test asserts the two lists are identical.

**`--only <slug>,<slug>` on the CLI,** added the same day. Re-reading three named people used to
cost the whole day's budget, because target order is fixed and the people you want are rarely at the
front of it. It implies `--redo`. A slug matching nothing is a hard refusal: scraping six of the
seven people you named looks exactly like a successful run.

**Tests:** the panel names *this* role and not another; the queue equals `targets_for_role` exactly;
an already-read person is listed but not queued; `--only` refuses an unmatched slug.

**Built 2026-09-02 — the button.** Five steps, in the order they had to happen:

1. **The advisory lock** (`hi/lock.py`, `pg_try_advisory_lock`). It sits **inside**
   `linkedin_profile.enrich`, not in the CLI and not in the handler, because the CLI, the worker and
   the button are three call-sites and a fourth will be added by someone who does not know about the
   lock. A second concurrent run gets `stopped_reason = "another profile read is already running"`
   and never opens the browser. Released when the connection closes, so a Ctrl-C'd run does not
   wedge the next one the way a `state = 'running'` row would.
2. **`enrich` in the worker** — `handle_enrich`, thin on purpose: it resolves targets, calls
   `enrich()`, writes the note, and queues `collect` + `score` when the run produced anything. Every
   limit (cap, pacing, wall detection, kill switch, lock) stays in the adapter, so a read started
   from a page and a read started from a terminal cannot drift apart.
3. **`POST /roles/{id}/enrich`** with a preflight that refuses in sentences before queueing anything:
   switched off, budget spent (naming the *time* it frees up, because the window rolls), nothing
   listening on `127.0.0.1:9222`, a read already running for **any** role, nobody left to read. The
   preflight is advisory — the lock is still the guard — and it exists so a recruiter finds out now
   rather than twenty seconds into a run they cannot see.
4. **Progress**, as `job.note` (migration 014) written per profile and polled by `_reading.html`. The
   polling attributes live on the fragment's own root with `hx-swap="outerHTML"`, so the poll stops
   when the run does instead of hammering the endpoint forever.
5. **The stopped state.** A blocked run writes `Stopped: …` into the note and the panel renders it in
   the warning style, permanently, under "Last read". A button is exactly where a silent zero-row run
   would get hidden, and silent success is the characteristic LinkedIn failure.

**The command panel stays** (owner's instruction, and it is right). It is not a fallback for the
button: it is the only route when the tool and the signed-in browser are on different machines, and
it is how an operator watches a run in detail. The kill switch and a closed browser hide the
*button*, never the commands — and in those two states the "Or run it yourself" block renders open
rather than folded, because it is then the only way in.

**Overriding the snippet gate, 2026-09-02** (owner's request). The gate refuses people before
anything is read, and its reason is shown — but on the first live business-operations role it
refused 12 of 23 on snippet text alone, and a recruiter looking at those reasons had no way to
disagree except to widen the search and pay for discovery again. The panel now offers two more
buttons next to the default one:

- **Read the N ruled out** — spends the quota only on the refused refs (`scope=ruled_out`).
- **Read everyone — all N** — both sides of the gate, `min(waiting + refused, budget)` (`scope=all`).

**How:** one `scope` field, resolved once in `worker.GATE_SCOPES`
(`passed -> "passed"`, `ruled_out -> "failed"`, `all -> None`) and threaded as `gate_state` through
`_by_promise` -> `waiting_for_role` / `targets_for_role`. Nothing else changes: the daily cap, the
lock, the pacing and the preflight are the same code, so an override cannot outspend the quota. An
unknown scope falls back to `"passed"` — a typo in a form field must never spend the day's reads on
people the gate refused. `scope` is omitted from the job payload when it is the default, so the
existing payload (and its dedup hash) is byte-identical to before.

**The gate's verdict is a default, not a ruling** — but it is still recorded: `gate_state` and
`gate_reason` stay as written, an override does not rewrite them, and scoring gates still apply, so
a person read this way can still rank as ruled out on real evidence. What changes is that "we never
opened it" stops being permanent. Reading one also removes them from the "we did not open" list,
which otherwise claimed we had not opened a profile we had just read.

**Mode C still ships disabled.** A fresh database refuses every read (migration 010) and the page
says so; `python -m hi.adapters.linkedin_profile enable` is still the only way to turn it on. The
button changed who *starts* a read, not whose account carries the risk.

**`--by` became optional 2026-09-02** (owner's decision: the tool has one named operator, Saum). The
flag asked "whose account carries this?", and with a single operator that question has one answer, so
requiring it retyped was friction rather than a safeguard. What §7.4 requires is that the answer be
*recorded* — absent the flag the signed-in OS user goes into `tos_note`, which on a one-operator
machine is the same fact. The risk sentence still prints on every enable, and the ban on burner,
fabricated and shared accounts is untouched.

**Files:** `hi/lock.py` (new), `migrations/014_job_note.sql` (new),
`web/templates/_reading.html` (new — the panel extracted so it can poll),
`worker.py::handle_enrich` / `set_note` / `pending` / `latest_job`,
`web/app.py::start_reading` / `reading_panel`,
`linkedin_profile.py::enrich` (now the lock wrapper) / `_enrich` / `policy_blocked` /
`browser_attached` / `budget_frees_at`.

**Handler contract changed:** handlers now take the `Job`, not the payload, because a handler that
reports progress needs its own id. `handle_discover(job)`, `job.payload` inside.

**Tests:** the lock admits one holder and frees itself on a closed connection and on an exception;
`enrich` refuses itself while a run is in flight, *before* attaching; the button caps `limit` to what
the budget can pay for; each of the five refusals returns its own outcome and queues nothing; a
running read shows its note and offers no second button; a stopped read says "Stopped" on the page;
the fragment stops polling when the run ends; the kill switch hides the button but never the command.

**DoD met:** a recruiter completes paste-JD → confirm → discover → read → ranked list without typing
a command, and every refusal renders as a sentence.

## 1.11e "Not enough people?" — searching deeper, and re-checking who is already here — built 2026-09-01 ✅

**Why:** a live role came back with 24 people and no way to ask for more without an
engineer. Depth was already a knob (`run_discovery(pages=)`, set once at role creation and
never again) but nothing surfaced it. The same run also carried two people abroad, because
the location fix of 2026-08-31 changes what *future* searches keep and nothing else — the
verdict is written to `candidate_ref` at discovery time, and correcting it meant an
engineer running `scripts/regate_refs.py`.

**One button does both,** deliberately, because a recruiter looking at a thin list cannot
tell which of the two problems they have:

- **Deeper.** `POST /roles/{id}/find-more` enqueues `discover` at `search_depth(role) + 1`.
  Costs SERP searches and takes minutes, so it goes through the queue like any other run.
- **Re-checked.** `discovery.regate` recomputes every stored ref's verdict against the role
  as it stands now. No network, no spend, instant. Runs even at maximum depth, because it
  is the half that is always free.

**Depth is read off the discover jobs' payloads** (`discovery.search_depth`), not a new
column on `role`: the payload is already the record of what was asked for, and a second
copy would drift. A job queued before `pages` existed counts as 1, so the button works on
exactly the old roles that need it.

**The gate decision has one home.** `scripts/regate_refs.py::regate` now calls
`discovery.regate`; two copies of a gate rule is how the CLI and the button come to
disagree about who is in.

**Three refusals, each a sentence on the page and not a silent no-op:**

| | |
|---|---|
| a search is already running | `more=busy` — a second would pay twice for the same pages |
| already at `linkedin_serp.MAX_PAGES` (5) | `more=deepest`, the button is not rendered, and the page says to widen the titles instead |
| unknown role | 404 |

The outcome travels back as a query param and is whitelisted against `MORE_OUTCOMES`
before it reaches the template — it comes off a URL a recruiter can edit or bookmark.
Post-redirect-get, so a refresh cannot re-run the search.

**Not built:** going *wider* — more job titles, another location. That is an edit of the
role, and editing a role that already has results is a different and much larger question
(what happens to the people found under the old titles). Say "start a new role" until
someone asks twice.

**Tests** (`tests/test_web.py`): the next search is deeper than the last; a role with no
`pages` in its payload still goes to 2; a person abroad flips to ruled-out and a local one
does not; a flipped ref always carries a reason; a running search is refused without
queueing; maximum depth renders a sentence and no button, and still re-checks.

## 1.12 Access control

**Why:** the app holds personal data about named individuals. "Internal tool" is not a network boundary, and the DPDP Rules make security safeguards an explicit obligation. I missed this in the first draft of the plan; it belongs in Milestone 1, not later.

**Deps:** 1.11.

**Build:** whichever is cheapest given your existing setup — Google OAuth if the team is on Workspace, otherwise HTTP basic auth over TLS behind a reverse proxy. Session cookie, `secure`, `httponly`, `samesite=lax`. Every mutating route requires a session and stamps `actor` from it — the `actor` columns already exist in the schema and must stop being null.

**Edge cases:** no anonymous read of any candidate data, including the admin page. Logout must actually invalidate. Do not build roles or permissions — every user is a recruiter, and the admin page is unlisted rather than access-controlled until there is a reason.

**Tests:** every route returns 401/302 unauthenticated; `actor` is populated on a `recruiter_action` created through the UI.

**DoD:** no candidate data is reachable without logging in.

**Milestone 1 done when:** a recruiter runs a real open role, gets ≥20 candidates, and says the top 10 are worth contacting.

---

# Milestone 2 — broaden discovery

## 2.1 `serp_web` adapter — built 2026-08-28 ✅

**Deps:** 1.3, 1.6. Highest value in this milestone — and after the PRD §13.1 measurement
(**0 of 9 profiles link a GitHub**) it is the only route that puts a non-technical
candidate above `self_reported` at all.

`src/hi/adapters/serp_web.py`, migration `012_serp_web_policy.sql`,
`tests/test_serp_web.py` (46 tests), fixtures in `tests/fixtures/serp_web/`.

**Built:** SerpAPI as URL discovery only (reusing the already-authorised vendor row and
`linkedin_serp._serpapi_url` rather than adding Serper), then **our** fetcher pulls each
URL and a deterministic parse produces `third_party_stated` employer and title evidence.

```
python -m hi.adapters.serp_web                                  # spend status
python -m hi.adapters.serp_web run --role <prefix> [--limit N] [--redo]
```

**Four departures from the spec above, each deliberate:**

1. **No LLM.** `llm_extract` (AGENTS.md §2.3) is still dark, so the parse is string work:
   the name and the employer must appear in the **same block** of page text, and the
   quoted snippet is that block. Narrower than an LLM — it reads a team page and skips a
   paragraph that names the two facts four sentences apart — but every row it writes
   quotes text a recruiter can go and read.
2. **Open-web domains needed a policy decision**, recorded in `ARCHITECTURE.md` §2.4: an
   adapter-scoped wildcard `source_policy` row, `respect_robots` still true. Rule 4 is
   intact for every other adapter, and a test proves it.
3. **Corroboration, not discovery.** It runs over a role's existing shortlist in ranked
   order, not as a source of new candidates. Employer corroboration is what the tiers
   are missing; new names are what `linkedin_serp` already does well.
4. **No PDF extraction, no `filetype:pdf` query.** A resume PDF is the candidate's own
   document — `self_reported` wearing a third party's file extension — and `selectolax`
   does not read PDFs. Dropped rather than deferred.

**Spend, both caps in code:** a candidate stops costing money at the first page that
corroborates them (so the common case is one query, one fetch), and
`DAILY_FETCH_BUDGET = 40` bounds open-web fetches per day across all roles.

**Bugs the live data found, fixed here:** a four-character floor on employer keywords
silently dropped **IBM, TCS, HCL, SAP, EY, PwC, Ola** — including the only shortlisted
candidate in the dev database. Genericness disqualifies a word, not length; matching is
now whole-word so short keys are safe.

**Bug the live data found, NOT fixed here:** `linkedin_profile.parse_experience_text`
writes `"Senior Software Engineer"`, `"Python Developer"`, `"Full-time"` and
`"Freelance"` into `employer` claims. `serp_web` refuses title-shaped employers so it
cannot spend money on them, but **the enrichment parser is still writing wrong rows** —
see §2.1a.

**DoD:** ✅ tests green off fixtures; ✅ every row carries a verbatim snippet from a real
`source_url`; ✅ blocklist, per-query cap and daily budget all covered by tests.
**Not yet met:** adapter-yield metrics on live data — needs the first live run.

## 2.1a Employer/title mis-assignment in LinkedIn enrichment — fixed 2026-08-28 ✅

**Found** while listing `serp_web` targets against the dev database: `employer` claims
reading `"Full-time"`, `"Internship"`, `"Senior Software Engineer"`, `"Python
Developer"`. A recruiter reading "Employer: Full-time" stops trusting the card, which is
a §9a.5 problem. No *score* was wrong — `seniority_fit` reads dates — only the claims.

**Root cause:** `_entries` took the two lines immediately above each date line as
(title, employer). That is a fixed offset, and LinkedIn ships three layouts. Confirmed
against four cached profile pages:

| Layout | Above the date line | Old reading |
|---|---|---|
| one role, one company | `Naviq · Full-time` | correct |
| promotion group, type on its own line | `Full-time` | **employer = "Full-time"** |
| promotion group, title only | `Senior Software Engineer` | **employer = the title, title = the previous entry's location** |

In a promotion group the employer is named **once**, above a tenure line
(`Full-time · 2 yrs 3 mos`), and the roles beneath it carry no company at all.

**Built:** `experience_entries` in `linkedin_profile.py` classifies each line — company
line, employment type, duration header, furniture — instead of counting offsets, and a
duration header marks the line above it as the group's employer. Where no group is open
the original two-line reading is kept unchanged, so every layout that parsed correctly
before still does. Education keeps `_entries`: its layout is the reverse order, it has no
groups, and it was already right.

Verified against the four cached pages: **same number of entries as before, every
employer now correct**, and each row the old parser produced but the new one does not is
one of the garbage rows. `EXTRACTOR_VERSION` bumped to `linkedin_mode_c@2`.

**`--redo` added to the CLI.** `skip_enriched=False` was previously reachable only by
editing code, which made the repair step above unrunnable.

**Repairing the stored rows** — `scripts/prune_bad_employer_rows.py`, dry run by default.
The evidence key is `(candidate_id, claim_type, claim_key, source_url)`, so re-enriching
*adds* the correct row and leaves the wrong one beside it. The script re-parses the
cached page bodies with the @2 parser and deletes only stored rows whose value the parser
does not produce — no heuristics, no network. 13 rows across 3 candidates.

Two traps it hit, both worth knowing:

- An earlier heuristic version wanted to delete `employer = "Freelance"`, which is
  **correct data** — those profiles really do name Freelance as the organisation on a
  `Freelance · Part-time` company line — and it missed `"Senior Software Engineer"`,
  the row that started this. Guessing from stored values does not work; re-parsing does.
- Fed a *whole* profile capture, the parser returns zero roles: `_significant_lines`
  stops at the first "people also viewed" marker, which those pages render *before* the
  experience section. In production the parser is fed the `details/experience/` sub-page,
  so this is a script-input problem only — but a script that concluded "zero roles" would
  have deleted every row. The script slices from the `Experience` heading and refuses to
  prune a candidate whose pages parse to nothing.

**Tests:** `tests/fixtures/linkedin_profile/experience_promotion_group.txt` reproduces
all three layouts with synthetic names. Asserts the right employer for each, that no
employment type or job title is ever an employer, that a bullet or `Skills:` line is
never a title, that a group closes when a standalone entry follows, and that the common
layout parses exactly as before.

## 2.2 Remaining artefact adapters

`huggingface` and `codeforces` only. Each is small once the interface exists — one file, one fixture set.

**Cut, deliberately:** `gitlab` (near-total overlap with GitHub, thin in India) and `stackexchange` (declining post-LLM relevance — if the plan says "expect to drop it", it shouldn't be built). Both remain trivial to add later behind the same interface. Revisit only if adapter-yield metrics show a gap these would fill.

**DoD:** each adapter is independently toggleable and reports its own yield.

## 2.3 `manual_paste`

Trivial and disproportionately useful: paste a URL or raw text, get evidence rows. It is also the fallback whenever any source blocks us.

**DoD:** a recruiter can add a candidate the pipeline never found.

## 2.4 Rationale generation

Per `AGENTS.md` §2.4. Score already exists; this only adds prose.

**Edge cases:** a discarded rationale must leave the card fully usable. Test the citation validator with a deliberately hallucinated evidence id.

---

# Milestone 3 — LinkedIn escalation, only on measured need

## 3.1 Mode C `attached_browser`, and mode D `people_search`

**Deps:** all of Milestone 1, plus §7.4 of ARCHITECTURE read in full.

**Build the modes in order, and ship each before starting the next:**

1. `serp_only` — snippet metadata only, no fetch. Nearly free, no risk.
2. `paste` — recruiter pastes URL + copied text.
3. `attached_browser` — CDP-attach to the recruiter's own logged-in Chrome.

**Non-negotiable in code, not in config:** never headless; concurrency 1; ≥20s + jitter between profiles; daily cap (default 15) per account; **on the first authwall, challenge, or HTTP 999 the run aborts and sets `source_policy.enabled=false` with a `disabled_reason`,** requiring a human to re-enable. No stored credentials anywhere — the browser is already logged in or the adapter does not run.

**Edge cases:** the cap must be per *account*, not per process, or two runs double it. A detected challenge mid-run must not lose already-extracted evidence. Evidence from modes 2 and 3 is flagged as not covered by the DPDP public-availability exemption (ARCHITECTURE §9.5).

**Tests:** the trip fires on a simulated authwall and flips the kill switch; the daily cap holds across two processes; the delay floor cannot be configured below 20s.

**DoD:** the adapter can be disabled by one row update, and adapter-yield metrics can answer "is this worth the risk" after 30 days.

## 3.2 `proxied_public` — mode E, CONTINGENCY, DO NOT BUILD ON SIGHT

Authorised by ARCHITECTURE §2.1, specified in §7.5. **This section is a plan, not a work item.**

**Do not start it because it is written down here.** Three gates, all of which must be passed:

1. M1 and M2 shipped, and modes A–C live for 30 days.
2. The §7.5.1 trigger measured true — all three conditions, from the admin page, not from memory.
3. A dated go decision appended to ARCHITECTURE §7.5 by a named human.

**Before any of that, do the cheap diagnosis first.** A thin shortlist is far more often a tight
location gate, a bad `role_spec`, or a starved query plan than it is missing LinkedIn coverage. The
gate-failure histogram (§4.1) tells you which in about a minute. Escalating source access to work
around a scoring bug spends real risk to avoid a one-line fix.

**If it is built — the shape, from §7.5.2:**

- Logged out. **Assert on every request that no LinkedIn auth cookie is attached** and fail closed if
  one is. This is the single most important line of code in the adapter.
- Enabling mode E sets mode C `enabled=false` **in the same transaction**. Never both. A real account
  arriving over a rotated residential IP is a stronger detection signal than either alone.
- Caps in code: 50/role, 300/month across all roles, concurrency 1, ≥20s + jitter.
- Captcha, challenge or HTTP 999 ⇒ abort, `enabled=false`, `disabled_reason`, human re-enable.
- Captcha solving stays banned. Burner accounts stay banned. Purchased datasets stay banned.
- Evidence flagged outside the DPDP §3(c)(ii) exemption, same as modes 2–3.

**Tests:** a request carrying an auth cookie raises rather than sends; enabling mode E flips mode C
off atomically; the per-month cap holds across processes and across roles; the delay floor cannot be
configured below 20s; a simulated captcha trips the kill switch without losing extracted evidence.

**DoD:** contact rate measured 30 days after go-live. Below 30% ⇒ disable it and accept coverage as
the constraint (ARCHITECTURE §9.1, §7.5.4).

---

# Milestone 4 — operations

## 4.1 Observability

`pipeline_event` + admin page per ARCHITECTURE §8. Adapter yield (discovered → passed gates → shortlisted) is the metric that drives every source decision, including whether Milestone 3 survives.

## 4.2 Erasure on request — and NO retention deletion

**Changed 2026-08-28 by owner decision, recorded in `ARCHITECTURE.md` §2.5.**

~~Nightly job deleting candidates with no `recruiter_action` and no open-role match older than
`RETENTION_DAYS`.~~ **Cancelled. Do not build it.** Candidate data is kept indefinitely: the corpus
is the asset, and someone who did not fit an August role may be the answer to a March one. There is
no `RETENTION_DAYS` and no time-based deletion of anything about a person.

**A test enforces this** so a future cleanup job cannot creep in: `tests/test_handoff.py` fails if
any module under `src/hi/` deletes from `candidate`, `evidence`, `candidate_ref` or `match`.

**Still to build: the erasure endpoint.** Retention is the company's decision; erasure is the
individual's right under the DPDP Act, and it transfers with the tool at hand-off. So this section
keeps its endpoint and loses only its scheduler.

**Build:** an admin action taking a candidate and a recorded reason, which removes the person's
`evidence`, `identity`, `candidate_ref`, `match` and `candidate` rows **and** the cached bodies in
`var/cache` that belong only to them. Fires on a recorded request from a named person, never on a
timer. The module that does it is the single allowlisted exception in the guard test above.

**Edge cases:** a cached body may back evidence for more than one candidate — do not delete a file
another person's evidence still cites. A merged-away candidate (`status='archived'`) must be erased
along with the person they were merged into, or erasure leaves a shadow copy. Record that an erasure
happened, without retaining the erased data, or you cannot answer "did you comply".

**Tests:** erasure removes evidence rows **and** the files on disk — a cascade that leaves bytes in
`var/cache` is not erasure; a shared cached body survives; an archived duplicate is erased too.

## 4.3 Weight proposals

Aggregate `recruiter_action` into suggested weight deltas on the admin page. Human approves, writing a new `scoring_weights` row. Old matches keep their `weights_version`, so history stays interpretable. **No automatic application, ever** (ARCHITECTURE §5.5).

---

# Testing strategy

- **No live network in CI.** Every adapter has recorded fixtures. A test that needs the internet is a test that fails on a Monday.
- **Pure functions get property tests.** `scoring.py` is pure by design specifically so this is cheap.
- **Invariant tests over the fixture corpus** — every evidence snippet is present in its source; every `components_json` matches the allowlist; no banned claim types exist.
- **One end-to-end** against fixtures: JD → shortlist, asserting a known candidate ranks in the top 5.
- **One degraded end-to-end** (1.11b): the same role with `linkedin_profile` disabled must still
  produce a non-empty shortlist. This is the test that protects the product after hand-off.
- **The extraction-drops-banned-fields test** (1.4c): feed a profile fixture containing education,
  graduation year and a photo URL; assert zero rows written from any of them. Easiest rule in the
  system to break by accident, since LinkedIn hands those over unasked.
- **Snippet-gate determinism**: same ref plus same spec yields the same verdict and reason string.
- Skip: load tests, browser E2E, coverage thresholds. Wrong tool for this size of team.

# What not to do

- Do not add Redis, Celery, Kafka, Elasticsearch, a vector DB, or an SPA. Each was considered and rejected; re-adding one needs a stated reason in `docs/ARCHITECTURE.md` §2.
- Do not let an LLM compute a score, merge a candidate, or decide a fetch.
- Do not add a proxy pool or a captcha solver. A target needing one is a target to drop. The sole exception is LinkedIn mode E (§3.2), which is trigger-gated and unbuilt; captcha solving is banned even there.
- Do not build a continuous crawler. Searches are role-triggered.
- Do not store plaintext candidate emails.
- Do not add a scoring feature without adding it to `FEATURE_ALLOWLIST` in the same commit.
- Do not enable a `source_policy` row without filling `reviewed_at`.

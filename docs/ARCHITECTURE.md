# Hiring Intelligence — Architecture

**Status:** finalised 2026-08-22. Supersedes `Project-Doc.md` (kept for history; where they disagree, this document wins).

---

## 1. What we are building

Requirements for a role in → a ranked, evidence-backed shortlist out.

Internal tool. A handful of recruiters. A few open roles at a time. India-primary. Engineering/data/ML roles first, other role families later behind the same adapter interface.

**We are not building** a candidate database, a crawler platform, or a SaaS product.

### The one-line thesis

> A profile tells you who to *consider*; an artefact tells you whether the profile is *true*.
> Discover people from what they publish about their career, verify those claims against what they
> have demonstrably built, and show the recruiter both — side by side, never merged.

### Non-goals

- Scale. ~30 role-searches/month. Anything justified only by scale is out.
- Coverage parity with LinkedIn's *own search UI*. We discover from LinkedIn but deliberately narrow to what a role spec asks for, and we verify before we rank (§9.1).
- Autonomous sourcing. A human confirms the role spec and a human owns every shortlist.

---

## 2. Why this shape (the decisions that cost the most to reverse)

| Decision | Rationale |
|---|---|
| **Role-triggered harvest, not continuous crawl** | Profiles rot. Data minimisation. A role search is minutes and ~$2–4. No crawl fleet, no scheduler, no accumulating stale DB. |
| **Evidence is the primary entity** | Every candidate attribute derives from an `evidence` row carrying `source_url` + `snippet` + `tier`. Buys explainability, per-claim freshness, dedup audit, and DPDP erasure-by-cascade in one move. |
| **Deterministic scoring, LLM prose only** | Scores must be reproducible and auditable. The LLM writes the *rationale*; it never writes the *number*. |
| **Strong-key identity resolution only** | Hundreds of candidates per role, not millions. Fuzzy ER is not worth its false-merge risk. Weak matches go to a human queue. |
| **Postgres is also the queue** | `FOR UPDATE SKIP LOCKED` + one worker. No Redis, no Celery. |
| **Server-rendered UI** | Jinja2 + HTMX. The recruiter surface is a table and a detail card. One deploy artifact. |
| **Profile source is the spine; artefact sources verify it** | A JD screens on employment history — years, employer, title, domain, city. Artefact sources answer none of that. Discovery runs against LinkedIn; GitHub and friends verify the claims discovery produced. Reversal recorded 2026-08-25 in §2.1. |
| **Undefended targets wherever the job allows it** | GitHub/GitLab/HF APIs, personal sites, PDFs and OSS repos still need no proxies and no captcha solving, and every adapter that *can* be undefended is. The exception is the spine, and §7.4 is the whole mitigation for it. |
| **Search API for discovery, our scraper for content** | Scraping Google is the most-defended target on the internet; the substitute costs ~20¢/role. The SERP is used as a sitemap — we fetch and parse every page ourselves. |

### 2.1 Decision reversals (this section is the record required by CLAUDE.md)

**2026-08-25 — LinkedIn becomes the discovery spine; artefact adapters become verification.**
Decided by ayushmaan. This reverses the *ordering* in §2, not the principle behind it.

The reason, and it is a sound one: a JD screens almost entirely on employment history — years in
role, current employer, title progression, domain, city. GitHub answers none of those. Limitations
§9.1 (coverage), §9.3 (location) and §9.4 (seniority skew) were not independent problems; all three
were consequences of using a *skills* source as a *people* source, and all three dissolve when the
spine is a profile source. LinkedIn `Experience` records carry `position_title`,
`institution_name`, `from_date`, `to_date` and `location` — which makes `seniority_fit` **computed
rather than estimated**, and that alone retires §9.4.

**What does not change:** the evidence model, the three tiers, hard gates, `FEATURE_ALLOWLIST`,
deterministic scoring, the fetch chokepoint, strong-key identity, human gates. GitHub moves from
discovery to verification, which is what it was always actually good at. This is a milestone
reorder, not a re-architecture — which is the evidence that the shape was right even though the
source ranking was wrong.

**Discovery and enrichment are now separate problems, and the tool in `linkedin_scraper/` only
solves the second one.** `joeyism/linkedin_scraper` exposes `Person(url)` and a *job* search; it has
no people-search. Discovery therefore comes from §7.4 modes A and D, enrichment from B and C.

What this costs, recorded so the decision stays informed:

- **Every LinkedIn claim is `self_reported`.** The spine is a pile of unverified assertions, so
  separation between candidates comes from the *verification* pass, not from the spine. A build that
  ships discovery without verification has rebuilt LinkedIn search with extra steps and a worse UI.
  Verification is therefore not a later milestone; it is what the product is.
- **DPDP §3(c)(ii) does not cover the primary source any more** (§9.5). This was previously true of
  a contingency adapter. Retention, erasure and purpose limitation stop being cheap posture and
  become load-bearing.
- **The posture moves from "no risk" to "managed risk".** Discovery via mode A touches no LinkedIn
  surface at all. Enrichment does, and §7.4's ladder — logged-out first, attached browser only if
  logged-out is too thin — is the entire mitigation.
- **The burner-account rule survives this reversal unchanged** (§7.4). A fabricated identity is the
  aggravating fact in every LinkedIn enforcement action to date, it returns *less* profile depth
  than an established account, and on a single laptop it contaminates the egress IP that the
  operator's real account also uses. Automation may run in a separate *browser*; it may not run
  under a separate *identity*.

*(Superseded in ordering by the 2026-08-25 entry above; the escalation path itself stands unchanged.)*

**2026-08-23 — conditional reversal of "no residential proxies, no fingerprint evasion, no session
persistence", scoped to LinkedIn only.** Decided by ayushmaan, over a stated objection.

The original ban stands as the **default**. What changed is that a bounded escalation path now
exists, is written down, and has trigger conditions — rather than being improvised under deadline
pressure with no caps. It is specified in §7.5 as mode `proxied_public`, it is **not built** until
its trigger fires, and it requires an explicit go decision at that point.

The objection, recorded so the decision stays informed:

- **"Internal, not commercial" is not a legal safe harbour.** DPDP's carve-out is for *personal or
  domestic* purposes; a company recruiting tool is business processing whether or not the output is
  sold. LinkedIn's User Agreement bars automated access on identical terms either way. In
  *hiQ v. LinkedIn* the CFAA claim went hiQ's way and the **breach-of-contract claim did not** —
  $500k, an injunction, and court-ordered data destruction. Non-commercial use did not feature.
- **The volume cap is the part that actually reduces exposure.** 50 profiles per role is a different
  posture from Proxycurl's account fleet, and that difference is real. It is the cap doing the work,
  not the word "internal".
- **DPDP §3(c)(ii) does not cover this output** (§9.5), same as modes B, C and D.

Accepted anyway as the owner's call on the owner's risk. §7.5 exists to make the escalation bounded
and reversible.

### 2.2 Education becomes storable, never scorable (2026-08-27)

Decided by the tool owner, on the stated business need that some JDs specify IIT/IIM candidates and
a recruiter therefore has to be able to see where someone studied. This is the record `CLAUDE.md`
requires, because it lifts a hard rule.

**What changed.** `signals.REFUSED_CLAIM_TYPES` no longer contains `education`, so the enrichment
adapter fetches `Person.educations` and writes an `education` evidence row carrying the institution
and the degree. It appears on the candidate card like any other self-reported claim.

**What did not change, and why each is deliberate rather than an oversight:**

| Held back | Reason |
|---|---|
| `FEATURE_ALLOWLIST` is untouched — education cannot enter the score | In India, institution tier correlates with caste. A score that reads it is a caste proxy, which `CLAUDE.md` forbids collecting *or inferring*. Storing a fact for a human to read is a different act from ranking on it, and only the first was authorised. A test asserts an education row changes `score`, `components` and `gates` by nothing. |
| Graduation dates are still refused (`graduation_year` stays in `BANNED_SIGNALS`) | A graduation year dates a person, and age is banned independently of this decision. The adapter never reads `from_date`/`to_date`, and leaves `observed_at` to default — putting the year in a timestamp column would re-encode exactly what was dropped. |
| `interests` is still never fetched | It is the one profile section that routinely reveals religion and politics. Nothing asked for it, and not fetching it is the cheapest way to keep that true. |

**The cheaper option, on the record because it was offered first.** `role_spec.extra_keywords`
already ANDs terms into every discovery query (`linkedin_serp.py`), so `extra_keywords: ["IIT"]`
returns an IIT-only shortlist today with nothing stored and nothing scored. The owner chose the
stored-evidence route as well, for visibility on the card. Both are available; the keyword route
remains the one that costs no fairness surface at all.

**If ranking on institution is ever requested**, it needs its own dated entry here, a
`FEATURE_ALLOWLIST` addition in the same commit, and someone willing to own the caste-proxy
argument above. It was advised against.

### 2.3 A weight profile for roles with no public artefacts (2026-08-27)

Decided by the tool owner on being told the tool must also serve non-technical and MBA
roles. The measurement that forced it: **two of the five scoring components read
`artifact_backed` evidence only**, and v1 weighted them 0.20 each. For anyone without
public code that is 40% of the score permanently unreachable, so the ceiling was ~0.47
against a "Strong match" threshold of 0.55 — **no MBA candidate could ever be a strong
match, however good.** It was already biting on LinkedIn-only engineers, which is why
the first real shortlist topped out at 0.250.

| Component | reads | v1 | v1-business |
|---|---|---|---|
| `skill_match` | any tier, discounted | 0.20 | **0.45** |
| `skill_depth` | `artifact_backed` only | 0.20 | **0.0** |
| `seniority_fit` | employment dates | 0.20 | **0.40** |
| `activity_recency` | `artifact_backed` only | 0.20 | **0.0** |
| `availability` | a self-set flag | 0.20 | **0.15** |

**Reweighting rather than moving the labels.** The alternative was lowering the
Strong/Good cut-offs for non-technical roles, which was rejected: a 0.45 reading
"Strong" because the threshold moved means the labels stop meaning the same thing from
one role to the next, and the label is what the recruiter actually reads.

**Chosen per role, never per candidate.** Two people on one shortlist must be scored on
the same scale or the ranking is meaningless. `match.weights_version` records which
profile produced each number, so an old score stays interpretable — the reason that
column exists.

**How a role is classified: deterministically, from the canon.** A role naming a
programming language or framework among its must-haves gets the engineering profile;
one naming only tools, practices, domains or qualifications gets the business profile
(`canon.expects_artifacts`). No LLM is asked, and a recruiter can see why by reading the
skill list. ~~A role with **no** must-have skills keeps the engineering profile.~~
*Superseded by §2.7: no must-haves now means business, and SQL/R alone no longer
counts as code.*

~~**The tier discount is not relaxed.**~~ *Superseded by §2.7.* A listed skill still counts 0.35 against a proven
one's 1.0. `third_party_stated` (0.6) is genuinely reachable for non-technical people —
a company team page, a conference programme, a published interview — so removing the
discount would remove the reason to go and find those. The honest long-term answer for
non-technical roles is `serp_web` giving them a Proven-equivalent, not a heavier weight
on self-assertion.

### 2.4 An adapter-scoped wildcard `source_policy` row for `serp_web` (2026-08-28)

The change CLAUDE.md hard rule 4 requires be recorded here. **The fetch layer is still
the single chokepoint; one adapter now reaches it through a wildcard row.**

`serp_web` is the adapter §2.3 called the honest long-term answer for non-technical
roles, and it is unlike every other adapter in one respect: **its domains cannot be
enumerated in advance.** GitHub is `api.github.com`; LinkedIn is `linkedin.com`. A
company team page, a conference programme or a published interview is whatever a search
returned. There is no list to seed, because the list *is* the result — so the per-domain
review that authorises every other source has nothing to attach to.

**What was chosen:** one row, `domain = '*'`, `adapter = 'serp_web'` (migration 012).
`_get_policy` tries an exact-domain row first and falls back to a wildcard row **only
when the calling adapter matches the adapter named on it**. That scoping is the safety
property, and it is what makes this a narrow exception rather than an open door:
`linkedin_profile` and `github` get `None` for an unlisted domain exactly as before, so
neither can reach the open web through this row.

**What the row gives up:** the per-host human review, and only that.

**What it keeps** — every mechanism that review was protecting:

| Mechanism | Under the wildcard |
|---|---|
| robots.txt | `respect_robots` stays **true**, so it is the per-site gate, read fresh per host. This is the site owner's own answer, which no seeded row could be better informed than. |
| rate limit | 0.2 rps per host, as any row |
| daily cap | 40 per host, plus `DAILY_FETCH_BUDGET = 40` across all hosts, in code |
| audit trail | the `fetch` table records every domain touched — complete after the fact rather than before it |
| exclusions | `BLOCKLIST` in code: job boards, self-published platforms, and people-data brokers |

**LinkedIn is on that blocklist.** It has its own policy rows, its own modes and its own
daily caps; reaching it through the wildcard would route around all three, and a test
asserts it cannot.

**The blocklist is not only a quality filter.** Job boards republish the candidate's own
resume, so a hit there is the candidate talking, mis-tiered as a third party. The
people-data brokers (`rocketreach`, `zoominfo`, `lusha`…) are worse: their pages are
scraped personal data resold without the person's knowledge, and treating one as
corroboration would launder a DPDP problem into the evidence table.

**Rejected alternative:** auto-provisioning a `source_policy` row per newly-seen domain.
It produces the same access with a row per host, so it looks more auditable while making
`reviewed_at` a lie — nobody reviewed those hosts. One honest wildcard with one
`reviewed_at` beats a thousand rows claiming a review that never happened.

### 2.5 No retention deletion — candidate data is kept indefinitely (2026-08-28)

Decided by the tool owner: *"never delete any candidate data, let all of it be in the db."*

**What this cancels.** `IMPLEMENTATION.md` §4.2 planned a nightly job deleting candidates with no
`recruiter_action` and no open-role match older than `RETENTION_DAYS`. That job is **not to be
built.** There is no time-based deletion of a candidate, their evidence, their refs or their
matches, and `RETENTION_DAYS` does not exist.

**Why it is defensible.** The corpus is the asset. A candidate who did not fit a backend role in
August may be the obvious answer to a different role in March, and re-discovering them costs SERP
spend and a scarce mode C profile read. The processing purpose — recruiting for this company — does
not change when a role closes, so the data is not stale in the sense retention rules exist to
address.

**What it does NOT cancel: erasure on request.** These are two different things and only the first
is the owner's to decide:

| | Whose decision | State |
|---|---|---|
| *Retention* — how long we keep data nobody asked us to remove | the company's | **indefinite, per this decision** |
| *Erasure* — removing a person because **that person asked** | the individual's, under the DPDP Act | **still required** |

A data principal's erasure request is a right, not a preference, and it transfers with the tool at
hand-off. So §4.2 keeps its erasure endpoint and loses only its scheduler. The endpoint must remove
evidence rows **and** cached bodies from disk — a cascade that leaves files in `var/cache` is not
erasure — and it fires only on a recorded request from a named person, never on a timer.

**Enforced by a test**, not by this paragraph: `tests/test_handoff.py` fails if any module under
`src/hi/` issues a `delete` against `candidate`, `evidence`, `candidate_ref` or `match`. The one
sanctioned exception, when erasure is built, will be a single named module the test allowlists — so
adding a cleanup job anywhere else breaks the suite.

**Merging already respects this.** `identity.merge` sets `status = 'archived'` and never deletes,
which is why a merge is reversible (§4.2 of the data model).

### 2.6 The job queue runs inside the web process (2026-08-31)

`§6` said one web process and one worker process. It is now one process by default: the web app
starts `worker.run_forever` in its own event loop at startup, and `INLINE_WORKER=false` splits
them again for a deployment that wants that.

**Why it changed.** The split was never a correctness requirement, it was tidiness — and it cost a
recruiter-visible failure the first time the tool was driven end to end by someone who had not read
the runbook. A role was created, `discover` was enqueued, and nothing drained it. The page said
*"Searching — looked at 0 people so far"* for over ten minutes, because from the page's point of
view a queued job and a running job are the same thing. The owner's reaction was the correct one:
clicking **Find candidates** is a request for a search, not a request to enqueue one.

**Why it is safe.** `claim()` is `for update skip locked`. Two drainers competing for one queue is
precisely the case that construct exists for, so `python -m hi.worker` still works unchanged and can
run alongside — during a long enrichment pass, for instance. Nothing about the job semantics
changes; only who calls `run_once`.

**Superseded 2026-09-02 — mode C is now driven by the worker** (`IMPLEMENTATION.md` §1.11d, built).
The `enrich` job kind exists and a button on the role page queues it. The prerequisite named here
was built first and is what makes it safe: `hi/lock.py` takes a Postgres **advisory lock** inside
`linkedin_profile.enrich`, so the CLI, the worker and the button all pass through one guard and the
second concurrent run is refused rather than queued. Concurrency 1 (§7.4) is therefore enforced by
the database instead of by one human running one command at a time — a strictly stronger position
than before the button existed.

The lock is deliberately *inside* the adapter rather than at each call-site: a guard at the
call-sites is a guard the next call-site forgets, and that call-site is the one that puts two runs on
one person's real account. It is released when the connection closes, so a run killed with Ctrl-C
frees it with no janitor and no stale `running` row.

**Two guards, because "it runs" is not the same as "it ran".**

- `run_forever(..., handle_signals=False)` when embedded. Installing a SIGINT handler over uvicorn's
  would make Ctrl-C stop the queue and leave the server serving.
- `matching.role_progress` reports `stalled` — a job queued, never attempted, older than 90 seconds
  — and the role page says *"The search has not started"* and names the missing process. The inline
  worker should make that unreachable; it stays because a page that reports progress nothing is
  making is the same lie as a green health light over a dead adapter, and the next cause will not be
  this one.

**Still owed before hand-off:** the web process is now the only thing that must stay up, so it needs
a restart policy. `ARCHITECTURE.md` §9a.5 gains that line.

### 2.7 Scraped data is scored as true; scoring made tech-neutral (2026-10-02)

Decided by the tool owner after "Senior Business / Operations Manager" ranked 9 of 10
people at 0.00 — every one "Weak" — despite 8 of them having full, dated LinkedIn
histories. The owner's rule: **whatever we scrape is taken as true.** Measured causes,
all of which fell hardest on non-technical roles:

1. **Tier discount.** `self_reported` counted 0.35 toward `skill_match`. Non-technical
   people have nothing to prove a skill with, so they were capped by construction.
   Now every tier counts 1.0 toward the score (`scoring.TIER_MULTIPLIER`).
2. **Over-experience zeroed `seniority_fit`.** A 25-year operations manager on a 3–6
   year role scored 0. Now only `min_years` is scored; more is never a penalty.
   Whether someone is too senior is the recruiter's call.
3. **Misclassified roles.** No must-haves meant engineering weights, and any "language"
   meant engineering — so "Business & Strategy Associate" (SQL beside Excel and
   PowerPoint) was scored *and searched* as an engineering role. Now the business
   profile is the neutral default; `canon.ANALYST_LANGUAGES` (SQL, R, MATLAB, HTML,
   CSS) count as code only when a title says engineer/developer ("Data Engineer").
4. **The must-have gate needed every must-have.** "Business & Strategy Associate" has
   10, and all 21 people read were ruled out — nobody lists ten skills on LinkedIn. Now
   the must-have gate never rules anyone out: **everyone read is listed.** It records
   "has 3 of 10; no evidence for …" so the card still shows what is missing; partial
   coverage is paid for in `skill_match`, and someone with *none* of the must-haves has
   their score multiplied by `NO_MUST_HAVE_FACTOR` (0.25) so they read Weak — the one
   place the score is not the plain weighted sum. Location and activity can still rule
   someone out. The page
   also said "Nobody has been read yet" when everyone read had been ruled out; it now
   says that only when it is true.

**What does not change.** Tiers stay separate everywhere except the score: the
`evidence.tier` column, the "proven" / "says so" split on the candidate card, and the
§5.3 allowlist. On engineering roles public code still earns `skill_depth` and
`activity_recency` on top. Scoring stays deterministic; `SCORER_VERSION` is
`scoring@2`, and old `scoring@1` rows are kept. Pages read the `current_match` view
(migration 015), the latest row per person per role, so a re-score never lists anyone
twice.

**Known consequence.** With over-experience free, meeting `min_years` alone earns 0.40
under the business profile — exactly the "Good match" cut-off. On a role with
must-haves `NO_MUST_HAVE_FACTOR` stops that; on a role with *only* nice-to-haves, someone
with no matching skill can still read "Good match" on experience alone.

### Explicitly deleted from `Project-Doc.md`

Redis · Celery · Scrapy · React/Next.js · continuous crawl fleet · fuzzy entity-resolution engine · vector search · `current_ctc_band` / `expected_ctc_band` / `notice_period_days` as *scraped* fields (§9.2).

---

## 3. Pipeline

```
JD text --LLM--> role_spec --> recruiter edits and confirms       [HUMAN GATE 1]
                        |
                query plan (per adapter)
                        |
   DISCOVER  linkedin_serp (mode A)            people_search (mode D)
             site:linkedin.com/in via a        logged-in filtered search,
             SERP API. Profile URL +           capped and session-bound
             headline snippet. Touches         (§7.4)
             no LinkedIn surface.
                        |
        candidate_ref = url + name + headline + location
                        |
   SNIPPET GATE  title / employer / region tested against the spec using the
                 SERP snippet ALONE. No page fetched, no cost, no risk.
                 This is what makes the volume arithmetic work.
                        |
   ENRICH    linkedin public_logged_out (mode B) --> attached_browser (mode C)
             Person.experiences[] : position_title, institution_name,
             from_date, to_date, location  ==> seniority is COMPUTED
                        |
   VERIFY    github, gitlab, huggingface, codeforces, serp_web
             run ONLY on refs that survived the gate. Artefact evidence,
             which is the only thing that separates candidates (§2.1).
                        |
   FETCH     httpx + robots.txt + per-host token bucket + content-addressed
             cache. Playwright for LinkedIn modes B/C and text-less pages.
                        |
   EXTRACT   deterministic parsers for JSON APIs; LLM -> strict JSON for prose
                        |
              ===== evidence rows, tiered =====
              linkedin  -> self_reported
              artefacts -> artifact_backed
                        |
   IDENTITY  strong keys only; weak candidates to identity_review  [HUMAN GATE 2]
                        |
   SCORE     hard gates -> allowlisted weighted components -> rationale
                        |
   REVIEW    ranked shortlist -> candidate card, every claim clickable [HUMAN GATE 3]
                        |
   FEEDBACK  actions logged -> weight-change *proposals*           [HUMAN GATE 4]
```

**The snippet gate is the load-bearing addition.** Discovery is cheap and unlimited; enrichment is
expensive and rate-limited. Testing title, employer and region against free SERP metadata before
fetching anything means a role can consider hundreds of profiles while enriching only the few dozen
that could plausibly pass. Without it, the daily caps in §7.4 cap the whole product.

Each stage's input, output, storage, trigger and failure behaviour is specified in §6.

---

## 4. Data model

Postgres 16. Timestamps are `timestamptz`. Ids are `bigint generated always as identity` except `candidate.id` and `role.id`, which are `uuid` because they appear in URLs.

### 4.1 Sources and fetching

```sql
source_policy(
  domain            text primary key,
  adapter           text not null,
  enabled           bool not null default false,   -- kill switch, see 7.4
  requires_login    bool not null default false,
  respect_robots    bool not null default true,
  rate_limit_rps    numeric not null default 0.5,
  daily_fetch_cap   int,                           -- null = uncapped
  tos_note          text,
  reviewed_at       timestamptz,
  disabled_reason   text                           -- set by the auto-disable trip, 7.4
)

fetch(
  id, url text not null, url_hash text not null, domain text not null,
  adapter text not null, http_status int, content_hash text, body_path text,
  bytes int, fetched_at timestamptz not null, from_cache bool not null default false,
  error text
)
create index on fetch (url_hash, fetched_at desc);
```

`source_policy` replaces the doc's `source_compliance_log` and is **load-bearing, not a log**: the fetch layer refuses any request whose domain has no enabled row. Adding a source is a deliberate insert — exactly what B5.1 demanded, made structural instead of procedural.

One row is not a domain: `domain = '*'` is a wildcard that applies **only to the adapter named on that row**, and it exists because `serp_web` fetches domains nobody can enumerate in advance. `_get_policy(domain, adapter)` prefers an exact-domain row and falls back to the wildcard only on an adapter match, so the wildcard grants nothing to any other adapter. `respect_robots` stays true on it, which makes robots.txt the per-host gate. Full reasoning and the mechanisms that survive the exception are in §2.4; the row is migration 012.

### 4.2 People and identity

```sql
candidate(
  id uuid primary key, display_name text, primary_location_text text,
  location_region text,                            -- normalised, see 9.3
  status text not null default 'active',           -- active | archived | erased
  created_at, updated_at
)

identity(
  id, candidate_id uuid not null references candidate on delete cascade,
  kind text not null,     -- github_login | gitlab_login | hf_user | codeforces_handle
                          -- | so_user_id | personal_domain | email_sha256 | linkedin_slug
  value text not null, first_seen, last_seen,
  unique (kind, value)
)

identity_review(
  id, candidate_a uuid, candidate_b uuid, reason text, evidence_json jsonb,
  status text not null default 'open',             -- open | merged | distinct
  decided_by text, decided_at timestamptz
)
```

`unique (kind, value)` is what makes resolution cheap: a strong key belongs to exactly one candidate. Emails are stored **only** as `sha256`, never plaintext — we do not do outreach (§7.3).

### 4.3 Evidence — the spine

```sql
evidence(
  id, candidate_id uuid not null references candidate on delete cascade,
  claim_type text not null,  -- skill | title | employer | location | experience_years
                             -- | education | activity | availability | project | link
  claim_key   text,          -- e.g. canonical skill name when claim_type='skill'
  claim_value text,
  value_num   numeric,       -- populated when the claim is numeric
  tier        text not null,  -- self_reported | third_party_stated | artifact_backed
  source_url  text not null,
  fetch_id    bigint references fetch,
  snippet     text not null, -- verbatim excerpt supporting the claim
  observed_at timestamptz not null,
  extractor   text not null, extractor_version text not null
);
create index on evidence (candidate_id, claim_type);
```

**Rules, enforced in code and tested:**

1. No candidate attribute is stored twice. `candidate.display_name` and `location_region` are caches of the highest-tier evidence; everything else is computed on read.
2. `snippet` is never empty and never paraphrased. If the extractor cannot quote the source, there is no evidence row.
3. Tiers never merge. B5.3 is right, and this is the enforcement: the score weights tiers explicitly (§5.2) and the UI badges them.

**Tier definitions** — the distinction the whole product rests on:

- `artifact_backed` — the person produced a durable thing we can point at: commits in a language, a published package, a model card, a rated contest submission, an accepted answer.
- `third_party_stated` — someone else's page asserts it (company team page, conference speaker bio, SERP snippet).
- `self_reported` — the person asserts it about themselves (profile bio, resume PDF, skills list).

### 4.3a Candidate refs and the snippet gate

The snippet gate (§3) needs somewhere to record what discovery found and what the gate decided,
*before* any page is fetched. Without this table there is nowhere to put a ref that never became a
candidate, and the excluded list cannot show gate-zero rejections.

```sql
candidate_ref(id bigserial primary key,
    role_id uuid not null references role on delete cascade,
    adapter text not null,                    -- linkedin_serp | linkedin_people_search | manual_paste
    ref_kind text not null,                   -- linkedin_url | github_login | url
    ref_value text not null,                  -- normalised; linkedin.com/in/<slug>
    -- what discovery saw, verbatim. This IS evidence-grade text and must stay quotable.
    snippet_name text, snippet_headline text, snippet_location text,
    snippet_raw text not null,                -- the full SERP snippet as returned
    source_url text not null,
    gate_state text not null default 'pending',   -- pending | passed | failed
    gate_reason text,                         -- 'title: no match for senior|lead' etc. NOT NULL when failed
    candidate_id uuid references candidate on delete cascade,  -- set once enriched
    discovered_at timestamptz not null default now(),
    unique (role_id, ref_kind, ref_value))
```

Three things this buys, all of which are load-bearing elsewhere:

- **`unique (role_id, ref_kind, ref_value)`** makes discovery idempotent. Re-running a role's queries
  is then free and safe, which is what lets the worker retry without duplicating people.
- **`gate_reason` not null on failure** is what R5's excluded list renders. A `failed` row with a null
  reason is a bug and there is a test for it.
- **`candidate_id` nullable** is the whole point: a ref that failed the gate never becomes a
  `candidate` row, so we never store a person we did not evaluate. That is data minimisation falling
  out of the schema rather than being remembered.

`snippet_raw` is retained because it is the `source_url`-backed quote for any claim derived from
discovery. It is deleted by the same cascade as everything else.

### 4.4 Skills canon

```sql
skill(id, canonical_name text unique, kind text)   -- language|framework|tool|domain|practice
skill_alias(alias text primary key, skill_id bigint not null references skill)
```

Seeded from a checked-in `data/skills.yaml` so it is reviewable in PRs. `react`, `reactjs`, `react.js` → `React`. This is B5.3's canonical mapping; keep it boring and human-edited. Aliases discovered during extraction go to a `skill_alias_proposal` table for human approval — never auto-inserted, or the canon drifts.

### 4.5 Roles

```sql
role(id uuid primary key, title text, jd_text text, spec_json jsonb not null,
     status text not null default 'draft',          -- draft|open|paused|closed
     created_by text, created_at, updated_at)

role_query(id, role_id uuid references role on delete cascade,
           adapter text, query_text text, run_at timestamptz, results_count int)
```

`spec_json` is the **recruiter-confirmed** structure, not the LLM's raw output:

```json
{
  "must_have_skills": ["Python", "PostgreSQL"],
  "nice_to_have_skills": ["Kubernetes"],
  "seniority": {"min_years": 4, "max_years": 8},
  "locations": ["IN-DL-NCR", "IN-KA-BLR"],
  "remote": "hybrid",
  "domains": ["fintech", "payments"],
  "max_staleness_days": 540,
  "excluded_employers": []
}
```

`role_query` exists so a disappointing search is debuggable — you can see which queries ran and how many refs each returned.

### 4.6 Matching and feedback

```sql
match(id, role_id uuid, candidate_id uuid, score numeric not null,
      components_json jsonb not null,  -- every weighted component, always all five
      gates_json jsonb not null,       -- {"must_have_skills": "pass", "location": "fail: ..."}
      rationale_text text, cited_evidence bigint[],
      scorer_version text not null, weights_version text not null, scored_at timestamptz,
      unique (role_id, candidate_id, scorer_version, weights_version))

recruiter_action(id, match_id bigint references match, action text,
                 -- shortlisted | rejected | contacted | needs_info
                 note text, actor text, created_at)

scoring_weights(id, version text unique, weights_json jsonb, note text,
                approved_by text, approved_at timestamptz, active bool)
```

Storing `scorer_version` + `weights_version` on every match makes a re-score a new row, not a mutation. You can always answer "why was this ranked 3rd last Tuesday".

### 4.7 Jobs and privacy

```sql
job(id, kind text, payload_json jsonb, state text default 'queued',
    -- queued | running | done | failed
    attempts int default 0, locked_at timestamptz, locked_by text,
    last_error text, created_at, finished_at)

erasure_request(id, candidate_id uuid, requested_at, source text, completed_at)
```

Retention: candidates with no `recruiter_action` and no match against an open role for `RETENTION_DAYS` (default 180) are hard-deleted by a nightly job.

---

## 5. Matching and ranking

### 5.1 Hard gates

Evaluated first; a failure excludes the candidate and records the reason in `gates_json`. Gate failures are **visible** behind a "show excluded" toggle — a silently dropped candidate is indistinguishable from a bug.

| Gate | Rule |
|---|---|
| `must_have_skills` | every must-have skill has ≥1 evidence row of any tier |
| `location` | `location_region` ∈ `spec.locations`, or `spec.remote` ∈ {`remote`, `remote_ok_relocate`} |
| `activity` | most recent `artifact_backed` evidence within `spec.max_staleness_days` |

### 5.2 Score components

`score = Σ (weight_i × component_i)`, every `component_i ∈ [0,1]`, all five always present in `components_json` even when zero. Weights live in `scoring_weights`, not in code.

| Component | Definition |
|---|---|
| `skill_match` | coverage of must + nice skills. Every tier counts 1.0 since `scoring@2` (§2.7); was `artifact_backed` 1.0, `third_party_stated` 0.6, `self_reported` 0.35 |
| `skill_depth` | per must-have skill, volume × recency of artifact evidence, log-scaled (a 200-commit repo is not 200× a 1-commit repo) |
| `seniority_fit` | 1.0 at or above `spec.seniority.min_years`, decaying below it; more than `max_years` is not a penalty (§2.7). Years are **computed** from `linkedin_profile.experiences[]` `from_date`/`to_date` spans. Falls back to the old estimate (earliest artefact date) only when enrichment yielded no dates — and the card says which of the two it used. Never parsed from a title string (B5.4) |
| `activity_recency` | exponential decay on the most recent `artifact_backed` evidence |
| `availability` | public availability signal (GitHub `hireable`, profile-README statement). Unknown ⇒ **0**, never negative (B5.8) |

*Until `scoring@2` (§2.7), which removed the discount:* **What the inversion does to `skill_match`, and why it is fine.** The spine is `self_reported`, so
most candidates carry a 0.35 multiplier on most skills and the absolute numbers compress downward.
That is harmless — ranking is relative and every candidate is scaled identically. What matters is
that the *spread* now comes almost entirely from `skill_depth` and from whichever skills the verify
adapters could corroborate. Two candidates with identical LinkedIn profiles separate only on
artefact evidence, which is exactly the intended behaviour and the reason verification is not
optional (§2.1).

**Not scored in v1:** `spec.domains` is captured and domain evidence is *displayed* on the candidate
card, but it is not a score component. Term overlap between project text and a domain label is a
weak, noisy signal; add it as a sixth component only when a recruiter asks for domain-weighted
ranking specifically.

### 5.3 Feature allowlist — fairness enforcement

```python
FEATURE_ALLOWLIST = frozenset({
    "skill_match", "skill_depth", "seniority_fit",
    "activity_recency", "availability",
})

BANNED_SIGNALS = frozenset({
    "college_name", "college_tier", "graduation_year", "age",
    "name_tokens", "photo", "gender", "caste", "religion",
    "marital_status", "nationality", "current_employer_prestige",
})
```

The scorer reads **only** allowlisted keys, and `components_json.keys()` is asserted equal to the allowlist on every write. Adding a feature requires editing the allowlist, which surfaces in code review. This is B5.7's "keep protected attributes out of the inputs, not just the filter list", enforced mechanically rather than by intent.

Also banned as *extraction targets*: we do not extract, infer, or store gender, caste, religion, age, marital status, or photographs. Not "we don't rank on them" — we do not collect them.

### 5.4 Rationale

An LLM writes `rationale_text` from the candidate's evidence rows and computed components. Constraints, validated after generation:

- It must cite evidence ids. `cited_evidence` is parsed out, and any id not belonging to this candidate causes the rationale to be **discarded**. The score is kept — the number never depends on the prose.
- It may not restate the score or invent a component.
- It may not mention anything in `BANNED_SIGNALS`.

### 5.5 Feedback loop

`recruiter_action` rows aggregate into a **weight-change proposal** on an admin page: "shortlisted candidates scored higher on `skill_depth` than rejected ones; suggest +0.05". A human approves, writing a new `scoring_weights` row.

**No online learning and no training on hire outcomes.** B5.7 flags exactly this risk and the original doc then schedules the feature anyway; we don't.

---

## 6. Stage contracts

| Stage | In | Out | Trigger | On failure | Idempotency |
|---|---|---|---|---|---|
| Role parse | `jd_text` | draft `spec_json` | recruiter submits JD | surface raw LLM output for manual editing | re-runnable; never overwrites a confirmed spec |
| Query plan | `spec_json` | `role_query` rows | spec confirmed | adapter with no plan is skipped and logged | deterministic given spec + skill canon |
| Discover | `role_query` | candidate refs | job `discover` | per-adapter; one adapter failing never fails the search | refs deduped on `(kind, value)` |
| Fetch | url | `fetch` row + body on disk | job `collect` | 3 retries with backoff; 4xx not retried; permanent failure recorded | content-addressed cache; same url same day = cache hit |
| Extract | `fetch` body | `evidence` rows | after fetch | quarantine to `extraction_failure`; never partial rows | `(fetch_id, extractor_version)` unique — re-extraction needs a version bump |
| Resolve | evidence + identities | merge or `identity_review` | after extract | ambiguous ⇒ review queue, never auto-merge | merges recorded and reversible |
| Score | evidence + spec + weights | `match` row | job `score`, or weights change | scoring is pure; a failure is a bug, not a retry | keyed on `(role, candidate, scorer_v, weights_v)` |

**Reprocessing:** raw bodies are kept, so a better extractor re-runs over cached fetches with no new network traffic. That is the main reason the fetch cache exists.

---

## 7. Sourcing

### 7.1 Adapter interface

```python
class Adapter(Protocol):
    name: str
    default_tier: Tier
    def plan(self, spec: RoleSpec) -> list[Query]: ...
    def discover(self, q: Query) -> Iterable[CandidateRef]: ...
    def collect(self, ref: CandidateRef) -> list[RawDoc]: ...
```

Every source is one of these. No source gets special treatment in the pipeline; this is what makes "drop it if it doesn't add value" a config change rather than a refactor.

### 7.2 Adapters

Three roles now, and an adapter's role matters more than its name. **Discover** produces
`candidate_ref`s from a spec. **Enrich** turns a ref into employment history. **Verify** tests the
claims enrichment produced against something the person actually built.

| Adapter | Role | Access | Tier | Notes |
|---|---|---|---|---|
| `linkedin_serp` | **discover** | SERP API, `site:linkedin.com/in/` | `third_party_stated` | The spine's front door. Returns profile URL plus a headline snippet carrying name, current title, employer and city. **Touches no LinkedIn surface** — no authwall, no account, no ban risk. ~Rs 20/role. Feeds the snippet gate (§3) |
| `linkedin_people_search` | **discover** | mode D, §7.4 | `third_party_stated` | Best recall, highest risk, and **not implemented by `linkedin_scraper/`** — this is new code against the surface LinkedIn polices hardest. Session-bound to mode C, shares its caps |
| `linkedin_profile` | **enrich** | modes B then C, §7.4 | `self_reported` | `Person.experiences[]` gives `position_title`, `institution_name`, `from_date`, `to_date`, `location`. This is what makes `seniority_fit` computed. Runs only on refs that survived the snippet gate |
| `github` | **verify** | official REST/GraphQL API | `artifact_backed` | Was the spine, now the verifier — which is what it was always good at. 5,000 req/hr authenticated; **search 30/min, code search 10/min**. Language volume, commit recency, contribution graph |
| `serp_web` | **verify** | search API then **our** fetch + parse, under the §2.4 wildcard policy row | `third_party_stated` | **Built 2026-08-28.** Company team pages, conference programmes, published interviews. The only source of independent corroboration for an *employer* or *title* claim, which artefacts cannot give — and therefore the only route above `self_reported` for a non-technical candidate. Deterministic parse, no LLM: the name and the employer must appear in the **same block** of page text, and the quoted window is that block. Aggregators, self-published platforms and people-data brokers are blocklisted in code. Stops spending on a candidate the moment one page corroborates them |
| `huggingface` | **verify** | public API | `artifact_backed` | Primary verifier for ML/DS roles |
| `codeforces` | **verify** | public API | `artifact_backed` | Algorithmic ability, mostly junior-end |
| `gitlab` | **verify** | public API | `artifact_backed` | Same shape as `github`, thinner in India |
| `stackexchange` | **verify** | public API 2.3 | mixed | Tag reputation is `artifact_backed`; bio is `self_reported`. Declining post-LLM; keep, measure, expect to drop |
| `manual_paste` | discover + enrich | recruiter pastes URL/text | `self_reported` | Zero risk, immediately useful, and the fallback for every blocked source. Also how a Recruiter-Lite seat feeds the tool if one is bought |

**The invariant that keeps this honest:** a candidate whose evidence is *entirely* `self_reported` is
a LinkedIn search result, not a product output. `skill_match` ranks them accordingly, and the
candidate card shows a column of "Says so" badges with nothing beside them. That is the correct and
visible outcome, not a bug to paper over.

### 7.3 Fetch discipline (all scraping adapters)

- `robots.txt` honoured; a disallow is a hard skip, cached per domain for 24h.
- Descriptive `User-Agent` with a contact address. We do not pretend to be a browser on undefended targets.
- Per-host token bucket at `source_policy.rate_limit_rps` (default 0.5 rps), plus jitter.
- Content-addressed on-disk cache; a URL is not refetched within 24h.
- Playwright is invoked **only** when a fetched page yields no extractable text. One shared browser, lazily started.
- **No** residential proxies, **no** captcha solving, **no** TLS/fingerprint spoofing. A target that needs those is a signal to drop the target, not to escalate. **One scoped exception exists** — LinkedIn mode E, §7.5, authorised as a contingency by §2.1, not built, trigger-gated. It does not relax this rule for any other adapter, and captcha *solving* stays banned even there.
- Emails are hashed at extraction and never stored in plaintext. GitHub's Acceptable Use Policy forbids using its data "for the purposes of sending unsolicited emails to users"; we identify people, and the recruiter contacts them through a channel the person opened.

### 7.4 LinkedIn — the four modes

The spine. Four modes with genuinely different risk profiles, and they are **not** a ladder to climb
for its own sake: A always runs, B is tried before C, and D is only worth its risk once A is
demonstrably starving the search.

**Mode A — `serp_only` (discovery, always on).** `site:linkedin.com/in/` queries against a SERP API.
Returns profile URL, name, headline (title + employer) and location straight from snippet metadata.
No page fetched, no authwall touched, no account to restrict. The only mode with **zero**
LinkedIn-side risk, and it supplies everything the snippet gate needs. Tier `third_party_stated`.

**Mode B — `public_logged_out` (enrichment, tried first).** Fetch the logged-out public version of a
profile URL: no session, no cookies, no account. Materially less data than a logged-in view —
experience sections are often truncated and dates frequently absent — but **nothing can be
restricted because nothing is authenticated.**

> **Mode B is the go/no-go experiment for the whole plan.** The question is what fraction of Indian
> engineer profiles return usable `experiences[]` *with dates* when logged out. High, and the product
> ships with no account risk at all. Low, and mode C is required and the risk conversation is real.
> Measure it on ~50 profiles before writing the adapter — a day's work that decides the
> architecture. Nothing below C should be built until that number exists.

**Mode C — `attached_browser` (enrichment, only if B is too thin).** CDP-attach to the operator's
**own, already-logged-in** browser: real profile, real fingerprint, visible window.

| Limit | Default |
|---|---|
| profiles per day per account | 30 (raised from 15 on 2026-08-27, see below) |
| concurrency | 1 |
| delay between profiles | at least 20s + jitter |
| headless | never |
| burner, fabricated, shared, or stored-credential account | **never** |
| on first authwall, challenge, or HTTP 999 | abort run, set `source_policy.enabled=false` and `disabled_reason`, require a human to re-enable |

**Daily cap raised 15 → 30 on 2026-08-27**, at the tool owner's request, on the owner's risk. The
reason offered was that the operator routinely sends ~30 connection invitations a day without
trouble. Recorded with the caveat that this is *not* equivalent evidence: invitations and profile
views are separately limited, and the non-recruiter commercial-use limit is monthly, so 30/day
(~900/month) is closer to it than 15/day was. The count was never the main risk driver anyway —
timing regularity is, which is what the ≥20s + jitter and concurrency 1 address, and those are
unchanged. It remains a hard ceiling in code: a run stops at it, and it is not a target to reach.
Lower it again the moment a challenge appears.

**Browser separation is fine; identity separation is not.** Running automation in a second browser
(Edge) while personal browsing stays in the primary (Brave) is good hygiene and is encouraged — it
isolates the automated session's cookie jar and extensions. Running it under a *fabricated account*
is refused, for three reasons that are practical rather than moral:

1. A fake-account fleet is the aggravating fact in every LinkedIn enforcement action to date;
   Proxycurl was destroyed for it. It converts "read public pages" into "breached the User Agreement
   under a false identity" — the fact pattern that draws a suit rather than a block.
2. **It returns less data.** LinkedIn gates profile depth on connection degree and account
   age/activity. A low-connection account sees truncated experience — precisely the `from_date` and
   `to_date` fields this plan depends on — and reaches the commercial-use limit wall quickly.
3. **On one laptop it contaminates the real account.** Both browsers share an egress IP and much of
   the device signal. Burning the fabricated account trains LinkedIn's trust systems on the IP where
   the operator's genuine account also lives. Using a burner to shield a real account is backwards
   on a single machine.

The residual risk in mode C is real and lands on a person: **that individual's account may be
restricted.** Whoever's account is used must agree to that explicitly and in advance.
`.env`-stored `LINKEDIN_EMAIL` / `LINKEDIN_PASSWORD` — how `linkedin_scraper/core/auth.py` works
today — is **not** this mode, and must be replaced by a CDP attach before mode C ships.

**Mode D — `people_search` (discovery, highest risk).** Automating LinkedIn's own logged-in people
search. Best recall, and real filters including years-of-experience that mode A cannot express. Two
things to hold onto: `linkedin_scraper/` does **not** implement this (it ships a *job* search), so it
is new code; and search is the most heavily policed surface on the site, so it consumes mode C's
session and caps rather than getting its own. Do not build it until `role_query.results_count` shows
mode A is genuinely starving the search — the same evidence standard §7.5.1 demands.

**Every mode's output is flagged outside the DPDP §3(c)(ii) exemption** (§9.5), mode A included.

### 7.5 `proxied_public` — the escalation path (mode E, NOT BUILT)

**Relationship to mode B, now that B exists.** Mode E is simply mode B — logged-out public profile
fetching — *plus* a residential proxy pool and fingerprint rotation. It is therefore the escalation
for exactly one failure: mode B works on the data but gets IP-blocked at the volume we need. If B
fails because the *data* is too thin logged out, E does not help at all and mode C is the answer.
Diagnose which of the two failed before reaching for this.

Authorised as a contingency by the §2.1 decision. **Do not build this until §7.5.1's trigger fires
and an explicit go decision is recorded here with a date.** Until then it is a plan, not a feature.

#### 7.5.1 Trigger — what "the current plan didn't work" has to mean

Measured, not felt. All three must hold after **M1 and M2 are both shipped and have run 30 days**:

1. Median **contact rate** over the trailing 10 roles is **< 20%** of top 20 (PRD §7 target is 40%).
2. Median shortlist size after gates is **< 10 candidates** per role.
3. LinkedIn modes A–C have been **live for 30 days** and their adapter yield is
   materially better than the artefact adapters — i.e. LinkedIn is demonstrably where the missing
   candidates are, rather than the shortlists being thin for some other reason.

Condition 3 exists because the likeliest cause of a thin shortlist is a bad `role_spec`, an
over-tight location gate, or a starved query plan. **Escalating source access to fix a scoring bug
is the expensive way to not fix a scoring bug.** Check the gate-failure histogram first; it is on the
admin page precisely for this.

#### 7.5.2 Design — logged-out, never account-attached

**The recruiter's account and the proxy pool must never appear in the same request.** This is the
one hard technical constraint in this section, and it is not a compliance point — it is a detection
point. LinkedIn's trust systems weight account/IP/geography coherence heavily; a session that has
logged in from Gurgaon for three years, suddenly arriving over a residential IP in another city with
a rotated fingerprint, is a **stronger** restriction signal than either factor alone. Combining
modes 3 and 4 gets you the legal exposure of one and the account risk of the other.

So the two paths are mutually exclusive, and choosing this one means turning the other off:

| | mode C `attached_browser` | mode E `proxied_public` |
|---|---|---|
| Account | recruiter's real, logged in | **none — logged out** |
| IP | recruiter's real | residential pool |
| Fingerprint | real | rotated |
| Data depth | full profile | public view only — significantly thinner |
| Risk lands on | **an individual's account** | the company, as contract exposure |
| DPDP §3(c)(ii) | outside it | outside it |

Mode E's one genuine advantage over mode C: **no individual's personal account is put at risk.**
§7.4 flagged that as the residual risk that lands on a person. This removes it and substitutes
corporate risk, which is a defensible trade for an internal tool — and is the strongest argument
for mode E over mode C, stronger than any of the throughput ones.

`source_policy.mode` is single-valued. Enabling `proxied_public` **sets mode C to disabled in the
same transaction.** Enforced by a check in code, not by convention.

#### 7.5.3 Caps — in code, not config

| Limit | Value |
|---|---|
| profiles per role | **50** |
| profiles per calendar month, all roles | **300** |
| concurrency | 1 |
| delay between profiles | ≥20s + jitter |
| logged-in requests | **zero — assert no auth cookie on every request** |
| stored LinkedIn credentials | never, in any mode |
| on captcha, challenge, or HTTP 999 | abort run, `enabled=false` + `disabled_reason`, human re-enable |

The 300/month ceiling is the one to argue about: 50/role × 30 roles is 1,500/month, which is no
longer a bounded experiment. If the real need is 50 per role across every role, that is a different
decision from the one recorded in §2.1 and needs its own entry here.

**Still banned at every volume:** captcha *solving* (detection is a stop signal, not an obstacle),
any fake or burner LinkedIn account, and purchased profile datasets (§9.5 — an import voids the
exemption on the whole record, which no proxy configuration can undo).

#### 7.5.4 Kill criterion

Same rule as every other adapter, and it is the point of putting caps on a contingency: if mode E
does not lift contact rate above 30% within 30 days of going live, it is not earning its risk.
Disable it and accept that coverage is the constraint (§9.1).

---

## 8. Observability

One `pipeline_event` table (`role_id, stage, adapter, level, message, meta_json, at`) and one admin page. What must be answerable without a debugger:

- Where did this candidate come from, and what did each adapter contribute? (adapter yield)
- Why is this shortlist small? (gate-failure counts per gate)
- Which adapter is failing, and since when? (error rate per adapter)
- What did this role search cost? (fetch count, LLM tokens, SERP queries per role)
- Which sources produce candidates recruiters actually shortlist? (§7.4's kill criterion)

---

## 9. Known limitations — say these out loud

**9.1 Coverage is now a function of discovery, not of publishing.** Superseded by the §2.1 reversal: with LinkedIn as the spine, reach is no longer limited to engineers who publish code. What limits it now is what a `site:linkedin.com/in/` query can express — mode A cannot filter on years-of-experience, only on text that appears in a headline. Expect good recall on title/employer/city and poor recall on anything a headline does not state. Mode D exists for that gap and has not earned its risk yet.

**9.1a The verified subset is much smaller than the discovered set, and that is the real narrowness.** We can discover a thousand plausible people and corroborate artefact evidence for a few dozen. Candidates with no public artefacts still rank — on `self_reported` evidence alone, visibly so — but the product's distinguishing claim only applies to the subset we could verify. Set that expectation before the first demo, in those terms.

**9.2 Comp and notice period are not discoverable.** `current_ctc`, `expected_ctc` and `notice_period` exist only in paid resume databases and recruiter conversations. They are recruiter-entered, post-contact fields with tier `recruiter_input`, excluded from discovery and from ranking. Bands, never point values (B5.6 is right).

**9.3 Location is a region set, not a string** — and enrichment makes it far denser than the GitHub-era plan assumed, since LinkedIn location is populated for nearly everyone. `IN-DL-NCR` covers Delhi / Gurgaon / Noida / Faridabad / Ghaziabad — the doc's NCR problem, fixed by making the canonical unit a region with a member list in `data/regions.yaml`. `remote` is a four-value enum, never a boolean (B5.5).

**9.4 Seniority — largely retired by the §2.1 reversal.** With `experiences[]` dates, years are computed from stated employment spans rather than inferred from publishing history, so the systematic downward skew is gone. Two residues remain. Stated spans are `self_reported` and people round them generously, so the number is a *claim*, not a measurement, and the card must show it as one. And when mode B returns no dates, the old artefact-derived floor is the fallback and the old skew applies to those candidates — so the card states which method produced the number.

**9.4b Mode B's yield is the largest unknown in the plan.** Everything above assumes logged-out profiles carry dated experience. That is untested. §7.4 makes measuring it the prerequisite to writing the adapter.

**9.4a `availability` may prove to be dead weight.** GitHub's `hireable` flag is rarely set and often stale. Measure its distribution during Milestone 1; if it is ~0 for most candidates it is contributing noise and a false sense of freshness, and the component should be dropped.

**9.4c The spine can be switched off by someone else.** GitHub cannot restrict us; LinkedIn can. A
block, an authwall change, or a markup change breaks discovery or enrichment with no warning and no
appeal, and unlike every artefact adapter there is no substitute for it. Mitigations are ordered:
mode A degrades to `manual_paste` and a Recruiter-Lite seat; the on-disk cache means an in-flight
role survives a mid-run block; `source_policy` disables the adapter rather than failing the
pipeline. Accept that a bad week for the adapter is a bad week for the product.

**9.5 The DPDP position no longer rests on the exemption for the primary source.** §3(c)(ii) excludes personal data the data principal made publicly available themselves — which still covers the *verify* adapters (GitHub profiles, personal sites, self-published resumes) processed in the form published. It does **not** cover the spine: LinkedIn data is published to LinkedIn under LinkedIn's terms, not to the world by the person. Since the spine is now primary, treat the default record as in-scope and stop treating the exemption as load-bearing. Three caveats that constrain the build:

- It is not a general "public internet" exemption. Crawlable ≠ exempt. Data behind a login, or from a third-party aggregated dataset, does **not** qualify — which includes **every** LinkedIn mode's output, A included. Treat that evidence as in-scope. Note that modes B and E are logged-*out*, which does not rescue them: the profile is published to LinkedIn under LinkedIn's terms, not published by the person at large, and the access route is disputed. Flag it in-scope and move on.
- **Combining public data with non-public data to build profiles can pull the whole record back into scope.** This is the strongest technical argument against ever importing a purchased enrichment file.
- DPDP Rules 2025 were notified 14 Nov 2025, so this is live law with phased obligations.

We honour erasure requests regardless of exemption, retain for 180 days by default, and keep a per-claim provenance trail. Cheap, and the right posture.

**9.6 This is not legal advice.** B5.1's call for a named sign-off before sources are finalised stands. `source_policy.reviewed_at` is the field that records that it happened.

---

## 9a. Operability and hand-off — the constraint that outranks features

**Stated premise, recorded 2026-08-25.** The author builds this, delivers it, and is then not
associated with it. The receiving company is an early-stage startup whose internal team will operate
it without a maintainer. That is not a footnote — it is a harder constraint than any feature in this
document, and the most likely way this project fails is not a bad shortlist. It is returning zero
rows six weeks after hand-off with nobody able to say why.

Everything in this section is **M1 scope**, not M4. Deferring it to "operations" is how it does not
get built.

### 9a.1 What breaks, and when

Ranked by probability, not severity.

| What | When | Blast radius |
|---|---|---|
| **LinkedIn changes profile markup** | Weeks to months. Assume it. | Mode B enrichment returns empty `experiences[]`. **Silent** unless detected — this is the dangerous one. |
| SERP API key expires, hits quota, or the card declines | Months | Discovery returns nothing. Loud if surfaced, invisible if not. |
| GitHub token expires (classic PATs do) | 90 days–1 year | Verification silently stops; every candidate becomes unverified. |
| Chromium/Playwright drift after an OS update | Months | Mode B fails to launch at all. |
| Postgres disk fills (cached page bodies grow) | 6–12 months | Writes fail. Everything stops. |
| LinkedIn IP-throttles the office egress | Unpredictable | Mode B degrades or dies. |

### 9a.2 The degraded mode is the survival strategy

This is the most important design consequence of the §3 pipeline, and it is worth stating loudly:

> **If enrichment dies completely, the product still works.** Mode A's snippet already carries name,
> current title, employer and city. That is enough to run the `location` gate, the `must_have_skills`
> gate against headline text, and GitHub verification. What is lost is computed seniority and
> employment history — real losses, but the tool still returns a ranked, evidence-backed,
> verified-where-possible shortlist.

So the ladder is: **A+B+verify** (full) → **A+verify** (degraded, still useful) → **`manual_paste`
+ verify** (manual, still better than a spreadsheet). Each rung is a `source_policy` row flip, not a
code change. Nothing in the pipeline may treat a missing enrichment as a fatal error, and there is a
test that runs a full role with `linkedin_profile` disabled and asserts a non-empty shortlist.

### 9a.3 Built in M1, because a maintainer will not add it later

1. **An adapter failure never fails a role.** A stage that raises marks the adapter unhealthy,
   records `disabled_reason`, and the role completes with whatever evidence exists. A role that
   returns 12 candidates with a visible "enrichment unavailable" banner is a working product; a role
   that 500s is not.
2. **`/admin/health` — readable by a non-engineer.** Per adapter, in plain words: last successful
   run, last error and when, refs found in the last three runs, and a red/amber/green light. The
   silent-failure modes above are only survivable if they are visible on one page that somebody
   checks. **A green light must mean "produced rows recently", not "did not throw".**
3. **Every LinkedIn selector in one file.** `src/hi/adapters/linkedin_selectors.py`, nothing else.
   This does not let a non-engineer fix a markup change, but it lets a contractor fix it in an hour
   instead of a day, and it makes the fix a one-file diff that is obvious in review.
4. **A canary test with a committed fixture.** One recorded profile fixture, and a test asserting
   `experiences[]` parses with dates. When LinkedIn changes markup the *fixture* still passes — so
   also ship `scripts/refresh_fixture.py`, which re-records that one profile from live and fails
   loudly when the new HTML no longer parses. That script is the whole diagnosis: run it, read the
   error, fix the one selector file.
5. **Nightly `pg_dump` to a second location.** Losing the database loses the recruiter's decision
   history *and* the per-claim provenance trail that §9.5 relies on. Two lines of cron.
6. **A disk-usage guard on the page cache.** The content-addressed cache is the thing that grows.
   Cap it, evict oldest, and alarm on the health page at 80%.
7. **All secrets in `.env`, none in code, and `.env.example` lists every one with what it is for and
   where to get a new one.** Key rotation must be a config edit by someone who has never seen the
   codebase.
8. **`RUNBOOK.md`, written for a competent generalist rather than a maintainer.** Symptom → check →
   fix, in that shape. Minimum entries: no candidates found; enrichment empty; adapter shows red;
   "it worked last month"; how to rotate each key; how to restore a backup; how to turn LinkedIn off
   entirely and keep working.

### 9a.4 What we deliberately do not do for durability

- **No retry-until-it-works loops against LinkedIn.** A failing adapter disables itself and waits for
  a human. Hammering a hostile target unattended is how a soft block becomes a hard one.
- **No auto-updating selectors, no "self-healing" LLM parser.** An LLM that re-derives selectors when
  parsing fails sounds like durability and is actually an unbounded token bill plus silent
  fabrication. When markup changes, the correct behaviour is to stop and go red.
- **No second server, no HA, no Kubernetes.** One VM, restartable, with a backup. At 30 roles a month
  the correct answer to a crash is "start it again", and a team with no engineer cannot operate a
  cluster.

### 9a.5 Definition of ready to hand over

The build is not done when the shortlist is good. It is done when all of these are true:

- [ ] A recruiter who has never seen it runs a real role start to finish with no help, from
      `README.md` alone.
- [ ] The full role completes with `linkedin_profile` disabled, and the UI says why (§9a.2).
- [ ] `/admin/health` shows a red light within one run of a broken adapter, and green means rows.
- [ ] `RUNBOOK.md` covers all eight symptoms in §9a.3.8, and someone other than the author has
      followed one entry successfully.
- [ ] Every secret is in `.env.example` with a rotation note. No key is in git history.
- [ ] `pytest` is green with no network, and `scripts/refresh_fixture.py` is documented in the
      runbook as the first diagnostic step.
- [ ] Backup has been restored once, to prove it works.
- [ ] A named person at the receiving company owns `source_policy.reviewed_at` and knows what
      enabling a row means. **The DPDP obligations transfer with the tool** — §9.5 sits with them
      once the author leaves, and they need to know that before it is switched on, not after.

---

## 10. Stack

Python 3.13 · FastAPI · Postgres 16 · httpx · selectolax · Playwright (lazy) · Jinja2 + HTMX · `anthropic` SDK · pydantic v2 · pytest.

One Docker Compose, one VM. The worker is a second process against the same `job` table.

**Model routing:** one `llm.py`, so routing is a single edit.

| Call-site | Model | Volume | Why |
|---|---|---|---|
| JD → `role_spec` | `claude-haiku-4-5` | ~30/month | Schema-filling from one short document. A stronger model is affordable here but not needed; the recruiter confirms every field. |
| Free-text page → evidence (M2) | `claude-haiku-4-5` | ~300/role | High volume, schema-constrained, temperature 0. **Do not upgrade the model to fix a prompt problem.** |
| Rationale prose (M2) | `claude-haiku-4-5` | ~40/role | Prose a human reads, and it is discarded if it cites evidence the candidate does not own (§5.4). |

Three cost levers, applied in this order:

1. **The scraper returns structured data, so M1 spends nothing on candidate text.** `Person` is a
   pydantic model; `experiences[]` is a parse, not an inference. Spending a token to read JSON we
   have already parsed is waste, and it is also how candidate personal data ends up in a model
   provider's logs. Both problems solved by not making the call.
2. **Batch API for extraction** (`client.messages.batches.create`) — **50% cheaper**, and a role
   search is already an asynchronous background job, so latency is free to trade.
3. **Prompt caching on the extraction prompt** — the schema and instructions are a stable prefix, so
   cache them and pay ~0.1x on the repeated portion. Verify with
   `usage.cache_read_input_tokens`; if it is zero, something volatile leaked into the prefix.

**Zero-spend variant, if it is wanted:** JD parsing is the only M1 call-site and it touches no
personal data (it is a job description — company text), so a free tier whose terms permit training
on inputs is *tolerable there and only there*. M2 extraction reads candidate bios and must not go to
a free training tier; run it on local Ollama (7–8B is sufficient for schema-filling) or on the paid
Haiku path above. This is the one place where the cheap option and the correct option genuinely
diverge, and the split above is what keeps both.

**Cost per role search.** Two figures, because M1 and M2 differ by an order of magnitude and quoting
only the second one has misled this project once already.

| | M1 (spine + GitHub verify) | M2 (+ free-text extraction) |
|---|---|---|
| SERP queries (discovery) | ~200 → **₹20** ($0.24) | same |
| LinkedIn fetches (mode B) | free | same |
| GitHub API | free | free |
| LLM | JD parse only → **≈₹1** | ~300 extractions, Haiku + batch + cache → **₹25–40** |
| **Per role** | **≈ ₹25 ($0.30)** | **≈ ₹60 ($0.75)** |
| **Per month at 30 roles** | **≈ ₹750** | **≈ ₹1,800** |

Plus one VM at roughly ₹2,000/month. Compare one Naukri Resdex seat at ₹55,000+/quarter. The
previous ≈$2–4 estimate assumed GPT-class extraction over 300 pages with no batching and no caching;
it is superseded.

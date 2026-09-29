# Hiring Intelligence — Product Requirements

**Status:** approved for build · **Owner:** ayushmaan · **Last updated:** 2026-08-25
**Companion docs:** `docs/ARCHITECTURE.md` (how it's built) · `docs/IMPLEMENTATION.md` (build order) · `AGENTS.md` (LLM boundaries)

This document says *what* we are building and *how we will know it worked*. It does not restate the
architecture. Where it appears to conflict with `ARCHITECTURE.md` on a technical point, the
architecture doc wins.

**How to read this.** If you hire people, read §1–§7 and §13 — no technical knowledge is assumed,
and §4 and §5 are the two you should argue with. If you build it, read all of it, then
`ARCHITECTURE.md`. Anything in `code font` is a name from the system, included so that a recruiter
and an engineer talking about the same thing use the same word.

---

## 1. Problem

A recruiter opening a new engineering role today does one of three things, all bad:

1. **Keyword-searches LinkedIn.** Returns thousands of profiles ranked by LinkedIn's interests, not
   ours, with no way to tell a claim from a fact — "Python" on a profile and forty Python repos look
   identical. Note carefully what the objection is: **not that LinkedIn's data is bad, but that
   LinkedIn's *ranking* is not ours and its claims are unverified.** We use the data and replace both
   of those (§4). This is the distinction the rest of the document turns on.
2. **Buys a resume database.** Stale, paid per seat, and the data is third-party aggregated — which
   in India puts it squarely *inside* DPDP scope with no self-publication exemption to lean on.
3. **Asks an engineer to eyeball GitHub.** Actually produces good candidates. Takes half a day per
   role and does not scale past one engineer's patience.

The gap: nobody ranks candidates by **what they demonstrably built**, and nobody shows the recruiter
*why* a person is ranked where they are.

## 2. Who this is for

| User | Count | What they do with it |
|---|---|---|
| **Recruiter** (primary) | 3–5 | Pastes a JD, confirms the parsed spec, reads a ranked shortlist, marks candidates to contact. Non-technical: cannot read a GitHub profile and judge it. |
| **Hiring manager** (secondary) | occasional | Opens a shared shortlist, sanity-checks the top 10, disagrees loudly and usefully. |
| **Engineer/admin** (you) | 1 | Enables sources, approves skill aliases, adjusts weights, reads the observability page. |

Volume: **~30 role searches a month.** This number is the single most important constraint in the
document — it is why there is no Redis, no queue broker, no SPA, and one VM.

## 3. What it does

**Role requirements in → ranked, evidence-backed shortlist out.**

Engineering, data and ML roles first. India-primary. Every ranked candidate arrives with the sources
that justify the ranking, quotable line by line.

### The one thing that makes it different

Three evidence tiers that **never merge**:

| Tier | Plain-language label | What lands here |
|---|---|---|
| `artifact_backed` | **Proven** | Code, published packages, model cards, rated contest submissions. A durable thing you can open and read. |
| `third_party_stated` | **Third party** | Someone with no stake in the person says it: a conference programme selected them to speak, a project granted them commit rights, an employer's own team page lists them. |
| `self_reported` | **Says so** | They say it about themselves — a profile field, a personal site, a resume PDF. |

"Lists Python on a profile" and "has 40 Python repos with commits this quarter" are different facts.
They stay visibly different from the database row all the way to the recruiter's screen, in
different-coloured badges. Every LinkedIn-shaped product in this space collapses them into one
confidence number. That collapse is the product problem we exist to fix.

The tiers are not a display convention — they carry different arithmetic weight in the ranking
(§5.3), and they determine which legal footing a claim sits on (§10, `ARCHITECTURE.md` §9.5).

**This matters more since the source reversal, not less.** The spine is LinkedIn, and every LinkedIn
claim is `self_reported` — a profile is a pile of unverified assertions, however well formatted. Two
candidates with identical profiles are indistinguishable until something corroborates one of them.
So the tiers are not garnish on top of the ranking; they *are* the ranking's ability to tell people
apart. A build that ships discovery and enrichment without verification has produced a LinkedIn
search with a slower UI.

---

## 4. Where the data comes from


**A JD screens on employment history.** Years in role, current employer, title progression, domain,
city. GitHub answers none of those — it is a *skills* source being asked to act as a *people* source.
The old plan's three biggest admitted weaknesses (single-digit city coverage, sparse location,
seniority skewing low) were not three problems; they were one problem wearing three hats. All three
dissolve when the spine is a profile source.

So: **LinkedIn discovers and describes; artefacts verify.** The decision record is
`ARCHITECTURE.md` §2.1.

### 4.1 The three jobs, and which source does which

| Job | Question it answers | Source |
|---|---|---|
| **Discover** | Who should we even consider? | LinkedIn, via `site:linkedin.com/in/` search |
| **Enrich** | Do they meet the JD's stated requirements? | The LinkedIn profile — `experiences[]` with employers, titles, dates |
| **Verify** | Is any of it actually true? | GitHub, packages, Hugging Face, conference lists, portfolios |

The third job is the product. The first two on their own are LinkedIn search with extra steps and a
worse UI. Anyone can open a profile and read a claim; the thing worth building is the layer that says
*this claim checks out and that one does not.*

### 4.2 What LinkedIn gives us that nothing else does

The scraper already in the repo returns an `Experience` record per role: `position_title`,
`institution_name`, `from_date`, `to_date`, `duration`, `location`. Against the old plan:

| JD requirement | GitHub-first (old) | LinkedIn spine (now) |
|---|---|---|
| 5-8 years experience | estimated floor from first public commit — near-useless | computed from stated employment dates |
| Currently at a product company, or in fintech | not discoverable at all | current employer, stated |
| Senior, or has led a team | title parsing is banned, so: nothing | title progression, visible |
| Based in Bangalore | sparse and stale profile field | populated for nearly everyone |
| Reach across a city | single-digit percentage | near-total |

### 4.3 How we get at it — four modes, ascending risk

Hard caps in `ARCHITECTURE.md` §7.4. Summarised here because the risk profiles differ so sharply
that treating "LinkedIn" as a single decision would be a mistake.

| Mode | What it does | Risk | Status |
|---|---|---|---|
| **A · `serp_only`** | `site:linkedin.com/in/` via a search API. Returns profile URL plus a snippet carrying name, current title, employer, city. **No page fetched, no account involved.**  | **None.** No LinkedIn surface is touched. | Always on. The discovery engine. |
| **B · `public_logged_out`** | Fetch the logged-out public version of a profile. No session, no cookies, no login. | **None to any account.** Can be IP-throttled. | **Tried first for enrichment. Unproven — see §13.1.** |
| **C · `attached_browser`** | Attach to the operator's own already-logged-in browser. Real profile, visible window, at least 20s between profiles, 15/day, self-disables on the first challenge. | **Real, and it lands on a person** — that individual's account may be restricted. | Only if B proves too thin. |
| **D · `people_search`** | Automate LinkedIn's own logged-in people search. Real filters, including years-of-experience that mode A cannot express. | **Highest.** The surface LinkedIn polices hardest. | Not built. Only once mode A is demonstrably starving the search. |

Two practical facts shape all of this:

- **The scraper we have does not discover anyone.** It ships `Person(url)` and a *job* search — there
  is no people-search in it. Mode A is therefore not a nicety, it is the only discovery route that
  exists today, and mode D would be new code against the worst possible target.
- **The snippet gate is what makes the arithmetic work.** Mode A is cheap and effectively unlimited;
  enrichment is capped at 15/day in mode C. Testing title, employer and city against the free
  snippet *before* fetching anything lets a role consider several hundred profiles while enriching
  only the thirty or forty that could plausibly pass. Without it, the daily cap caps the product.

### 4.4 Verification — the layer that makes it a product

Runs only on candidates that survived the snippet gate, so volume is small and cost is low.

| Source | What it corroborates | Tier |
|---|---|---|
| `github` | Whether a claimed language is real, how much, how recently. "Says 8 years of Python" against "has 8 years of public Python commits". | `artifact_backed` |
| Package registries — PyPI, npm, Maven, crates.io | Strangers depend on their code. Strongest single signal available. **Still not an adapter — see §13.7.** | `artifact_backed` |
| `huggingface` | Published models and datasets. Primary verifier for ML/DS roles. | `artifact_backed` |
| `codeforces` | Algorithmic ability, mostly at the junior end. | `artifact_backed` |
| `serp_web` — conference speakers, `MAINTAINERS` files, team pages | Independent corroboration of an *employer* or *seniority* claim, which artefacts cannot give. The only third-party check on "led a team". | `third_party_stated` |
| `gitlab`, `stackexchange` | Thinner. Measure; expect to drop `stackexchange`. | mixed |

### 4.5 Sources considered and rejected

Unchanged by the reversal, and easier to justify now: with LinkedIn as the spine, most of what these
offered was coverage we already have.

| Source | Verdict | Why |
|---|---|---|
| **Kaggle** | Likely yes, data roles | Real verification signal for DS. Weak on engineering ability. |
| **Google Scholar / arXiv / DBLP** | Yes, if we hire researchers | Correct primary verifier for research-ML. Useless otherwise. |
| **GSoC / Outreachy alumni** | Probably yes | Cheap, public, India-heavy, strong for junior hiring. |
| **Naukri Resdex, Instahyre, Cutshort** | **No** | Around ₹55,000/quarter per seat, third-party aggregated so no self-publication exemption, and no verification layer either. LinkedIn gives better coverage for less. |
| **Bought enrichment files** — Apollo, Lusha, ZoomInfo | **Never** | Combining bought data with self-published data can pull the *entire* record out of the §3(c)(ii) exemption. One import contaminates the database, and deleting the file does not undo it. |
| **LeetCode** | **No** | Mostly private, gamified, measures interview prep. `codeforces` does it honestly. |
| **X, Reddit, Discord** | **No** | Inferring things about people from their opinions is what §4.6 promises not to do. |
| **Wellfound / AngelList** | By hand only | Candidate-consented and high-intent, but access needs an employer login and the terms forbid automation. |
| **A Recruiter Lite seat** (~₹8-12k/month) | **Worth pricing seriously** | The legitimate version of mode D: real filters, no account risk, exports. Feeds the tool through `manual_paste` and composes with mode A. If B fails and C is unpalatable, this is the answer. |
| **Our own past applicants and referrals** | Out of scope | Highest-yield pool the team already owns. This tool does not touch it and is not a substitute for working it. |

### 4.6 What we never collect, from any source

Not "collect but do not rank" — **do not collect.** Gender, caste, religion, age, marital status,
photographs, college name or tier, graduation year, and anything derived from a person's name.
Excluded at *extraction* time so they never enter the database, and listed in `BANNED_SIGNALS`
(`ARCHITECTURE.md` §5.3) so adding one is a visible code change.

This constraint got *harder* under the reversal, which is why it is restated here: a LinkedIn profile
hands us education history, graduation dates and a photograph on a plate. The extractor drops them at
the door. Emails are stored as `sha256` only.

### 4.7 How a source earns its place, or loses it

Every source reports three numbers per run: **found**, **survived the gates**, **shortlisted**. A
source with no shortlisted candidate after 30 days is disabled with one row update — decided on the
number, not on a feeling.

The spine is the exception, and it deserves honesty: **LinkedIn cannot be dropped on yield the way
`stackexchange` can, because nothing replaces it.** GitHub cannot restrict us; LinkedIn can. A block
or a markup change breaks discovery with no warning and no appeal (`ARCHITECTURE.md` §9.4c). The
fallbacks are `manual_paste` and a paid seat, both of which put a human back in the loop.

## 5. How a candidate gets ranked

Two stages. The first is yes/no with no partial credit. The second is arithmetic simple enough to do
on paper — deliberately, because a recruiter has to be able to defend it to a hiring manager without
a laptop.

### 5.1 Stage zero — the snippet gate

Before anything is fetched, each discovered profile is tested against the spec using **only** the
free search snippet: current title, employer, city. No page load, no cost, no risk. Most candidates
leave here, and that is the point — enrichment is capped at 15 profiles a day in mode C, so a role
that had to enrich everything it discovered would manage one role a fortnight.

Candidates dropped at this stage appear in the excluded list like any other, labelled with what the
snippet said, because "we never even looked at them" is exactly the kind of silent narrowing that
makes a tool untrustworthy.

### 5.2 Stage one — three gates

Evaluated after enrichment, before anyone is scored. Fail one and the candidate leaves the ranked list entirely and
appears under "show excluded (N)" with the reason attached (`gates_json`).

| Gate | The question it asks | A typical failure |
|---|---|---|
| `must_have_skills` | Is there at least one piece of evidence, **of any tier**, for every skill marked must-have? | Kubernetes was marked must-have and there is nothing about Kubernetes anywhere in their public work. |
| `location` | Is their region in `spec.locations` — or is `spec.remote` set to `remote` or `remote_ok_relocate`? | Role is Bangalore-only, profile says Berlin. Or their location is published nowhere, so we cannot tell, and we do not guess. |
| `activity` | Is their most recent `artifact_backed` evidence inside `spec.max_staleness_days` (default 540 — eighteen months)? | Impressive body of work, last touched in 2019. |

Why gates rather than letting the score absorb it: someone who cannot be in the city is not a "70%
match", they are a no. Folding hard requirements into a score produces a beautifully ranked list of
people you cannot hire. And the excluded list is never hidden, because a tool that silently drops
people is indistinguishable from a tool with a bug (R5).

### 5.3 Stage two — five components, added up

Everyone who passes gets five marks, each in the range 0–1, each multiplied by a weight and summed.
That is the whole calculation. All five are always present in `components_json`, even at zero.

| Component | The plain question | Weight, `v1` |
|---|---|---|
| `skill_match` | Of the skills asked for, how many do we have evidence for — and how strong is that evidence? | 0.20 |
| `skill_depth` | Not "has Python" but how much Python, how recently. Log-scaled, so a 200-commit repo is not worth 200 one-commit repos. | 0.20 |
| `seniority_fit` | How close is experience to the range asked for, **computed from the employment dates on the profile** rather than estimated. Well over counts against, same as under. | 0.20 |
| `activity_recency` | How long since they last published anything, as a smooth decay rather than a cliff. | 0.20 |
| `availability` | Any public signal they are looking. **Unknown scores 0 and never goes negative.** | 0.20 |

**All five weights start equal**, because we have no evidence yet for any other split and inventing
one would be a guess wearing a decision's clothes. Weights live in a `scoring_weights` row with a
version, not in code, and every stored `match` records the `scorer_version` and `weights_version` in
force — so "why was this ranked third last Tuesday" is always answerable, and a re-score is a new
row rather than an overwrite.

Recruiter decisions aggregate into a *proposed* weight change on the admin page — "shortlisted
candidates scored higher on `skill_depth` than rejected ones; suggest +0.05" — which a named human
approves. The tool never retunes itself, and it never learns from who we hired before, because that
would faithfully reproduce whatever bias those hires contained.

### 5.4 What each tier is worth

Inside `skill_match`, the three tiers from §3 carry different weight for the same claim:

| Tier | Multiplier | Reasoning |
|---|---|---|
| `artifact_backed` — **Proven** | 1.00 | You can open the work and read it. |
| `third_party_stated` — **Third party** | 0.60 | Somebody with no stake says so. |
| `self_reported` — **Says so** | 0.35 | Not worthless — most true things about a person are only ever self-stated — but it is the weakest thing we hold. |

So proven Python outranks listed Python on that skill by roughly three to one. The tiers never
collapse into a single confidence figure at any point, including on screen.

### 5.5 A worked example

Illustrative numbers, not measured output. Role: Senior Backend Engineer, `IN-DL-NCR` or
`IN-KA-BLR`, 4–8 years. Must-have Python and PostgreSQL; Kubernetes nice to have.

**Gates:** evidence for Python ✓ and PostgreSQL ✓ · Bangalore ✓ · last published six days ago ✓ →
scored.

| Component | Value | Why |
|---|---|---|
| `skill_match` | 1.00 | All three skills present, all `artifact_backed` |
| `skill_depth` | 0.72 | Deep in Python, thin in PostgreSQL |
| `seniority_fit` | 0.85 | 9+ years against a 4–8 ask — slightly over |
| `activity_recency` | 0.98 | Six days |
| `availability` | 0.00 | No public signal either way |

`(1.00 + 0.72 + 0.85 + 0.98 + 0.00) × 0.20 = ` **0.71**, displayed as **Strong match**.

That example shows something that would otherwise read as a bug: **almost nobody scores near 1.0.**
Hardly anyone announces publicly that they are job-hunting, so `availability` is zero for most
people and a fifth of the total is unreachable from the start. This is why the screen shows "Strong
match" rather than "71%" — a percentage invites comparison against 100, and 100 is not a real number
here. It is also why `availability` is on probation: if it is ~0 for nearly everyone in M1 it is
contributing noise, and it gets dropped with the weight redistributed
(`ARCHITECTURE.md` §9.4a, §11 risk row).

### 5.6 The two estimates people will question

**Years of experience.** Now added up from the `from_date`/`to_date` spans on the profile, which
retires the old downward skew — we no longer infer career length from publishing history. Two
caveats survive, and the card states which applies:

- Stated spans are `self_reported`, and people round them generously. The number is a *claim*, not a
  measurement, and it is badged as one.
- If enrichment returned no dates — which is a live risk in mode B (§13.1) — we fall back to the old
  artefact-derived floor, "≥9 years of public work", and the old skew applies to that candidate. The
  card says which method produced the number, because a computed 7 and an estimated 7 are not the
  same fact.

Still never read off a job title, because titles inflate at wildly different rates between
companies.

**Location.** Taken from what the person published — profile field, own site, conference bio — and
matched against a named region list in `data/regions.yaml`, so "Bengaluru", "Bangalore" and "BLR"
resolve to one region. `IN-DL-NCR` covers Delhi, Gurgaon, Noida, Faridabad and Ghaziabad, because a
recruiter searching NCR means all of it. If nothing about location is published, we do not infer it
from a name, a language, or a university: the candidate fails the `location` gate and lands in the
excluded list where a human can see them and decide.

**And one thing we refuse to estimate:** whether two profiles are the same person. Merging happens
only on a strong key — same platform login, same personal domain, same hashed email, each unique to
one person by database constraint. Anything weaker goes to a human review queue and stays separate.
A wrong merge is invisible in the output and impossible to unpick later, so neither the code nor the
LLM is allowed to guess (`AGENTS.md` §3).

### 5.7 When the shortlist comes back thin — check in this order

The most common complaint about a tool like this is "it only found four people". Nearly always the
cause is a setting rather than missing candidates, and the excluded list says which. Open it first;
it groups people by the gate they failed.

| What the gate-failure histogram shows | What it means | The fix |
|---|---|---|
| "310 dropped at the snippet gate" | Normal, and healthy — this is the gate doing its job. Only worrying if what survived is tiny. | Nothing, unless the survivor count is low too. |
| "few profiles discovered at all" | Mode A cannot express the filter you need. It searches headline text; it cannot filter on years of experience. | Loosen the query, or this is the argument for mode D or a Recruiter seat (§4.3). |
| "27 failed `location`" | Location list too tight, or these people never published a city. | Add a region, or set `spec.remote`. Ten seconds. |
| "31 failed `must_have_skills`" | Almost certainly a nice-to-have marked must-have. One must-have like Kubernetes removes most otherwise-excellent backend engineers. | Move it to nice-to-have. It still counts towards `skill_match`; it just stops being a veto. |
| "19 failed `activity`" | Eighteen months excludes anyone who went quiet inside a demanding job — which is most senior people. | Widen `max_staleness_days` for senior roles. |
| Few exclusions — the search itself returned little | *Now* it is a coverage problem: the people wanted are not in these sources. | More sources (§4.3), or §6.3. This is the only case where that is the right response. |

This ordering is the whole point of the histogram. Escalating to a riskier data source to fix what
turns out to be a too-tight location filter spends real risk to avoid a ten-second fix — and that is
the single most likely way this project goes wrong (§11).

---

## 6. Scope

### 6.1 In scope — v1 (Milestone 1)

| # | Requirement | Acceptance |
|---|---|---|
| R1 | Recruiter pastes a JD and gets a structured, **editable** role spec | Spec form is pre-filled; recruiter can change every field; nothing runs until they confirm |
| R2 | System discovers candidates from LinkedIn and filters them on snippet metadata before fetching | ≥200 refs discovered and ≥40 surviving the snippet gate, for a typical Python/backend Bangalore role |
| R3 | Every claim is an evidence row with `source_url`, verbatim `snippet`, `tier` | Invariant test: every snippet is a literal substring of its stored source body. Zero exceptions |
| R4 | Candidates ranked by a deterministic score | Same role re-run on the same evidence produces a byte-identical `components_json` |
| R5 | Hard gates exclude candidates, **visibly** | "Show excluded (N)" toggle listing each candidate and the gate that failed |
| R6 | Recruiter can answer "why is this person #3?" from the card alone | Five component bars plus the evidence rows behind each, each with a clickable source URL, and each badged by tier so a profile claim never reads as a verified one |
| R7 | Recruiter marks shortlist / reject / contacted | `recruiter_action` recorded with the `weights_version` in force at the time |
| R8 | No source is fetched without an enabled `source_policy` row with `reviewed_at` filled | Fetch layer raises `PolicyDenied`; there is no second code path to the network |
| R9 | Every shortlisted candidate shows at least one `artifact_backed` claim, or is visibly flagged as unverified | A shortlist row with only "Says so" badges carries an explicit *unverified* marker. This is the reversal's acceptance test (§3) |
| R10 | The product still works when LinkedIn enrichment is unavailable | Disabling `linkedin_profile` yields a non-empty ranked shortlist plus a banner naming what is missing. Tested end to end (`IMPLEMENTATION.md` §1.11b) |
| R11 | A silent adapter failure becomes visible within one run | `/admin/health` goes amber on zero rows and red on error. Green means *produced rows*, never merely *did not throw* |

### 6.2 In scope — later milestones

- **M2 — verification breadth:** package registries (§13.5), `serp_web` (portfolios, resume PDFs,
  conference speakers, MAINTAINERS files, team pages), `huggingface`, `codeforces`, `gitlab`,
  `stackexchange`, `manual_paste`, rationale prose.
- **M3 — LinkedIn escalation, only if measured need:** mode C `attached_browser` if mode B's yield is
  too thin, and mode D `people_search` only if `role_query.results_count` shows mode A starving the
  search. Neither is automatic; each needs its number first.
- **M4:** observability page, retention/erasure job, weight-proposal review.

**Modes A and B are M1, not M3.** That is the substantive change from the previous version of this
plan: LinkedIn is no longer a late, optional, risk-gated adapter. It is the spine, and the two
zero-account-risk modes ship first.

### 6.3 Authorised contingency — `proxied_public` (not built)

Decided 2026-08-23 by ayushmaan, over a stated objection recorded in `ARCHITECTURE.md` §2.1. If the
plan above does not produce usable shortlists, we escalate LinkedIn access to logged-out fetching
over a residential proxy pool with fingerprint rotation, capped at **50 profiles per role**.

Three things make this a plan rather than a decision to build now:

1. **It is trigger-gated.** ARCHITECTURE §7.5.1: contact rate < 20% *and* median shortlist < 10 *and*
   LinkedIn modes A–C live 30 days showing that LinkedIn is genuinely where the missing candidates
   are. All three, measured off the admin page.
2. **The cheap diagnosis comes first.** A thin shortlist is usually a tight location gate, a bad
   `role_spec`, or a starved query plan — not missing coverage. §5.6 is that diagnosis, it takes a
   minute, and the fix is usually one line versus a real risk increase.
3. **It requires a dated go decision** appended to ARCHITECTURE §7.5 by a named human.

Two things to be clear-eyed about, since they change the shape of the escalation:

- **"Internal, not commercial" is not a legal safe harbour.** DPDP's carve-out is for personal or
  domestic use; company recruiting is business processing regardless of whether output is sold, and
  LinkedIn's User Agreement bars automated access on the same terms either way. hiQ won on CFAA and
  **lost on breach of contract** — $500k, injunction, data destruction. The **50-profile cap** is
  what actually reduces exposure here, and it is doing all of the work.
- **It replaces mode C, it does not stack with it.** Mode E is logged out; routing the recruiter's
  real account through a rotated residential IP is a *stronger* detection signal than either factor
  alone. Enabling mode E disables mode C in the same transaction. The upside of that trade: **no
  individual's personal LinkedIn account is at risk any more** — the residual risk moves from a
  person to the company. For an internal tool that is arguably the better trade, and it is the
  strongest argument for mode E.

Data depth is lower than mode C (logged-out profiles show materially less). Kill criterion: if
contact rate is not above 30% within 30 days of go-live, disable it and accept coverage as the
constraint.

### 6.4 Explicitly out of scope

| Not building | Why |
|---|---|
| LinkedIn-scale coverage | We find engineers who leave public artefacts. We knowingly miss excellent engineers who do not publish. **This expectation must be set before the first demo** (§4.6). |
| CTC, expected CTC, notice period as *discovered* fields | Not publicly discoverable. Recruiter-entered post-contact, tier `recruiter_input`, excluded from ranking. Bands, never point values. |
| Outreach — email sending, sequences, templates | Different product. GitHub's AUP forbids using its data for unsolicited email; we identify people, the recruiter contacts them through a channel the person opened. |
| ATS, interview scheduling, offer management | Different product. |
| A continuous crawler | Searches are role-triggered and cached. Holding data on people nobody is evaluating is a liability, not an asset. |
| Non-engineering roles | Sales and marketing leave no artefacts. The tier model has nothing to grip. |
| Candidate-facing anything | No profiles, no logins, no "claim your profile". Erasure requests come by email. |

## 7. The four screens

Detail lives in `IMPLEMENTATION.md` §1.11. Product requirements only here.

1. **Roles** — list, status, shortlist count, "New role". Nothing else.
2. **New role** — paste JD, then confirm spec. Two steps, one page. The confirm step is a **hard
   gate**; the LLM draft is never used unedited.
3. **Shortlist** — ranked rows. Each row: name, one-line summary, a plain-word match rating, the
   top-two evidence quotes with tier badges. Sortable, filterable, "show excluded (N)".
4. **Candidate detail** — a side panel, not a page. Component breakdown, every evidence row grouped
   by tier with source links, action buttons, and whether this person was actioned on a previous
   role.

Two non-negotiable UI rules: **a recruiter must never see a number they cannot trace to a quote**,
and **tier badges are never the same colour.** The score is shown as words, not a percentage, for the
reason in §5.4.

## 8. AI usage — deliberately minimal

> Free-tier / low-cost operation is a v1 requirement, not an optimisation.

`AGENTS.md` sanctions four LLM call-sites. For v1 we run **one**, and it touches no candidate data.

| Call-site (`AGENTS.md`) | v1 decision | Rationale |
|---|---|---|
| **JD → role spec** (§2.1) | **ON** — free tier acceptable | The only text here is a job description. It is company data, not candidate personal data, so a free tier whose terms permit training on inputs is tolerable. The recruiter confirms every field anyway. |
| **Query expansion** (§2.2) | **OFF in v1** | The skill canon plus alias table does this deterministically. Turn on only when `role_query.results_count` shows canon-only queries are starving the search. |
| **Page → evidence** (§2.3) | **OFF for GitHub** | The GitHub API returns structured JSON. Languages, repo counts, stars, commit dates, `hireable` — all of it is a parse, not an inference. Spending a token to read JSON we have already parsed is the definition of waste. Turn on only for free-text documents (READMEs, bios, portfolio pages) in M2. |
| **Rationale prose** (§2.4) | **OFF in v1** | The component breakdown *is* the explanation. Prose is a nicety. Ship it in M2 once recruiters tell us the bars are not enough. |

**Milestone 1 therefore makes zero LLM calls against candidate data.** A recruiter can run a full
search on a laptop with no API key, and R6 ("why is this person #3?") is answered entirely by
deterministic components and quoted evidence.

The governing rule at every budget, from `AGENTS.md` §3: **a model may read unstructured text and
emit structured data, or write prose a human will read. It may never decide, rank, merge, or do
arithmetic.** No LLM computes a score, merges two candidates, decides whether a URL may be fetched,
or re-ranks a list.

### 8.1 Constraints that hold when call-sites do turn on

- **Free tiers that train on inputs are banned for candidate text.** A free Gemini or Groq key
  sending a candidate's bio into a training corpus is a DPDP problem regardless of how the data was
  obtained. Candidate-text extraction runs on either a local model (Ollama) or a paid API with a
  no-training term. JD parsing is exempt — no personal data.
- **The extraction model stays cheap.** Haiku-class or a local 7–8B model. This is schema-filling,
  not reasoning. Do not upgrade the model to fix a prompt problem.
- **Documents per candidate capped at 8** (default). One candidate with 300 repos must not cost more
  than the rest of the search.
- **A role search that exceeds its token ceiling pauses and asks.** It does not keep spending.
- **Token counts per role are logged to `pipeline_event`** and shown on the admin page. If we cannot
  see the cost, we are not controlling it.

**Resolved 2026-08-25** — the two docs no longer disagree, and the numbers came down sharply once the
spine changed. Full table in `ARCHITECTURE.md` §10.

| | Per role | Per month at 30 roles |
|---|---|---|
| **M1** — discovery + snippet gate + mode B + GitHub verify | **≈ ₹25** | **≈ ₹750** |
| **M2** — plus free-text extraction | **≈ ₹60** | **≈ ₹1,800** |

Plus one VM at ~₹2,000/month. No paid data subscriptions. For comparison, one Naukri Resdex seat
starts around ₹55,000 per quarter.

Why M1 is nearly free: **the scraper returns structured data, so there is no LLM call on candidate
text at all.** The only call is JD parsing, at roughly ₹1/month. M2 extraction uses
`claude-haiku-4-5` with the Batch API (−50%, and a role search is already an async job so latency is
free to trade) and prompt caching on the stable schema prefix (~0.1× on the repeated portion).

**If zero LLM spend is required:** JD parsing contains no personal data and can run on a free tier.
M2 extraction reads candidate bios and must not go to a free tier that trains on inputs — run it on
local Ollama instead. That split is the only place where cheapest and correct diverge.

## 9. Success metrics

Measured 30 days after the first real role, then again at 90.

| Metric | Target | Why this one |
|---|---|---|
| **Contact rate** — shortlisted candidates the recruiter actually reaches out to | **≥ 40% of top 20** | The whole product thesis. If a recruiter will not contact 8 of 20, the ranking is not working. |
| **Time to shortlist** — JD confirmed to usable list | **< 30 min** | Beats half a day of engineer eyeballing. |
| **Traceability** — shortlisted candidates where the recruiter could state the reason unprompted | **100%** | Non-negotiable. A single untraceable rank is a bug. |
| **Verified share** — shortlisted candidates with at least one `artifact_backed` claim corroborating a must-have skill | **≥ 60% of top 20** | *The* metric for the reversal. The spine is entirely self-reported, so this number is the difference between a product and a LinkedIn search. Lower than the old 70% target because the candidate pool is no longer pre-filtered to people who publish. |
| **Mode B yield** — discovered profiles returning dated `experiences[]` when fetched logged out | tracked, decides §13.1 | Determines whether mode C is needed at all. |
| **Adapter yield** — per adapter: discovered → passed gates → shortlisted | tracked, no target | The number that decides which sources live and die (§4.5), including whether modes C and D earn their risk. |
| **Gate-failure distribution** | tracked | A shortlist of 3 must be explainable — "27 failed the location gate", not a shrug. This is the input to §5.6. |
| **`availability` distribution** | tracked | Decides whether the fifth component survives M1 (§5.4). |
| **Cost per role** — fetches, SERP queries, LLM tokens | tracked, ceiling enforced | Free-tier operation is a requirement; drift is caught here. |

**Anti-metric:** candidates discovered. 5,000 rows is a failure mode, not a result. 20 candidates a
recruiter wants to contact beats 5,000 they scroll past.

## 10. Legal and privacy posture

Summarised for the non-technical reader; the authoritative version is `ARCHITECTURE.md` §9.5, and
**none of this is legal advice.**

- **What changed on 2026-08-25.** The exemption used to be load-bearing, because the primary source
  was self-published artefacts. It no longer is. LinkedIn data is published *to LinkedIn under
  LinkedIn's terms*, not to the world by the person, so **the spine sits outside DPDP §3(c)(ii)** and
  the default assumption for a candidate record is now *in scope*. This is a real increase in
  obligation, and it is the price of the coverage.
- **What we still rely on it for.** §3(c)(ii) continues to cover the *verification* sources — GitHub
  profiles, personal sites, self-published resumes — processed in the form they were made public.
  DPDP Rules were notified 14 Nov 2025, so this is live law.
- **Two limits that shape the build.** Crawlable ≠ exempt: data behind a login, or from a
  third-party aggregated dataset, does not qualify — which now includes **every LinkedIn mode's output, mode A included** — the spine itself, not just a
  contingency. All of it is flagged in-scope. And **combining
  public data with non-public data can pull the whole record back into scope**, which is the
  strongest technical argument against ever importing a purchased enrichment file (§4.3).
- **What we do regardless of exemption — now mandatory rather than good posture.** Honour erasure by
  email, deleting evidence rows *and* cached page bodies. Retain 180 days by default for anyone with
  no recruiter action and no match against an open role. Keep per-claim provenance so any stored fact
  traces to its source. Since the spine is in scope, these move from "cheap and correct" to
  load-bearing, and the retention/erasure job (M4) is no longer safely deferrable to last.
- **The chokepoint.** No domain is fetched without an enabled `source_policy` row with `reviewed_at`
  filled by a named human. `robots.txt`, rate limits, daily caps and caching all live in the one
  fetch layer, and a cross-domain redirect re-enters the policy check. No residential proxies, no
  captcha solving, no fingerprint spoofing — except the scoped, unbuilt, trigger-gated exception in
  §6.3, where captcha solving stays banned anyway.

## 10a. Operating it after hand-off

The author of this tool builds it and then is not associated with it. That is the single largest risk
to it *working*, and it outranks every feature in this document. Full engineering detail in
`ARCHITECTURE.md` §9a; what the receiving team needs to know:

**It will break, and the most likely break is silent.** LinkedIn changes its page markup every few
months. When it does, enrichment returns empty rather than erroring. Three things are built in M1 so
that this is survivable rather than fatal:

1. **A health page** (`/admin/health`) in plain words, red/amber/green per source. Amber means "ran
   fine and found nothing", which is exactly what a markup change looks like. **Somebody has to
   look at this page monthly.** That is the whole maintenance burden, and it is the one thing that
   cannot be automated away.
2. **A degraded mode that still works.** If profile enrichment dies entirely, discovery still
   provides name, current title, employer and city from search results, and verification still runs
   against GitHub. The shortlist keeps working with a banner explaining what is missing; what is
   lost is computed years-of-experience. One configuration row switches between them.
3. **A runbook** written for a competent generalist, not for a maintainer: symptom, what to check,
   what to do. Including how to turn LinkedIn off entirely and keep using the tool.

**Two obligations transfer with the tool, and they are not optional.** A named person at the
receiving company must own them before the first source is switched on:

- **Data protection.** Candidate records here sit *inside* DPDP scope (§10) because the primary
  source is LinkedIn rather than self-published work. Erasure requests must be honoured, retention
  is 180 days, and both are built — but the legal responsibility follows the company, not the
  author. Whoever operates it needs to know that before switching it on rather than after.
- **The LinkedIn account decision.** See §13.4. The delivered default uses **no LinkedIn account at
  all.** If the company later wants richer data, that is their volunteer, their consent, and their
  risk — with the caps already implemented in code.

## 11. Risks

| Risk | Severity | Response |
|---|---|---|
| **Coverage disappoints the recruiter** | High · likely | Set the expectation before the first demo (§4.6). Show the excluded list so narrowness reads as deliberate, not broken. |
| **Seniority estimate skews low** | Medium · certain | Display as a **floor** — "≥5 years of public work" — with inputs visible. Never a range implying we know the ceiling (§5.5). |
| **DPDP exemption does not hold** | High · unlikely for M1 | §10. Never import a purchased enrichment file. Honour erasure regardless; 180-day retention. |
| **LinkedIn restricts the recruiter's account** (M3 only) | Medium · possible | Whoever's account is used consents explicitly and in advance. Caps in code: 15 profiles/day/account, concurrency 1, ≥20s delay, never headless, kill switch on first challenge. This risk lands on a person, not a server. |
| **Mode 4 contingency is built early, or "just to see"** | Medium · likely | This is the realistic failure mode — it is written down, so it looks available. Three gates in ARCHITECTURE §7.5.1 plus a dated human go decision. Run §5.6 first; a thin shortlist is usually a scoring or spec bug, and escalating access to route around one spends real risk to avoid a one-line fix. |
| **Mode 4 goes live and LinkedIn pursues it** | Medium · low at 50/role | Contract exposure, not CFAA — hiQ lost that claim. The cap is the mitigation that matters. Evidence flagged outside the DPDP exemption; erasure honoured. No burner accounts, no captcha solving, no purchased datasets at any volume. |
| **`availability` component is dead weight** | Low · likely | Measure its distribution in M1 (§9); drop the component and redistribute the weight if it is ~0 for most candidates. |
| **Extraction silently omits content** | Medium · possible | We test against fabrication (R3), not against omission. Hand-label ~30 pages as a recall baseline before M2. |
| **Nobody uses it — it is one more tool** | High · possible | The only real mitigation is M1 being genuinely good, which is why it is narrow. A recruiter with GitHub advanced search and thirty minutes can do one role by hand; this earns its keep on repeatability, the evidence trail, and five roles at once. If the team hires two engineers a year, a saved-search checklist beats this, and we should decide that now (§13.3). |
| **No named compliance sign-off** | **Blocker** | No `source_policy` row is enabled without `reviewed_at` filled by a named human. Currently open (§13.1). |
| **Mode B returns profiles without dates** | High · unknown | The single largest unknown in the plan (§13.1). Measure on ~50 profiles before writing the adapter. If it fails, the choice is mode C's account risk or a paid seat — decide then, on the number. |
| **The spine gets blocked and nothing replaces it** | High · possible | GitHub cannot restrict us; LinkedIn can, with no warning and no appeal. Mitigations are ordered in `ARCHITECTURE.md` §9.4c: mode A degrades to `manual_paste` and a paid seat, the on-disk cache lets an in-flight role finish, `source_policy` disables the adapter rather than failing the pipeline. A bad week for the adapter is a bad week for the product. |
| **Shortlists fill with unverified profiles** | High · likely | The failure mode of the reversal: discovery works, verification does not, and we have shipped LinkedIn search. R9 is the guard — an unverified shortlist row is visibly marked — and "verified share" in §9 is the metric that catches it. |
| **Someone uses a burner account because it seems safer** | Medium · likely | It is not safer, it is worse on all three axes (`ARCHITECTURE.md` §7.4): it is the aggravating fact in LinkedIn enforcement, it returns *less* profile depth, and on one laptop it contaminates the IP the real account uses. Separate browsers, yes; separate identities, no. |
| **Scope creep toward "AI recruiter"** | Medium · likely | `AGENTS.md` §3 is the fence. Every request to "let the model decide" gets a deterministic stage instead. |

## 12. Release plan

| Milestone | Deliverable | Done when |
|---|---|---|
| **M1** | Spine end to end: skeleton, schema, fetch layer, canon, mode A discovery, snippet gate, mode B enrichment, GitHub verification, scoring, four screens | A recruiter runs a real role unaided and shortlists someone, and every shortlisted candidate carries at least one verified claim or a visible unverified flag (R9). **Zero LLM calls on candidate data.** |
| **M2** | Package registries, `serp_web`, remaining verification adapters, `manual_paste`, rationale prose | Verified share (§9) is above target, and the adapter yield table has ≥3 verifiers with 30 days of data |
| **M3** | Mode C and/or mode D, each only if its number justifies it | The escalation was triggered by measured need, not by impatience; adapter can be killed by one row update |
| **M4** | Observability, retention and erasure, weight proposals | Erasure verifiably removes evidence rows *and* cached bodies on disk |

## 13. Decisions and remaining questions

**Decided 2026-08-25**, so implementation can start. Four were genuine questions and are now closed;
three need the receiving company and only one of those blocks anything.

### 13.1 Task, not a question — measure mode B first ⚠️

What fraction of Indian engineer profiles return `experiences[]` **with dates** when fetched logged
out? High → ships with zero account risk. Low → the choice is mode C or a paid Recruiter seat.
**~50 profiles, about a day, and it is step 1.4a — before the enrichment adapter is written.** Record
the number here when it exists. Everything downstream is guesswork until then.

#### Measured 2026-08-26 — the spike could not run as designed. Blocked upstream. ⛔

`https://www.linkedin.com/robots.txt` returns, for `User-agent: *`, exactly one directive:

```
Disallow: /
# Notice: If you would like to crawl LinkedIn,
# please email whitelist-crawl@linkedin.com to apply for white listing.
```

Every path, all agents. Verified against the real fetcher, which correctly raised
`RobotsDenied: www.linkedin.com: disallowed by robots.txt` on a profile URL. **The yield number
does not exist and cannot be obtained while `respect_robots` is honoured**, so mode B has no
measurable yield rather than a low one.

This is a contradiction inside our own documents, not a new external constraint:
`ARCHITECTURE.md` §7.3 says "robots.txt honoured; a disallow is a hard skip", and §7.4 mode B says
"fetch the logged-out public version of a profile URL". Both cannot hold. One has to give, and that
is a decision for the receiving company (§13.2), not an implementation detail.

**What this does not block.** Mode A (`linkedin_serp`) is unaffected — it queries a SERP vendor and
never touches a LinkedIn surface. SERP snippets carry name, title, employer and location, which is
everything the snippet gate needs. The degraded path in `ARCHITECTURE.md` §9a.2 /
`IMPLEMENTATION.md` §1.11b — discovery → gate → GitHub verification → score, with seniority
estimated from artefacts rather than computed from employment history — was written for exactly
this situation and is now the **primary** path, not the fallback.

**Options, for whoever holds §13.2:**

| Option | Cost | Notes |
|---|---|---|
| Ship modes A + degraded (recommended) | none | Works today. Seniority estimated, flagged as such in the UI. |
| Email `whitelist-crawl@linkedin.com` | days–weeks | The sanctioned route; the robots file explicitly invites it. Free to try, likely declined for this use case. |
| `manual_paste` (`IMPLEMENTATION.md` §2.3) | small build | Recruiter pastes a profile they already have open. Zero risk, already planned, covers the high-value few. |
| Set `respect_robots=false` for linkedin.com | — | The schema has the knob and it is the company's call. Note it likely buys little: logged-out profiles authwall aggressively and return HTTP 999, so the practical yield may be ~0 regardless of the policy position. Measure before relying on it. |

**Recommendation: ship A + degraded now**, try the whitelist email in parallel, and revisit only if
the shortlist quality actually proves insufficient — per `IMPLEMENTATION.md` §3.2, a thin shortlist
is far more often a tight location gate or a starved query plan than missing LinkedIn coverage.

#### Resolved 2026-08-27 — mode C built, `respect_robots` left alone 🔧

The contradiction above is settled the way §7.4 always intended, and **without** flipping
`respect_robots`:

- **Mode B stays blocked and `respect_robots` stays `true`** on both LinkedIn rows. The
  logged-out crawler is what `robots.txt` addresses, so honouring it there is simply correct,
  and a test (`test_mode_b_stays_blocked_by_robots`) fails if that flag ever flips as a side
  effect of something else. Flipping it remains available to §13.2 as a *decision*, and is not
  needed for anything now.
- **Mode C (`attached_browser`) is built and shipped disabled.** It is not a crawler: it is one
  operator driving their own already-logged-in browser, at human pace, over pages that account
  may see. It never launches a browser, never logs in, and reads no credential — it attaches
  over CDP to a browser a human started, which is why `linkedin_scraper/core/auth.py` can stay
  unported. Limits live in code and cannot be edited in a row: **15/day, serial, ≥20s apart,
  visible window, self-disabling on the first wall.**
- **It stays off until a person turns it on** (`… linkedin_profile enable`, which records who),
  because the residual risk is that individual's account, and the CLI refuses to run without
  the name.

**The yield number is still unmeasured** — mode C's measurement replaces mode B's, and 1.4a is
now `linkedin_profile measure`, which scrapes and reports the dated-experience share while
storing nothing about anyone. Record it here when it exists.

| Mode B (blocked) | Mode C (built, off) |
|---|---|
| httpx, no session | operator's own browser, over CDP |
| refused by `robots.txt` | not a crawler; `robots.txt` unchanged and still honoured for B |
| no account risk | risk lands on one named person's account |
| yield unmeasurable | yield measurable, still unmeasured |

#### Measured 2026-08-27 — mode C yield: **100% (3/3)** ✅

The number §13.1 was written to wait for. Live mode C run against the Bangalore backend role,
reading `details/experience/` for each candidate:

| Measure | Value |
|---|---|
| Profiles attempted | 3 |
| **Carrying dated `experiences[]`** | **3 — 100%** |
| Evidence rows written | 28 |
| Roles per profile | 4–10 |
| Walls, challenges, HTTP 999 | **0** |
| Strong keys (GitHub, personal domain) found | **0 of 3** |

**What this settles.** `seniority_fit` is now *computed* rather than estimated for enriched
candidates, so the degraded path in §9a.2 is a fallback again rather than the primary route. n=3 is
small and the sample is one role in one city — treat 100% as "this works", not as a population
statistic. Re-measure on a role in a different city before quoting it to anyone.

**What it did not solve, and the more useful finding: nobody linked a GitHub account.** Zero strong
keys across three profiles, which matches the intuition that Indian engineers rarely put a GitHub
URL on LinkedIn. Artefact verification therefore reaches very few LinkedIn-discovered candidates,
and the product cannot depend on it. The response was to make LinkedIn's own Skills section
`self_reported` skill evidence (`details/skills/`), so `skill_match` has something to read at a
tier-discounted 0.35 while `skill_depth` correctly stays 0 without artefacts. A LinkedIn-only
candidate is now rankable; a GitHub-verified one still outranks them on identical claims, which is
the whole point of the tier system.

**Two bugs this run exposed, both fixed:**

- Every `employer` row was silently refused: `is_banned` matches substrings in both directions, and
  the claim_type `employer` is a substring of the banned signal `current_employer_prestige`. The
  writer no longer passes claim_type through the fuzzy check — it is a closed enum already checked
  exactly, and against `REFUSED_CLAIM_TYPES`.
- `enriched` counted any profile whose page loaded, so the previous run reported "enriched 5,
  evidence 0" — a green light over a dead parser. It now counts only profiles that produced
  evidence, and a profile that yields nothing says so and points at `refresh_fixture.py`.

#### Re-measured 2026-08-28 — mode C yield: **92% (11/12)**, and this is the number to quote ✅

A second live run on the same Bangalore backend role, 12 profiles instead of 3. It supersedes the
100% above, which was n=3 and was recorded with the caveat that it meant "this works" rather than a
statistic. **92% at n=12 is the honest figure** — still one role in one city, so re-measure before
quoting it outside the team.

| Measure | 2026-08-27 | 2026-08-28 |
|---|---|---|
| Profiles attempted | 3 | **12** |
| Carrying dated `experiences[]` | 100% (3/3) | **92% (11/12)** |
| Evidence rows written | 28 | **190** |
| Walls, challenges, HTTP 999 | 0 | **0** |
| Strong keys found | 0 of 3 | **1 of 12** |
| Candidates past the hard gates | 1 | **9** |

**One strong key in twelve.** Better than zero, and still low enough that the §13.1 conclusion
stands: artefact verification cannot be the product's spine for LinkedIn-discovered candidates.

**Three bugs this run exposed, all fixed** (details in `IMPLEMENTATION.md` §2.1a):

- `_entries` read the two lines above each date line as (title, employer), a fixed offset that is
  wrong for two of the three layouts LinkedIn ships. It produced `employer` claims reading
  `"Full-time"`, `"Internship"`, `"Senior Software Engineer"` and `"Python Developer"`.
  `EXTRACTOR_VERSION` is now `linkedin_mode_c@2`.
- `BANNED_SIGNALS` contained the bare word `"college"`, so an education row for any institution
  named "… College …" was silently refused — five of the twelve profiles. That word predated the
  §2.2 decision to store and show the institution, and after it, contradicted it. The compound keys
  `college_name` and `college_tier` stay: a *feature* keyed on college tier is the caste proxy §2.2
  refuses to rank on, and neither matches an institution's name.
- The recruiter page reported unread candidates as ruled out. An unread candidate has no evidence,
  so every gate read "no evidence for Python", which the page rendered as **"No public sign of
  Python"** — a claim about someone nobody had opened, covering **18 of 25 exclusions** on this
  role. The page now has three groups: ranked, ruled out, and *not looked at yet*.

#### Re-measured 2026-08-31 — mode C yield: **95% (35/37) cumulative**, and the role is fully read ✅

Two runs on the same Bangalore backend role, both clean. The first read the 18 people discovery had
found and nobody had opened; the second used the new `--only` flag to re-read the 7 profiles whose
rows predated the `@2` parser.

| Measure | 08-27 | 08-28 | 08-31 run 1 | 08-31 run 2 (`--only`) |
|---|---|---|---|---|
| Profiles attempted | 3 | 12 | **18** | **7** |
| Carrying dated `experiences[]` | 100% (3/3) | 92% (11/12) | **100% (18/18)** | **86% (6/7)** |
| Evidence rows written | 28 | 190 | **322** | **87** |
| Walls, challenges, HTTP 999 | 0 | 0 | **0** | **0** |
| Failed | 0 | — | **0** | **0** |

**Cumulative dated-experience yield is 35 of 37 = 95%**, and that is now the number to quote inside
the team. Still one role in one city; still re-measure before quoting it outside.

**The role is fully read.** It moved from 9 ranked · 7 ruled out · 18 not looked at yet, to
**10 ranked · 24 ruled out · 0 unread**. The 24 rule-outs are honest: 105 skill rows between them and
not one names Python. They are the Java, C#, Spring Boot and Angular people the broad
"Backend Engineer × Bangalore" title query pulled in — the query being wide, which is by design, not
the parser failing.

**It closed the question `HANDOFF.md` §6 left open.** Three of the seven re-read profiles did not
change at all. With the `@2` parser reading them and other profiles in the same run parsing
education fine, that is the answer: `v-pavan-kumar`, `chethan-p` and `er-surya-bhan` have those
sections empty on LinkedIn. `chethan-p` lists no skills whatsoever, so his "no evidence for Python"
rule-out is a true statement about a real absence. **This retires the manual-paste collection** —
seven scrapes cost seven reads out of thirty and answered it; twenty-eight hand-pasted files would
have answered the same thing slower.

#### Measured 2026-08-26 — mode A works, and here are the real numbers ✅

Live run against SerpAPI, one role (Backend Engineer · Bangalore · Python), 3 SERP searches spent:

| Measure | Value |
|---|---|
| Organic results per search | **~10** (Google caps `site:` queries at 10/page; `num=20` is ignored) |
| Refs per search after normalise + dedup | **~9.3** |
| Snippet-gate pass rate | **75%** (21 of 28) |
| Searches to reach 1.4b's "≥200 refs" DoD | **~21** |
| Searches to reach 1.4b's "≥40 passing gate" DoD | **~6** |

Gate rejections were all legible — `title: 'Senior Software Engineer / Java, Spring Boot,
Microservices' does not match Backend Engineer` and two `location:` rejections. That reason string is
the R2/R5 excluded-list text and it is already good enough to act on: it tells the recruiter to widen
`titles`, which is the correct fix.

**Depth costs searches, 1:1.** `discover(q, pages=N)` defaults to `pages=1`; raising it is the only
way to go deeper and each page is one search. So SERP spend per role is a deliberate dial, not a
surprise.

**Budget arithmetic** (free tier = 250 searches/month, PRD target = ~30 roles/month):

- At ~21 searches/role (200 refs) → **12 roles/month**. Free tier is ~3x short.
- At ~8 searches/role (~75 refs, ~56 passing — still comfortably over the 40 DoD) →
  **31 roles/month**, which fits the free tier exactly.
- If more depth per role is wanted, `serper.dev` at ~$1/1,000 searches makes 600 searches/month cost
  about **$0.60** — already the vendor named in `IMPLEMENTATION.md` §2.1, and ~100x cheaper than
  SerpAPI's paid tier for this workload. Switching is a one-function change in `_serpapi_url`.

**Recommendation: stay on the free tier at ~8 searches/role for now**; move to Serper if and when
depth per role turns out to matter more than the DoD requires.

### 13.2 Sources sign-off — **the one real blocker** ⚠️

A named human must fill `source_policy.reviewed_at` before any source is enabled. Still unassigned,
and it got weightier: the spine is outside the DPDP exemption (§10), so this is no longer a
formality over public repos. **Must be someone at the receiving company, not the author** — the
obligation outlives the engagement (§10a).
### 13.3 Roles per year — company input, non-blocking

If it is two, a saved-search checklist beats this and somebody should say so out loud. Fifteen or
more and it pays back fast. **Decision: build M1 regardless** — it is 3–4 weeks and the answer
arrives from using it. This only gates whether M2 is worth starting.
### 13.4 Whose LinkedIn account — **DECIDED: nobody's** ✅

**The delivered product uses modes A and B only, and touches no LinkedIn account.** Reasoning, and
it is the hand-off that settles it rather than caution:

- Wiring the author's own account into a tool the author is leaving means the tool breaks the day
  that access lapses, *and* that personal account carries ban risk for a company they no longer work
  at. Both bad, and avoidable.
- Modes A and B need no account at all.
- If the company later wants mode C's richer data, it is **their** volunteer, their informed consent,
  and their risk — with the caps already sitting in code (15/day, concurrency 1, ≥20s, never
  headless, self-disabling on first challenge).
- If mode C is ever run: separate *browser* for automation is fine and encouraged; a separate
  *identity* is refused, for the three practical reasons in `ARCHITECTURE.md` §7.4 — a burner is the
  aggravating fact in LinkedIn enforcement, it returns *less* profile depth, and on one machine it
  contaminates the IP the real account uses.
### 13.5 Recruiter Lite seat — company input, decide after §13.1

~₹8–12k/month buys real filters with no account risk, and it is the honest alternative to modes C and
D rather than an admission of defeat. **Decision: do not buy it yet.** Measure §13.1 first; if mode B
carries dates, nobody needs it.
### 13.6 Regions — **DECIDED, and cheap to change** ✅

Ship NCR, Bangalore, Hyderabad, Pune. Chennai and Ahmedabad stubbed but disabled. It is a checked-in
YAML file; adding a region is a one-line PR, so this was never worth blocking on.
### 13.7 Package registries — **DECIDED: yes, in M2** ✅

PyPI, npm, Maven Central, crates.io all have public JSON APIs with no auth and no rate-limit
problems, and "strangers install this person's code" is the strongest verification signal available —
which matters far more now that the spine is entirely self-reported. Add the `packages` adapter row
to `ARCHITECTURE.md` §7.2 when the adapter is written. Until then, §1 and §4.3's claim that a
published package is `artifact_backed` evidence is aspirational, and this note is the record of that.
### 13.8 Domain-weighted ranking — **DECIDED: no** ✅

`spec.domains` stays captured and displayed but unscored. Term overlap between project text and a
domain label is a weak, noisy signal, and a sixth component adds a weight nobody can tune with 30
roles of data. Revisit only if a recruiter asks for it by name.

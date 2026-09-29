# Handoff — state as of 2026-08-31

Read `CONTEXT.md` first (it is short); this is the detail behind it. The 2026-08-28 handoff is
superseded — where the two disagree, this file wins.

**534 tests, green, no live network in the suite.** 13 migrations. **Everything below is
uncommitted** — one commit is the first job tomorrow.

---

## 1. Where to start tomorrow

1. **Commit.** 23 modified files plus `scripts/regate_refs.py`, and last session's §2.5 docs are in
   there too. The tree has been green all day; it just has not been recorded.
2. **Decide the re-gate.** `python scripts/regate_refs.py --role e6c2de35 --apply` flips the two
   foreign candidates on the ops role to `failed` (§5). Dry run confirmed, owner had not answered
   when the session ended. It costs no SERP spend and frees 2 of the 5 remaining profile reads.
3. ~~**The button**~~ — built 2026-09-02, all five steps of §1.11d. What is worth doing next on it:
   the button has only ever been exercised against tests, so the first live click needs watching, and
   the panel does not surface the `dated-experience yield` line the CLI prints.
4. **The erasure endpoint** (§4.2). Not a feature — a DPDP obligation that transfers at hand-off.
   **Now the largest remaining gap.**

---

## 2. What the product does, end to end

**Paste a JD, SerpAPI finds unique LinkedIn profiles, we scrape them, we rank them, the recruiter
decides.** All of it works against live APIs and live LinkedIn. Milestone 1 is complete.

```
JD text ──LLM──▶ RoleSpec ──▶ [recruiter confirms] ──▶ SERP discovery ──▶ snippet gate
   ──▶ identity ──▶ LinkedIn enrichment (mode C) ──▶ evidence writer
   ──▶ deterministic scoring ──▶ ranked page
```

**One process runs all of it as of today**: `uvicorn hi.web:app` drains the job queue in-process
(`ARCHITECTURE.md` §2.6). `python -m hi.worker` still works and can run alongside.

---

## 3. Built today, 2026-08-31

| What | Where |
|---|---|
| `--only <slug>,<slug>` on the enrichment CLI, implying `--redo` | `linkedin_profile.py::filter_to_slugs` |
| The command panel on the role page — budget, queue, exact commands | `web/app.py::_collect_context`, `shortlist.html` |
| Locations as checkboxes, and the error marking the field | `role_form.html`, `roles.py::BLOCK_NO_LOCATION` |
| The job queue running inside the web process | `web/app.py::lifespan`, `config.inline_worker` |
| "The search has not started" when a job sits unclaimed >90s | `matching.role_progress`, `_shortlist_rows.html` |
| The location gate closed against unresolvable foreign places | `gating.py::_mentions_india` |
| "N we did not open" — the refs the gate refused, with reasons | `_shortlist_rows.html` |
| `ranked` counting only people actually read | `matching.role_progress` |
| `scripts/regate_refs.py` — re-runs the gate on stored refs | new file |
| **2026-09-01:** "Find more people" — one page deeper, plus a free re-gate of every stored ref | `web/app.py::find_more`, `discovery.regate` / `search_depth`, `IMPLEMENTATION.md` §1.11e |
| **2026-09-18:** The location gate only refuses people **outside India**. Another Indian city passes with the mismatch recorded on the verdict, because on live roles that was every location refusal but the foreign ones — and people relocate. Same rule in `scoring.evaluate_gates`, which now borrows `_region_compatible` rather than keeping a second copy | `gating.py::snippet_gate`, `scoring.py::evaluate_gates` |
| **2026-09-18:** `IN-REMOTE` ("Anywhere in India" on the role form) reads as `IN`. As a literal code it shared no prefix with any real region, so picking it rejected every Indian candidate — including people in the city the role named | `gating.py::_region_compatible` |
| **2026-09-18:** A role that keeps under half of what it found stops trusting its job-title filter and re-admits the refusals that are in India. `RESCUE_BELOW = 0.5`. Runs at the end of every search and every re-gate, so it applies to new roles without anyone asking. Employer exclusions and "outside India" still refuse — the rescue is the same gate re-run against a spec with no titles, not a second looser gate | `discovery.py::_rescue` / `regate` / `run_discovery` |
| **2026-09-24:** 20 pre-made roles loaded once from a connections export (4,794 LinkedIn ids, ~265 per role, some in two), generic developer JD each; interns/students filed as ruled out. Those refs carry adapter `manual`, which `regate` never re-judges. Separately, and with no tie to that import: after SERP, every search now also offers the people **other roles already found** (any source) whose stored snippet passes this role's gate — `known_candidates`, capped at 100. Backup taken first: `var/pre_csv_seed_backup.sql` | `scripts/seed_connections.py`, `discovery.known_candidates` / `regate` / `run_discovery` |
| **2026-09-18:** The shortlist poll lives inside the polled partial, so a search that starts after page load still streams in and one that finishes stops polling. The running line names the stage and its age | `shortlist.html`, `_shortlist_rows.html`, `matching.role_progress` |

---

## 4. What is NOT capable yet

| Gap | State |
|---|---|
| ~~**No button starts scraping**~~ | **Built 2026-09-02.** "Read them now" on the role page queues an `enrich` job; the five steps in `IMPLEMENTATION.md` §1.11d are all in. Concurrency 1 is now a Postgres advisory lock inside `linkedin_profile.enrich`, so the CLI, the worker and the button cannot overlap on one account. The command panel stays — it is the only route when the app and the signed-in browser are on different machines. Mode C still ships disabled and still needs one `enable` command. |
| **Erasure endpoint** | `IMPLEMENTATION.md` §4.2. Retention deletion is cancelled by owner decision; erasure on request is the individual's right and is not ours to waive. Must exist before anyone else operates this. |
| **The location gate is inert on enriched data** | Every candidate on `d956b79f` reads `pass: location unknown` — mode C stores no location claim, so the gate does nothing after discovery. Nobody slipped through (the *snippet* gate filters geography) but the card shows a gate that never fires. Either populate location or stop displaying it. |
| **Nothing supervises the web process** | It is now the only thing that must stay up. It needs a restart policy in Compose, or a "worker last seen" line on `/admin/health`. `ARCHITECTURE.md` §9a.5 gains that line. |
| **Duplicate detection is per-role only** | **Owner decided 2026-08-28 to leave this.** Do not build it without asking again. |

---

## 5. Measured on live data

`PRD.md` §13.1 holds the full records.

| Measure | Result |
|---|---|
| Mode C dated-experience yield | **95% cumulative (35/37)**, superseding 92% at n=12 |
| Profiles linking a GitHub / personal domain | 1 in 12 — **settled, do not re-report** |
| Mode C walls, challenges, HTTP 999 | **0**, across 40 profiles over four days |

**Roles in the dev database:**

| id | title | refs | passed gate | matches |
|---|---|---|---|---|
| `d956b79f` | Backend Engineer (Bangalore) | 53 | 34 | 34 — **10 ranked · 24 ruled out · 0 unread** |
| `e6c2de35` | Senior Business / Operations Manager | 23 | 11 | 11 — none read yet |
| `2207a9b7` | Python Engineer (demo) | 1 | 1 | 1 — GitHub-verified at 0.768, still the best card to show anyone |
| `4808bcf8` | *(untitled, empty)* | 0 | 0 | 0 |

46 candidates · 517 evidence rows. **Mode C spend: 25 of 30** at session end; the window is rolling,
so it is clear by mid-morning.

**The Bangalore role is fully read.** Its 24 rule-outs are honest — 105 skill rows between them and
not one names Python. They are the Java, C# and Angular people a broad title query pulls in.

**`HANDOFF.md` §6 of 2026-08-28 is closed.** Re-reading the seven thin profiles with the `@2` parser
changed three and left three identical, which is the answer: those sections are empty on LinkedIn.
`chethan-p` lists no skills at all, so his "no evidence for Python" is a true statement about a real
absence. **The `manual_paste` loader is retired** — seven scrapes answered what 28 pasted files would
have.

---

## 6. Bugs found and fixed 2026-08-31

| Bug | Why it mattered |
|---|---|
| **An unresolvable location passed the gate** | The gate had a foreign-country list and an India-only region resolver. "Greater Sydney Area" and "Austin, Texas Metropolitan Area" match neither, and unresolvable meant "cannot disprove, let through" — so an *unknown* place was safer than a *known* one, and Hyderabad was rejected while Sydney was not. 2 of the 11 people shown on the ops role were abroad. Now: a present location naming nowhere in India fails. Blank still passes — inferring a country from a name is the guess this project refuses. |
| **`ranked` counted people nobody had opened** | A `match` row exists from discovery, so the banner read "11 ranked" directly above a section reading "nothing to rank". |
| **The 12 the gate refused were invisible** | The page said "looked at 23 people" and listed 11. An over-tight gate looked like an empty market. |
| **Forgetting the worker looked like a slow search** | "Searching — looked at 0 people" for ten live minutes over a queue nobody was draining. Fixed twice over: the queue runs in-process now, and an unclaimed job is reported as stalled. |
| **The location `<select multiple>`** | Needed Ctrl+click, gave no sign of it, and the refusal named no field. Three submits, read as "it is not working". |

---

## 7. Dev environment

```
Postgres        docker compose up -d      host port 5442, container port 5432
Dev database    hi
Test database   hi_test  (pytest drops/recreates its schema; it will NOT touch `hi`)
Migrations      013 applied — run `python -m hi.db migrate` after pulling
The app         uvicorn hi.web:app --reload      ← this is the whole application now
Worker          optional second drainer: python -m hi.worker
```

`.env` holds live keys and is gitignored. `SERPAPI_KEY`, `ANTHROPIC_API_KEY` and `HI_USERS` are set;
`GITHUB_TOKEN` is not (and is no longer worth chasing). **`.env` is read at startup — restart
uvicorn after editing it**; `--reload` only watches `.py` files.

**Sign-in:** `ayushmaan.singh@habuild.in` / `demo1234`.

**Running mode C** (RUNBOOK entry 9 is the full procedure; the role page prints these for you):

```powershell
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" `
    --remote-debugging-port=9222 --user-data-dir="D:\au\hiring-intelligence\var\edge-automation"
# leave it open, visible, signed in
python -m hi.adapters.linkedin_profile run --role e6c2de35 --limit 5
python -m hi.adapters.linkedin_profile run --role d956b79f --only some-slug,other-slug
```

`python -m hi.adapters.linkedin_profile` on its own prints spend and remaining budget.

---

## 8. Traps already hit — do not rediscover these

| Trap | What happened |
|---|---|
| **Two `pytest` runs at once wipe each other** | `conftest` drops and recreates `hi_test`, so a background suite plus a foreground one produced 32 spurious `relation "skill" does not exist` errors that looked like real failures. Never start a second run while one is going. |
| **Heredocs mangle `\b` into a literal backspace** | Documented twice before and hit again today: `re.compile(r"\bindia\b")` was written to disk as `\x08india\x08`, which silently matches nothing. **Use the editor tools for anything containing a backslash.** |
| **`spent_today()` is a rolling 24h window, not a calendar day** | Check with `python -m hi.adapters.linkedin_profile` before planning a run. |
| **A whole profile capture parses to zero roles** | `_significant_lines` stops at the first "people also viewed" marker, which those pages render *before* the experience section. Slice from the `Experience` heading and refuse to act on a page that parses to nothing. |
| **A gate fix does not fix existing rows** | The verdict is stored on `candidate_ref` at discovery time. `discovery.regate` re-runs it from `snippet_raw` without spending SERP searches — via `scripts/regate_refs.py`, or the "Find more people" button, which does it on every press. |
| `pytest` wiped the dev database | `conftest` now uses `hi_test`, with an assertion refusing to wipe anything else. |
| Gate verdicts drifted between runs | The composed display headline overwrote the real one. `ref_from_row` rebuilds from `snippet_raw` only. |
| "India" as a search term matches "Indonesia" | And "Indiana" is not India. Both are why `_mentions_india` uses word boundaries. |
| SerpAPI key would have landed in Postgres | It travels as a URL query param. `fetcher.redact_url` strips credentials before anything is hashed or stored. |
| `pg_dump`/`psql` are not on the Windows host | Go through `docker compose exec -T postgres`. Port 5442 from the host, 5432 inside. |
| `taskkill /F /IM msedge.exe` kills *all* Edge | Including the signed-in automation window. Never run it while mode C is live. |
| Console output dies on LinkedIn text | Windows `cp1252` cannot encode emoji or `\ufffd`. Encode to ASCII with `errors="replace"` before printing. |

---

## 9. Not built, deliberately

- **`manual_paste`** — retired today, §5.
- **`huggingface`, `codeforces`, package registries** — dropped: too few people on them in this market.
- **`serp_web`** — built, dormant by decision. Migration 013 disables its policy row.
- **Rationale prose** (LLM writing "why this ranking") — the component bars already explain the number.
- **Mode D (`people_search`)** and **mode E (`proxied_public`)** — trigger-gated, not built.
- **Milestone 4** — observability, weight proposals. Erasure (§4.2) is the exception: it is owed.

---

## 10. Open governance items — unchanged, still open

- **Nobody at the receiving company owns `source_policy.reviewed_at`.** `PRD.md` §13.2 calls this the
  one real blocker to hand-off.
- **Mode C is enabled on an HR intern's account.** §7.4 requires that person to have agreed
  explicitly and in advance. Worth confirming a senior is comfortable.
- **Two fixtures contain a real person's profile text**, including bystanders' names from the
  sidebar, and are now committed to git. Worth redacting to synthetic names.

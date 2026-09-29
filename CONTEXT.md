# Context

**Current task:** Milestone 1 complete; today closed the gap between "it works" and "a recruiter can
drive it". The role page now prints the exact enrichment command with the role id, the shared daily
budget and the queue; the job queue runs inside `uvicorn` so one command is the whole app; and the
location gate no longer lets unresolvable foreign places through. **534 tests green, all
uncommitted.**

**Key decisions 2026-08-31** (full records in `ARCHITECTURE.md` §2.6, `IMPLEMENTATION.md` §1.11d)
- **The queue runs in the web process.** The split was tidiness, not correctness, and it cost ten
  live minutes of "Searching — looked at 0 people" over a queue nobody was draining.
- **A present location naming nowhere in India now fails the gate.** Unresolvable used to mean
  "cannot disprove", which made Sydney safer than Hyderabad. Blank still passes — inferring a
  country from a name is the guess this project refuses to make.
- **`manual_paste` is retired.** Seven `--only` scrapes answered what 28 hand-pasted files would have.
- **GitHub/artefact coverage is settled and closed.** Do not re-measure or re-report it.

**Next steps** (`HANDOFF.md` §1 has the full list)
1. **Commit** — 23 modified files plus `scripts/regate_refs.py`, green all day, never recorded.
2. **`regate_refs.py --role e6c2de35 --apply`** — awaiting the owner's yes. Flips 2 foreign
   candidates to `failed`, costs no SERP spend, frees 2 profile reads.
3. **The button** (`IMPLEMENTATION.md` §1.11d). Step 1 is the advisory lock, without which a queued
   job and a CLI run can both attach to one LinkedIn account. Then the **erasure endpoint** (§4.2).

**Gotchas:** two `pytest` runs at once wipe each other's `hi_test` and produce 32 fake errors ·
**heredocs turn `\b` into a literal backspace** — use the editor tools for anything with a backslash ·
`spent_today()` is a **rolling 24h window** (25 of 30 at session end) · Postgres on **5442** from the
host · `.env` is read at startup, so restart uvicorn after editing.

# Runbook

For whoever keeps this running. **You do not need to have read the code.** Each entry is
symptom → check → fix.

Two things to know before anything else:

1. **The health page is the first place to look:** `/admin/health`. A green light means a
   source *produced information recently* — not merely that it did not crash.
2. **Nothing here is urgent.** This tool runs about 30 searches a month. If it is broken
   at 2am, fix it at 9am. There is no on-call.

```
Start:    docker compose up -d && uvicorn hi.web:app && python -m hi.worker
Stop:     Ctrl-C both, then docker compose down
Logs:     whatever the terminal running them prints
```

---

## 1. A search found no candidates

**Check** — open the role and click *"Show the N people we ruled out"*. That list tells you
which test people failed, in plain words.

**Fix, in order of how often it is the answer:**

| What the reasons say | What to do |
|---|---|
| "No public sign of X" on nearly everyone | Too many must-haves. Move all but 2–3 to nice-to-have and search again. |
| "Located outside …" on nearly everyone | The location is too narrow. Add nearby areas, or set the role to fully remote. |
| Mostly title mismatches | Add more job titles. "Backend Engineer" alone misses people who call themselves "Software Engineer". |
| The ruled-out list is *empty too* | Nothing was found at all — go to entry 2. |

**Do not** assume the tool is broken because a search was thin. A tight location or a
starved title list is far more often the cause.

---

## 2. Nothing is found at all — zero people, not zero matches

**Check** `/admin/health`. Look at the `linkedin_serp` row.

- **Red, "Switched off"** → someone disabled it. See entry 4.
- **Amber, "produced no new information"** → the search vendor is answering but we cannot
  read the answer. Run the first diagnostic:

  ```bash
  python scripts/refresh_fixture.py
  ```

  It queries live and prints exactly what stopped parsing. The fix is almost always one
  line in `src/hi/adapters/linkedin_selectors.py`.

- **Amber, "Has never returned anything"** → the API key is missing, wrong, or out of
  quota. Check `SERPAPI_KEY` in `.env` and the balance at
  <https://serpapi.com/dashboard>. Free tier is 250 searches/month and one role uses
  roughly 8–20.

---

## 3. Experience and employment history are missing

**This is expected, not a fault.** The shortlist shows a banner saying so.

The tool does not crawl LinkedIn profile pages: `robots.txt` forbids it and the fetcher
obeys. Left alone, years of experience are estimated from each person's public work and
the candidate card says *"estimated from public work"* wherever that is the case.

**To get real employment history**, someone has to run the enrichment pass by hand —
entry 9. It reads profiles in a browser that person is signed into, 30 a day, and it puts
*their* LinkedIn account at some risk. That is why it is a deliberate act and not
something the tool does on its own.

After a successful pass the card says *"from LinkedIn profile"* instead, and the years
figure is counted from dated jobs rather than estimated.

---

## 4. A source shows red on the health page

**Check** the "State" column — it shows the reason it was switched off.

Sources switch themselves off when they hit a wall (a block, a challenge, a refusal).
That is deliberate: hammering a source that is refusing us turns a soft block into a
permanent one.

**Fix:** deal with whatever the reason says, then switch it back on **by hand**:

```sql
-- connect: docker compose exec postgres psql -U hi -d hi
update source_policy
   set enabled = true, disabled_reason = null, reviewed_at = now()
 where domain = 'serpapi.com';
```

Re-enabling is deliberately manual. If you do not understand why it went off, do not turn
it back on.

---

## 5. "It worked last month and now it returns nothing"

This is the characteristic failure: a website changed its layout, and the tool now reads
zero results from a response that looks fine. **It fails silently** — which is why the
health page distinguishes "produced rows" from "did not crash".

**Check** `/admin/health` for an amber light, then:

```bash
python scripts/refresh_fixture.py
```

**Fix:** the error names the field that changed. Edit
`src/hi/adapters/linkedin_selectors.py`, re-run the script until it prints `OK`, then:

```bash
python scripts/refresh_fixture.py --write   # re-record the test fixture
pytest                                       # confirm nothing else broke
```

If you are not comfortable editing Python, this is the point to call a contractor. It is
a one-file, one-hour job and you can tell them exactly that.

---

## 6. Rotating a key

Every secret lives in `.env`. Nothing is in the code. `.env.example` lists each one, what
it is for, and where to get a new one.

| Key | Where to get a new one | What breaks without it |
|---|---|---|
| `SERPAPI_KEY` | <https://serpapi.com/manage-api-key> | All discovery. Nothing is found. |
| `ANTHROPIC_API_KEY` | <https://console.anthropic.com/settings/keys> | Job descriptions are read less well — the tool falls back to matching words literally and still works. |
| `GITHUB_TOKEN` | <https://github.com/settings/tokens> (no scopes needed) | Verification slows from 5,000 to 60 checks/hour. |
| `HI_USERS` | Generate with `python -m hi.auth hash 'password'` | Nobody can sign in. |
| `DATABASE_URL` | Your Postgres | Everything. |

**To rotate:** edit `.env`, restart the web and worker processes. That is all — no code
change, no migration.

**Never commit `.env`.** It is in `.gitignore`. If a key is ever pasted into a commit,
rotate it rather than trying to rewrite history.

---

## 7. Restore a backup

Backups are written nightly by `scripts/backup.sh` to `$HI_BACKUP_DIR`
(default `/var/backups/hiring-intelligence`).

```bash
# 1. Stop the app and worker (Ctrl-C both).
# 2. Restore into a SCRATCH database first — never straight over the live one.
createdb -h localhost -p 5442 -U hi hi_restore_test
gunzip -c /var/backups/hiring-intelligence/hi-20260826-021500.sql.gz \
  | psql -h localhost -p 5442 -U hi -d hi_restore_test

# 3. Sanity check it actually contains something.
psql -h localhost -p 5442 -U hi -d hi_restore_test \
  -c "select count(*) from candidate; select count(*) from evidence;"

# 4. Only if that looks right, point DATABASE_URL at it in .env and restart,
#    or drop and recreate the real database and restore into that.
```

**Verified 2026-08-27.** A real dump of the development database was restored into a
scratch database and every table matched: `candidate_ref` 54, `candidate` 35,
`evidence` 94, `match` 35, `fetch` 31, `skill` 254, `scoring_weights` 2. Zero errors.
Do it again after any change to the schema or the Postgres version.

### If `pg_dump` and `psql` are not installed

They are not on a typical Windows development machine, and `scripts/backup.sh` and the
commands above both assume them. On the Linux VM they come with Postgres and the
commands work as written. Anywhere else, run them **inside the container**, which needs
no client tools on the host at all:

```bash
# Dump
docker compose exec -T postgres pg_dump postgresql://hi:hi@localhost:5432/hi \
  | gzip > hi-backup.sql.gz

# Restore into a scratch database
docker compose exec -T postgres psql -U hi -d postgres \
  -c "create database hi_restore_test;"
gunzip -c hi-backup.sql.gz | docker compose exec -T postgres psql -q -U hi -d hi_restore_test

# Compare — the numbers must match the live database
docker compose exec -T postgres psql -tA -U hi -d hi_restore_test \
  -c "select count(*) from candidate" -c "select count(*) from evidence"
```

Note the port difference: **5442** from the host, **5432** inside the container.

**Do this once before you rely on it.** An unrestored backup is a rumour.

---

## 8. Turn LinkedIn off entirely and keep working

The tool is designed to survive this. Discovery uses a search engine, not LinkedIn
itself, and verification uses GitHub.

```sql
update source_policy
   set enabled = false, disabled_reason = 'switched off by <your name> on <date>'
 where domain like '%linkedin.com';
```

Searches keep running. The shortlist shows a banner explaining what is unavailable.
Nothing 500s, nothing silently empties.

To turn it back on, set `enabled = true` and clear `disabled_reason`.

---

## 9. Run LinkedIn enrichment (mode C)

**Read this whole entry before running it.** It is the only part of the tool that uses a
real LinkedIn account, and the account it uses may get restricted. That risk lands on a
person, not on the company, so the person whose account it is has to be the one who
decides — and their name is recorded when it is switched on.

**Rules that are enforced in code and cannot be configured away:** 30 profiles a day, one
at a time, at least 20 seconds apart, a visible window (never hidden), and it switches
itself off the moment LinkedIn shows a wall or a challenge. If you find yourself wanting
to raise those numbers, the answer is no — they are what keeps the account usable.

**Never use a second, made-up, or shared LinkedIn account.** It is against LinkedIn's own
rules, it returns *less* data (profile depth depends on account standing), and on one
laptop it puts the real account at more risk, not less. A separate *browser* is fine and
encouraged. A separate *identity* is not.

### One-time, per person

```sh
python -m hi.adapters.linkedin_profile enable
```

It records who ran it (the signed-in Windows user) and prints the one thing that matters:
this uses **your** real LinkedIn account, and that account is the one that carries the
risk. `--by "Some Name"` still works if the person who owns the account is not the person
at the keyboard.

### Each run

1. Start Edge on its own automation profile, with the debug port open. **One line, in
   PowerShell** — the leading `&` is required, and so is `--user-data-dir`: current Edge
   refuses remote debugging on the normal profile, and a separate profile is what §7.4
   wants anyway (a separate *browser* is good hygiene; a separate *account* is banned).

   ```powershell
   & "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" `
       --remote-debugging-port=9222 `
       --user-data-dir="D:\au\hiring-intelligence\var\edge-automation"
   ```

2. **The first time only:** sign in to LinkedIn in that window, with the real account
   named in the `enable` step. The profile persists under `var/`, so later runs open
   already signed in. `var/` is gitignored — the session never lands in the repo.

3. Leave that window open and visible. Do not minimise it to a hidden desktop.
4. Run the pass for one role:

   ```sh
   python -m hi.adapters.linkedin_profile run --role <role-id> --limit 10
   ```

   The verification and re-scoring it queues are picked up by the running web app,
   which drains the queue itself. If the app is not running, `python -m hi.worker`
   does the same job.

   **Or click the button instead.** Open the role in the app, scroll to "Read LinkedIn
   profiles", set how many, and press **Read them now** (built 2026-09-02). Steps 1–3
   above still have to be true first — the button attaches to the *same* Edge window and
   cannot open one for you; if nothing is listening on port 9222 it says so and starts
   nothing. It reads progress back per profile and shows `Stopped:` in place if LinkedIn
   walls it.

   The two routes are the same run through the same guard: a Postgres advisory lock inside
   the adapter means whichever starts second is refused, so it is safe to have the app up
   while you run the command yourself. The panel still prints both commands with this
   role's id in them, shows the shared daily budget, and lists who is next in the order the
   run will actually read them.

   **Use the command, not the button, when** the web app and the signed-in browser are on
   different machines (the browser has to be on the machine running the app), or when you
   want the full run output in front of you — including the `dated-experience yield` line,
   which the panel does not show.

### Re-reading particular people

`--only` names them, and implies `--redo`:

```sh
python -m hi.adapters.linkedin_profile run --role <role-id> --only alice-1234,bob-5678
```

The slugs are the last part of each LinkedIn URL, comma-separated, no spaces. A slug that
matches nobody stops the whole run rather than reading the others — scraping six of the
seven people you named looks exactly like success. Use it after a parser fix, or when a
profile came back thin; without it, re-reading three specific people costs the whole day's
budget, because the run's order is fixed and they are rarely at the front of it.

It takes roughly 25 seconds per profile, on purpose. Ten profiles is about five minutes.

### Reading the result

- `dated-experience yield` is the number that matters — the share of profiles that
  actually carried dated jobs. Write it into `docs/PRD.md` §13.1.
- `STOPPED:` means it hit a wall and switched LinkedIn off. **Do not re-enable it the
  same day.** Wait, use the account normally for a while, and if it happens twice, stop
  using mode C and say so — the tool works without it.
- `attempted 0` with a cap message means today's 30 are spent. That is not a fault,
  and the window is rolling — reads free up gradually, they do not reset at midnight.

### Checking state

```sh
python -m hi.adapters.linkedin_profile           # status and how many are left today
python -m hi.adapters.linkedin_profile disable --reason "why"
```

---

## Routine upkeep

| How often | What |
|---|---|
| Weekly | Glance at `/admin/health`. Everything green? Done. |
| Monthly | Check the SerpAPI quota before it runs out mid-search. |
| Monthly | Check "Saved pages on disk" on the health page. Amber at 80% — delete the oldest folders under `var/cache/`; they are just cached copies and are safe to remove. |
| Once, before hand-off | Restore a backup (entry 7). |

## Things that are working as intended, not faults

- **A candidate with no "Proven" claims.** They exist, we just found no public work.
  That is a real answer and the badges say so.
- **Fewer results than a LinkedIn search.** Deliberate. 20 people worth contacting beats
  5,000 rows.
- **A source going amber right after you enable it.** It has not produced anything *yet*.
- **The tool refusing to record age or photographs.** It does not collect them at all.
  That is not a missing feature.
- **Education showing on the card but not affecting the ranking.** Deliberate, decided
  2026-08-27. A recruiter screening for IIT/IIM can see where someone studied; the score
  never reads it. Graduation years are still not collected, because a graduation year
  dates a person and age is not collected either.

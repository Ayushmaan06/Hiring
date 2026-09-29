# Hiring Intelligence

Internal candidate-sourcing tool. Role requirements in → ranked, evidence-backed shortlist out.

Engineering/data/ML roles first. India-primary. Small internal team, not a platform.

## Docs

| File | What it is |
|---|---|
| [`CONTEXT.md`](CONTEXT.md) | Where the work stopped and what's next. **Read first when resuming.** |
| [`docs/HANDOFF.md`](docs/HANDOFF.md) | Full state, decisions already made, and the traps already hit. |
| [Handover](#handover--running-the-tool) | **At the bottom of this file** — setup, the Edge command, the demo script, day-to-day use. Start here if you are operating it, not building it. |
| [`RUNBOOK.md`](RUNBOOK.md) | Symptom → check → fix, for whoever keeps it running. |
| [`docs/PRD.md`](docs/PRD.md) | What we're building, for whom, where the data comes from, how ranking works, and how success is measured. **Start here** — §1–§7 assume no technical knowledge. |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | The design. Authoritative. |
| [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) | Dependency-ordered build plan, definition-of-done per step. |
| [`AGENTS.md`](AGENTS.md) | Where LLMs may and may not act. |
| [`CLAUDE.md`](CLAUDE.md) | Working rules for coding agents in this repo. |
| [`Project-Doc.md`](Project-Doc.md) | Original proposal. **Historical only** — superseded by the architecture doc. |

## The idea in one paragraph

A profile tells you who to *consider*; an artefact tells you whether the profile is *true*. So we
discover candidates from LinkedIn — which is where employment history actually lives, and what a job
description actually screens on — then verify those claims against public artefacts: commits,
packages, model cards, contest ratings, conference talks. Every claim about a candidate is an
`evidence` row carrying a source URL, a verbatim snippet, and a confidence tier, and the tiers never
merge, so "says they know Python" never reads like "has eight years of public Python". Scoring is a
deterministic weighted sum of five allowlisted components; an LLM writes the explanation, never the
number.

The source ordering was reversed on 2026-08-25 — artefacts first was the original plan and it could
not answer a JD. The reasoning is recorded in [`docs/ARCHITECTURE.md` §2.1](docs/ARCHITECTURE.md).

The recruiter-facing UI is four screens, built for non-technical users:
[`docs/IMPLEMENTATION.md` §1.11](docs/IMPLEMENTATION.md). A shareable product overview for both
audiences lives in [`docs/prd.html`](docs/prd.html).

## Quickstart

```bash
uv sync
docker compose up -d              # Postgres 16
python -m hi.db migrate
uvicorn hi.web:app --reload       # web
# The queue runs inside the web process. `python -m hi.worker` is optional.
pytest                            # no live network; adapters use fixtures
```

Milestone 1 is **complete**, LinkedIn enrichment included. Done: 1.1 skeleton · 1.2 schema ·
1.3 fetch layer · 1.4 canon · 1.4b SERP discovery + snippet gate · 1.4c LinkedIn enrichment (mode C,
attached browser) · 1.5 GitHub verifier · 1.6 evidence writer · 1.7 identity · 1.8 JD intake ·
1.9 scoring · 1.10 worker · 1.11 recruiter UI · 1.11a health page · 1.11b degraded mode ·
1.11c runbook and backups · 1.11d read-profiles button · 1.11e search deeper · 1.12 access control.

Enrichment reads profiles through a browser the operator has already signed into — never a stored
credential, never a fabricated account, 30 profiles a day, and it disables itself on the first wall
([`docs/ARCHITECTURE.md` §7.4](docs/ARCHITECTURE.md)). It ships **switched off**. The product runs
without it: experience is estimated from public work and the UI says so.

## Running it

```bash
uv sync
docker compose up -d                    # Postgres 16 on host port 5442, not 5432
python -m hi.db migrate
python -m hi.auth hash 'a-password'     # put the result in HI_USERS in .env
uvicorn hi.web:app --reload             # web  -> http://127.0.0.1:8000
# The queue runs inside the web process. `python -m hi.worker` is optional.
pytest                                  # 576 tests, no live network
```

Secrets go in `.env` (gitignored) — copy `.env.example`, which lists every key, what it is for,
and where to get a new one. `HI_USERS` and `SERPAPI_KEY` are required; `ANTHROPIC_API_KEY` and
`GITHUB_TOKEN` are optional and degrade gracefully without.

**Operations:** [`RUNBOOK.md`](RUNBOOK.md) — symptom → check → fix, written for a generalist who
has never read the code. First diagnostic for anything scraping-related is
`python scripts/refresh_fixture.py`.

---

# Handover — running the tool

For the person operating it. Written for someone who has never opened the code and does not intend
to; everything here is copy-paste. [`RUNBOOK.md`](RUNBOOK.md) is the longer, symptom-first version
for when something breaks, and [`docs/handover.html`](docs/handover.html) is the same content as a
page you can send someone.

Two things to know before anything else:

1. **The tool reads LinkedIn profiles through your own signed-in browser window.** Not a fake
   account, not a stored password — a real Edge window you open and sign into yourself. So that
   window has to be open on the machine running the tool, and **your** account is the one that
   carries the risk. It is capped at 30 profiles per rolling 24 hours and paced at ~25 seconds each
   for exactly that reason.
2. **It finds fewer people than a LinkedIn search, on purpose.** Twenty people worth contacting
   beats five thousand rows. If a search returns twelve people, that is not a fault.

## Setting it up on this machine

Once. About half an hour, most of it downloads.

**1. Install these**

| What | Where | Note |
|---|---|---|
| Docker Desktop | docker.com | Must be **running** before the tool starts — look for the whale in the tray |
| Python 3.13 | python.org | Tick "Add to PATH" during install |
| `uv` | `pip install uv` | Installs the tool's dependencies |
| Git | git-scm.com | To get the code |
| Microsoft Edge | Already on Windows | The automation window |

**2. Get the code**

```powershell
git clone <repo-url> D:\hiring-intelligence
cd D:\hiring-intelligence
uv sync
```

**3. Start the database**

```powershell
docker compose up -d
python -m hi.db migrate
```

`migrate` prints the list of files it applied. If it hangs, Docker Desktop is not running yet.

**4. Make your login**

```powershell
python -m hi.auth hash 'pick-a-real-password'
```

Copy the line it prints.

**5. Fill in `.env`**

Copy `.env.example` to `.env` and open it in Notepad — it explains every line and where to get every
key. `HI_USERS` (your email, a colon, then the hash from step 4) and `SERPAPI_KEY` are required;
`ANTHROPIC_API_KEY` and `GITHUB_TOKEN` are optional and degrade gracefully without.

`.env` is read **once at startup**, so restart the app after changing it or you will be looking at
the old value.

**6. Turn LinkedIn reading on**

```powershell
python -m hi.adapters.linkedin_profile enable
```

It ships switched off. This is the one command that says "yes, use my real account", and it records
who ran it. Read what it prints.

**7. Start the tool**

```powershell
uvicorn hi.web:app
```

Open <http://127.0.0.1:8000> and sign in with the email and password from steps 4–5. That one
command is the whole application — the background work runs inside it. **Leave that window open
while you use the tool;** closing it stops everything mid-search.

## Reading LinkedIn profiles — the Edge command

This is the part people get wrong, so it is its own section. **From the repository folder**, in
PowerShell — one line:

```powershell
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --remote-debugging-port=9222 --user-data-dir="$PWD\var\edge-automation"
```

- The leading `&` is required, and `$PWD` only resolves correctly if you are **in the repository
  folder** — `cd D:\hiring-intelligence` first. A full path works just as well if you prefer.
- If that path errors, Edge is in the other standard location: drop the ` (x86)`.
- `--user-data-dir` is required too: Edge refuses remote debugging on your normal profile, and a
  separate browser profile is what we want anyway.
- **First time only:** sign in to LinkedIn in that window. It stays signed in afterwards — the
  profile lives under `var/`, which is gitignored, so the session never reaches the repository.
- Leave it open and **visible** while reading. Do not minimise it to a hidden desktop.
- Then press **Read them now** on a role page, or run the command that same panel prints.

**Never sign a made-up, second, or shared account into that window.** It is banned, it is the fact
pattern that gets companies sued rather than blocked, and it returns *less* data — LinkedIn shows
less history to low-standing accounts. A separate *browser* is good hygiene; a separate *identity* is
not.

## Working, day to day

1. Start Docker Desktop, or leave it running at login.
2. `docker compose up -d`
3. `uvicorn hi.web:app`
4. Open the Edge window above when you intend to read profiles.
5. Work in the browser: **New role** → paste the job description → confirm → wait →
   **Read them now** → shortlist.

**The rhythm the budget forces.** Thirty reads per rolling 24 hours, shared across every role. A
role with forty people found cannot be fully read in one day, and that is the intended shape: read
the top ten, look at the ranking, decide whether the must-have skills were right, and only then
spend more. If the top ten are all wrong, editing the role beats reading thirty more people.

**Two buttons worth knowing.** *Read them now* reads the next few profiles through the Edge window,
and refuses in a sentence if the window is shut, the budget is spent, or a read is already running.
*Find more people* searches one page deeper **and** re-checks everyone already found against the role
as it stands — the re-check is free and instant, the deeper search costs searches and a few minutes.

**Weekly:** glance at `/admin/health`. Green means it has actually produced something recently, not
merely that nothing crashed. **Monthly:** check the SerpAPI quota before it runs out mid-search.

## Demonstrating it end to end

About twelve minutes. The only thing that goes wrong live is timing — reading profiles takes ~25
seconds each, so a role started from scratch has no ranked list for several minutes. Show two roles:
one you prepared, one you run live.

**Before they arrive:** start everything and sign in · open the Edge window and check LinkedIn is
signed in · have **one finished role** ready, searched *and* with 8–10 profiles already read · have a
fresh job description in a text file to paste · check `/admin/health` is green and note how many
reads are left today. If it says 0, the live read below will refuse.

| | What you do | What to say |
|---|---|---|
| **A** | Nothing — no screen | "A job description goes in, a ranked shortlist comes out, and every line about a person has a link to where we read it. It does not guess. If it cannot show you the source, it does not make the claim." |
| **B** | **New role** → paste → **Read it**. Point at the filled-in form | "It read the description and filled this in. It is a draft — I get the final say, and nothing searches until I confirm. Locations are checkboxes: it will not go looking in a city I did not tick." Then **Find candidates** |
| **C** | The page fills in on its own. Scroll to "N we did not open" and read one reason aloud | "It is searching LinkedIn the way you or I would from Google — public results only, no account at this step. Each result gets a first pass: right city, right kind of role. The ones that fail are listed too, with the reason, so you can see when I have been too strict." |
| **D** | **Read LinkedIn profiles** → set the count to **2** → **Read them now** | "This is the rationed part. Thirty reads per 24 hours, shared across every role. It is deliberately slow, about 25 seconds a person, because that is what keeps my account healthy. It is reading through that Edge window — one profile at a time, and it tells me who it is on. If LinkedIn shows it a wall it stops, switches itself off, and says so here. It will never quietly return nothing and look successful." |
| **E** | Open the prepared role. Point at the three groups, then open a person's card | "Three groups: checked and matched, checked and ruled out, and *nobody has read them yet*. That third one used to say 'no sign of Python' about people we never opened. Every line on this card has a source link and the exact words we read — and 'Says so' is what someone wrote about themselves, 'Proven' is what we verified in their public work. Those two never get added together. The ranking itself is arithmetic: five things, fixed weights, same answer every time. The sentence explaining it is written by an AI; the AI never touches the number." |
| **F** | Go back to the live role — the two profiles have folded into the ranking | "It will not collect age, gender, caste, religion, marital status or photographs. Not 'collects but ignores' — it does not read them at all. And it never stores anyone's email in plain text. We identify people; you contact them the normal way." |

**Not in a demo:** do not start a read with a big number to look impressive (it is minutes of
watching a progress line), do not re-read the same people to fill time (it spends the budget for
nothing), and if it says **Stopped**, move to the prepared role — never turn it back on to try again
in front of an audience.

## When something looks wrong

| What you see | What it means | What to do |
|---|---|---|
| **Stopped: …** on the reading panel | LinkedIn showed a wall and reading switched itself off | **Do not turn it back on today.** Use LinkedIn normally for a while. If it happens twice, stop using the reading feature and say so — the tool works without it (`RUNBOOK.md` §8) |
| "The LinkedIn window is not open" | That Edge window is closed, or is on a different machine from the tool | Open it, then press the button again |
| "All 30 profile reads are used up" | Normal | Nothing. It frees up gradually and the page names the time — a rolling window, not a midnight reset |
| "Searching…" forever | The window running `uvicorn` was closed | Restart `uvicorn hi.web:app` |

**Do this once:** back up, and then *restore* that backup into a scratch database. `RUNBOOK.md` §7
has the commands, including the version that runs inside Docker so you need no Postgres tools
installed. An unrestored backup is a rumour.

## What it refuses to do, and why that is not a bug

- **No age, gender, caste, religion, marital status or photographs.** Never collected, so they can
  never leak into a ranking.
- **No plain-text email addresses.** Stored as a one-way hash only.
- **No second LinkedIn account, no proxies, no captcha solving.** If a source needs those, we drop
  the source.
- **The caps live in code, not in a settings file.** Thirty a day, one at a time, twenty seconds
  apart, never a hidden window. Not adjustable by design — they are what keeps the account usable.
- **It will not merge "says they know Python" with "has eight years of public Python."** Different
  facts, kept visibly different all the way to the screen.

If someone asks for one of these to change, that is a conversation with a decision recorded in
`docs/ARCHITECTURE.md` — not a settings change.

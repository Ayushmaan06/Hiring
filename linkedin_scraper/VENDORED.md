# Vendored dependency — provenance

This directory is a **vendored copy**, committed as ordinary files rather than a submodule.

| | |
|---|---|
| Upstream | https://github.com/joeyism/linkedin_scraper.git |
| Commit vendored from | `b1cdc1c0e85bee8764d62565d229c682e5eb81bb` ("upped version") |
| Vendored on | 2026-08-28 |
| Licence | see `LICENSE` in this directory |

## Why it is committed as files, not a submodule or a PyPI pin

`pyproject.toml` in the repo root declares it as a **local editable path dependency**:

```toml
linkedin-scraper = { path = "linkedin_scraper", editable = true }
```

so this directory *is* the dependency — `uv sync` installs from here, not from PyPI. It was
originally a nested git clone, which git records as a gitlink storing **no file contents**: a fresh
clone of the outer repo would have found an empty directory here and `uv sync` would have failed.
The local modification below was also untracked by the outer repo and would have been lost outright.

The original clone's `.git` directory is preserved at `var/vendor-history/linkedin_scraper.git`
(gitignored, local only). Upstream history is recoverable from `origin` at the SHA above.

## Local modifications

Anything changed here must be listed, or the next person cannot tell our code from upstream's.

### 1. `linkedin_scraper/scrapers/person.py` — `_get_accomplishments`

Trimmed the accomplishment sections from eight to three.

Upstream walks eight sections, which is eight page loads per candidate on top of the profile,
experience and education pages. Mode C's premise is human pace (`docs/ARCHITECTURE.md` §7.4), and
eleven loads per person is a burst, not a person. Dropped:

- `honors`, `courses`, `languages`, `organizations` — no bearing on the score, and the score is the
  only reason we read anything.
- `projects` — GitHub covers projects as `artifact_backed` evidence, which is strictly better than
  the same claim `self_reported` on a profile.

Kept: `certifications`, `publications`, `patents`.

**Currently dead code.** `hi.adapters.linkedin_profile.scrape_profile` does not call
`_get_accomplishments` at all — accomplishments were authorised on 2026-08-27 and dropped the same
day, because their parser was as dead as the rest of the vendored getters and their page has no date
line to anchor a text parse on. The modification is kept so that re-enabling the section does not
silently reintroduce eight page loads.

## What must not be ported

`linkedin_scraper/core/auth.py` reads `LINKEDIN_EMAIL` / `LINKEDIN_PASSWORD` from `.env`. **Stored
credentials are banned** (`CLAUDE.md`, `docs/ARCHITECTURE.md` §7.4). Mode B needs no auth and mode C
attaches over CDP to a browser a human already signed in, so nothing in this repo may import it.

## What upstream is still used for

Navigation, scrolling and the login check only. **Its parsing is dead** against LinkedIn's current
obfuscated markup — verified live 2026-08-27, every vendored getter returned empty, which is how a
run once reported five profiles enriched and zero evidence. `hi.adapters.linkedin_profile` parses
page *text* instead, and substitutes its own wall detector for `check_rate_limit`, whose loose
phrase list ("try again later") matches ordinary LinkedIn pages.

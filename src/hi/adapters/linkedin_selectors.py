"""Every brittle external-shape assumption about LinkedIn data, in one file.

ARCHITECTURE.md §9a.3.3. When a source changes its shape, this is the only file that
should need editing, and the fix should be obvious in review. Nothing here is imported
for its logic — these are constants that the adapters read.

**First diagnostic when something breaks: `python scripts/refresh_fixture.py`.** It
re-records live data and fails loudly with the parse error.
"""

# --------------------------------------------------------------------------
# Mode A — SERP field paths (currently the only live LinkedIn-derived source)
# --------------------------------------------------------------------------
# These are not CSS selectors, but they are the same class of thing: an external
# JSON shape we do not control and that will change without warning. Keeping them
# here means a SerpAPI response change is a one-file diff too.

SERP_RESULTS_KEY = "organic_results"
SERP_TITLE_KEY = "title"
SERP_LINK_KEY = "link"
SERP_SNIPPET_KEY = "snippet"

# rich_snippet.top.extensions -> [location, current_title, current_employer]
SERP_RICH_KEY = "rich_snippet"
SERP_RICH_SECTION = "top"
SERP_RICH_EXTENSIONS = "extensions"

SERP_ERROR_KEY = "error"


# --------------------------------------------------------------------------
# Mode B — logged-out profile DOM selectors
# --------------------------------------------------------------------------
# NOT BUILT. LinkedIn's robots.txt is `Disallow: /` for every user agent, so mode B
# does not run — see docs/PRD.md §13.1 for the measurement and the options.
#
# When and if mode B is built, every selector it uses goes here and nowhere else:
#
#   PROFILE_NAME = "h1.top-card-layout__title"
#   EXPERIENCE_SECTION = "section.experience"
#   EXPERIENCE_ENTRY = "li.experience-item"
#   EXPERIENCE_TITLE = "h3.experience-item__title"
#   EXPERIENCE_EMPLOYER = "h4.experience-item__subtitle"
#   EXPERIENCE_DATES = "p.experience-item__duration"
#
# Those names are illustrative, not verified — do not ship them without checking
# against a freshly recorded fixture.
PROFILE_SELECTORS: dict[str, str] = {}

# --------------------------------------------------------------------------
# Mode C — attached-browser profile DOM
# --------------------------------------------------------------------------
# Mode C's DOM selectors are NOT here, and that is deliberate rather than an
# oversight. It parses through the vendored `linkedin_scraper` package, so the
# selectors are that package's, in:
#
#     linkedin_scraper/linkedin_scraper/scrapers/person.py
#
# Duplicating them here would give two places to edit and one of them would go
# stale. The one-file-diff rule still holds — it just points at that file for
# mode C markup, and at this file for everything we parse ourselves.
#
# When LinkedIn changes its profile markup, the symptom is `enrich` returning
# rows with empty `experiences[]` while the page clearly shows jobs. Fix it in
# the vendored scraper, and bump EXTRACTOR_VERSION in linkedin_profile.py so
# the new output is distinguishable from the old.

# Markers that mean "we were served a wall, not a profile". A run that sees one of
# these must stop and go red rather than record an empty result as success.
BLOCK_MARKERS = (
    "authwall",
    "Join LinkedIn",
    "Sign in to see",
    "Please verify you are a human",
)

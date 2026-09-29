"""Recruiter UI — four screens (IMPLEMENTATION.md 1.11).

Server-rendered Jinja + HTMX. No build step, no npm, no JavaScript framework.

The language rules are enforced in review and by a lint test: no jargon reaches a
recruiter-facing string. The words are *proven*, *says so*, *source*, *ruled out*,
*match*. And the number is never shown bare — a label plus a bar, with the raw value
on hover for whoever wants it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from hi import auth, discovery, matching, roles, worker
from hi.adapters import linkedin_serp
from hi.config import settings
from hi.models import RoleSpec, Seniority

TEMPLATES = Path(__file__).parent / "templates"
# The repository root, so the panel can print the Edge command with a path that is true on
# whichever machine this is running on. It used to be the author's path, hardcoded in the
# template, which was wrong for every other clone.
REPO_ROOT = Path(__file__).resolve().parents[3]
templates = Jinja2Templates(directory=str(TEMPLATES))

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """Run the job queue inside the web process.

    It used to be a second process you had to remember to start, and forgetting it did
    not look like an error: the page said "Searching — looked at 0 people" over a queue
    nobody was draining, for as long as you were willing to watch it. A recruiter who
    clicks "Find candidates" is asking for a search, not for a search to be queued.

    Two drainers are harmless — `claim()` uses `FOR UPDATE SKIP LOCKED`, which is the
    whole reason the queue is a table — so `python -m hi.worker` still works alongside
    this, and `INLINE_WORKER=false` turns it off for a deployment that wants the processes
    split. (The env var has no `HI_` prefix — only `HI_USERS` does, because that is the
    field's own name. `HI_INLINE_WORKER` silently does nothing.)

    It runs on its own thread with its own event loop, not as a task on uvicorn's. That
    is not tidiness: `uvicorn --reload` on Windows hands the server a
    `SelectorEventLoop`, which cannot spawn a subprocess, so Playwright's driver dies
    with a bare `NotImplementedError` and every LinkedIn read fails while the same job
    run from `python -m hi.worker` succeeds. A fresh loop gets the platform default —
    Proactor on Windows — and jobs behave the same however they were started.
    """
    if not settings.inline_worker:
        yield
        return

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="hi-worker", daemon=True)
    thread.start()
    future = asyncio.run_coroutine_threadsafe(
        worker.run_forever(f"web:{os.getpid()}", handle_signals=False), loop
    )
    try:
        yield
    finally:
        future.cancel()
        with contextlib.suppress(Exception):
            future.result(timeout=5)  # let the cancellation land before the loop stops
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)  # daemon, so a job mid-flight cannot block shutdown


app = FastAPI(title="Hiring Intelligence", lifespan=lifespan)

# Everything else requires a session — including the admin page. There is no
# anonymous read of candidate data anywhere.
PUBLIC_PATHS = {"/login", "/healthz"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)

    actor = auth.actor_for(request.cookies.get(auth.COOKIE_NAME))
    if actor is None:
        if request.method == "GET":
            return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
        # A mutation with no session is an error, not a redirect — HTMX would
        # otherwise swap a login page into the middle of the shortlist.
        return Response("Please sign in again.", status_code=401)

    request.state.actor = actor
    return await call_next(request)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/", error: str | None = None):
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": next, "error": error, "no_users": not auth.configured_users()},
    )


@app.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    actor = auth.authenticate(email, password)
    if actor is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": next, "error": "That email and password did not match.", "no_users": False},
            status_code=401,
        )

    token = auth.create_session(actor)
    # Only redirect to our own paths — an open redirect here is a phishing gift.
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        auth.COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        max_age=auth.SESSION_HOURS * 3600,
        path="/",
    )
    return response


@app.post("/logout")
def logout(request: Request):
    auth.destroy_session(request.cookies.get(auth.COOKIE_NAME))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return response


# --------------------------------------------------------------------------
# presentation helpers — the score never reaches a template as a bare number
# --------------------------------------------------------------------------

MATCH_LABELS = ("Strong match", "Good match", "Possible match", "Weak")


def match_label(value: float | None) -> str:
    if value is None:
        return "Not yet ranked"
    if value >= settings.match_strong_min:
        return "Strong match"
    if value >= settings.match_good_min:
        return "Good match"
    if value >= settings.match_possible_min:
        return "Possible match"
    return "Weak"


def match_segments(value: float | None, total: int = 5) -> int:
    """Filled segments of the five-segment bar."""
    if not value:
        return 0
    return max(1, min(total, round(value * total)))


COMPONENT_LABELS = {
    "skill_match": "Skill match",
    "skill_depth": "Depth of evidence",
    "seniority_fit": "Experience fit",
    "activity_recency": "Recent activity",
    "availability": "Availability signal",
}

LINK_LABELS = {
    "github_login": "GitHub",
    "gitlab_login": "GitLab",
    "linkedin_slug": "LinkedIn",
    "personal_domain": "Website",
    "hf_user": "Hugging Face",
    "codeforces_handle": "Codeforces",
    "so_user_id": "Stack Overflow",
}


def link_url(kind: str, value: str) -> str:
    return {
        "github_login": f"https://github.com/{value}",
        "gitlab_login": f"https://gitlab.com/{value}",
        "linkedin_slug": f"https://linkedin.com/in/{value}",
        "personal_domain": f"https://{value}",
        "hf_user": f"https://huggingface.co/{value}",
        "codeforces_handle": f"https://codeforces.com/profile/{value}",
    }.get(kind, "")


def plain_reason(reason: str) -> str:
    """Turn an internal gate reason into something a recruiter can read."""
    text = reason.split(":", 1)[-1].strip() if ":" in reason else reason
    replacements = {
        "no evidence for": "No public sign of",
        "outside": "Located outside",
        "most recent public work": "Most recent public work is",
    }
    for needle, plain in replacements.items():
        text = text.replace(needle, plain)
    return text[:1].upper() + text[1:] if text else "No reason recorded"


templates.env.globals.update(
    match_label=match_label,
    match_segments=match_segments,
    component_labels=COMPONENT_LABELS,
    link_labels=LINK_LABELS,
    link_url=link_url,
    plain_reason=plain_reason,
)


# --------------------------------------------------------------------------
# Screen 1 — Roles
# --------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        request, "roles.html", {"roles": matching.roles_overview()}
    )


# --------------------------------------------------------------------------
# Screen 2 — New role, two steps on one page
# --------------------------------------------------------------------------


@app.get("/roles/new", response_class=HTMLResponse)
def new_role(request: Request):
    return templates.TemplateResponse(request, "new_role.html", {})


@app.post("/roles/read", response_class=HTMLResponse)
def read_jd(request: Request, jd_text: str = Form("")):
    draft = roles.draft_from_jd(jd_text)
    return templates.TemplateResponse(request, "role_form.html", _form_context(draft, jd_text))


def _form_context(draft, jd_text: str, title: str | None = None) -> dict:
    return {
        "draft": draft,
        "jd_text": jd_text,
        "title": title,
        "all_regions": _region_choices(),
        # Highlight the field, not just the banner at the top of a long form.
        "needs_location": roles.BLOCK_NO_LOCATION in draft.blocking,
    }


def _region_choices() -> list[tuple[str, str]]:
    """Region codes never reach the recruiter — they pick names."""
    from hi import canon

    pretty = {
        "IN-DL-NCR": "Delhi NCR",
        "IN-KA-BLR": "Bangalore",
        "IN-MH-MUM": "Mumbai",
        "IN-MH-PUN": "Pune",
        "IN-TG-HYD": "Hyderabad",
        "IN-TN-CHE": "Chennai",
        "IN-WB-KOL": "Kolkata",
        "IN-GJ-AHM": "Ahmedabad",
        "IN-RJ-JAI": "Jaipur",
        "IN-KL-KOC": "Kochi",
        "IN-REMOTE": "Anywhere in India",
    }
    return [(code, name) for code, name in pretty.items() if canon.region_cities(code)]


@app.post("/roles")
def create_role(
    request: Request,
    title: str = Form(...),
    jd_text: str = Form(""),
    must_have: str = Form(""),
    nice_to_have: str = Form(""),
    titles: str = Form(""),
    min_years: str = Form(""),
    max_years: str = Form(""),
    remote: str = Form("onsite"),
    locations: list[str] = Form(default=[]),
    pages: int = Form(linkedin_serp.FIRST_SEARCH_PAGES),
):
    def split(value: str) -> list[str]:
        return [v.strip() for v in value.split(",") if v.strip()]

    spec = RoleSpec(
        titles=split(titles) or [title],
        must_have_skills=split(must_have),
        nice_to_have_skills=split(nice_to_have),
        seniority=Seniority(
            min_years=int(min_years) if min_years.strip().isdigit() else None,
            max_years=int(max_years) if max_years.strip().isdigit() else None,
        ),
        locations=locations,
        remote=remote if remote in {"onsite", "hybrid", "remote", "remote_relocate"} else "onsite",
    )

    warnings, blocking = roles.review(spec)
    if blocking:
        draft = roles.RoleDraft(spec=spec, source="empty", warnings=warnings, blocking=blocking)
        return templates.TemplateResponse(
            request, "role_form.html", _form_context(draft, jd_text, title), status_code=422
        )

    role_id = roles.confirm(title, spec, jd_text=jd_text, actor=request.state.actor)
    depth = max(1, min(pages, linkedin_serp.MAX_PAGES))
    worker.enqueue("discover", {
        "role_id": str(role_id), "pages": depth,
        "max_queries": linkedin_serp.FIRST_SEARCH_QUERIES,
    })
    return RedirectResponse(f"/roles/{role_id}", status_code=303)


# --------------------------------------------------------------------------
# Screen 3 — Shortlist
# --------------------------------------------------------------------------


@app.get("/roles/{role_id}", response_class=HTMLResponse)
def shortlist(
    request: Request,
    role_id: uuid.UUID,
    show_ruled_out: bool = False,
    more: str | None = None,
    rechecked: int = 0,
    reading: str | None = None,
):
    role = matching.spec_of(role_id)
    if role is None:
        return HTMLResponse("<h1>We couldn't find that role.</h1>", status_code=404)
    # The collection panel is built here and *not* in `_shortlist_context`, because that
    # context is also the HTMX polling target and this costs a full scan of `evidence`.
    context = _shortlist_context(role_id, show_ruled_out)
    context.update(_collect_context(role_id))
    context.update(_search_context(role_id, role))
    # What "find more people" just did, carried across the redirect rather than held in
    # the session: a refresh must not re-run it.
    context.update(more=more if more in MORE_OUTCOMES else None, rechecked=rechecked)
    context.update(reading=reading if reading in READING_REFUSALS else None)
    return templates.TemplateResponse(request, "shortlist.html", context)


MORE_OUTCOMES = ("started", "busy", "deepest")


def _search_context(role_id: uuid.UUID, spec) -> dict:
    """How wide and how deep this role has been searched, for the "find more" panel.

    Depth is the only lever discovery has. The queries are fixed by the job titles and
    the must-haves; going wider means editing those and starting again, which is a
    different (and much larger) button.
    """
    depth = discovery.search_depth(role_id)
    planned = len(linkedin_serp.plan(spec))
    ran = min(discovery.queries_run(role_id), planned) or planned  # 0: predates role_query
    return {
        "search_depth": depth,
        "search_queries": ran,
        "search_at_max": depth >= linkedin_serp.MAX_PAGES,
        # "Find more" runs every planned query to depth+1. Pages already fetched in the
        # last 24h come from the fetch cache, so: one new page per query already run, and
        # every page for a query never run. ponytail: assumes the cache is warm; a role
        # last searched over a day ago re-pays its earlier pages (planned * (depth+1)).
        "find_more_cost": ran + (planned - ran) * (depth + 1),
    }


@app.post("/roles/{role_id}/find-more")
def find_more(request: Request, role_id: uuid.UUID):
    """"Not enough people?" — search one page deeper, and re-check everyone already found.

    Two separate things, deliberately in one button, because a recruiter looking at a
    thin list cannot tell which of the two they need:

    - **Deeper.** Each planned search goes one more page into the results. This costs
      SERP searches and takes minutes, so it goes through the queue like any other run.
    - **Re-checked.** The verdict on every person already found is recomputed against
      the role as it stands now. Free, instant, and the fix for the run that let two
      people abroad through before the location rule was tightened.
    """
    spec = matching.spec_of(role_id)
    if spec is None:
        return HTMLResponse("<h1>We couldn't find that role.</h1>", status_code=404)

    def back(outcome: str, rechecked: int = 0) -> RedirectResponse:
        return RedirectResponse(
            f"/roles/{role_id}?more={outcome}&rechecked={rechecked}", status_code=303
        )

    # Two searches at once on one role would pay twice for the same pages.
    if matching.role_progress(role_id)["running"]:
        return back("busy")

    rechecked = len(discovery.regate(role_id, spec))
    depth = discovery.search_depth(role_id) + 1
    if depth > linkedin_serp.MAX_PAGES:
        # Still worth re-scoring: the re-check above may have moved people either way.
        worker.enqueue("score", {"role_id": str(role_id)})
        return back("deepest", rechecked)

    worker.enqueue("discover", {"role_id": str(role_id), "pages": depth})
    worker.enqueue("score", {"role_id": str(role_id)})
    return back("started", rechecked)


def _collect_context(role_id: uuid.UUID) -> dict:
    """What to run to read these people's LinkedIn profiles, and who is still waiting.

    Reading a profile is rationed at 30 a day across *every* role, so the panel has to
    show the budget as one shared pot: 10 spent here are 10 unavailable to the next role.
    """
    from hi.adapters import linkedin_profile as lp

    waiting = lp.waiting_for_role(role_id)
    todo = [p for p in waiting if not p["enriched"]]
    # The people the snippet gate refused before reading anything. A recruiter who thinks
    # the gate was too tight can spend their own quota on them, so the count has to be on
    # the page next to the button that does it.
    refused = [p for p in lp.waiting_for_role(role_id, "failed") if not p["enriched"]]
    remaining = lp.budget_remaining()
    frees_at = lp.budget_frees_at() if not remaining else None
    job = worker.latest_job("enrich", role_id)
    if job and job["state"] == "failed" and not (job["note"] or "").startswith("Stopped"):
        # A job that exhausted its attempts leaves its last progress line behind, and
        # "Reading 2 of 8" over a dead run reads as success. The note is what the page
        # shows, so the correction belongs here rather than in the template.
        job["note"] = "Stopped: the run hit an error and did not finish."
        # A crash is not a wall. Standing down for a day is the right advice when
        # LinkedIn blocked us and the wrong advice when our own process broke.
        job["retryable"] = True
    return {
        "role_id": role_id,
        # A read in flight for this role, and whether it is still going. `note` is the
        # per-profile line the handler writes, and the place a "Stopped:" ever appears.
        "reading_job": job,
        "reading_now": bool(job and job["state"] in ("queued", "running")),
        "reading_off": lp.policy_blocked() is not None,
        "reading_ready": lp.browser_attached(),
        # A rolling window, so the honest answer is a time, never "tomorrow".
        "collect_frees_at": frees_at.strftime("%H:%M") if frees_at else None,
        "collect_todo": todo,
        "collect_done": [p for p in waiting if p["enriched"]],
        "collect_budget": remaining,
        "collect_cap": lp.MAX_PROFILES_PER_DAY,
        # The default asks for what is both wanted and affordable — never a number the
        # run would silently truncate.
        "collect_default": min(len(todo), remaining),
        "collect_refused": refused,
        "collect_refused_default": min(len(refused), remaining),
        "collect_all_default": min(len(todo) + len(refused), remaining),
        "collect_role_ref": str(role_id)[:8],
        "collect_profile_dir": str(REPO_ROOT / "var" / "edge-automation"),
    }


# Why a click was refused. The panel turns each of these into a sentence — the endpoint
# never composes prose, so there is exactly one place to read what a recruiter is told.
READING_REFUSALS = ("started", "off", "spent", "no-browser", "busy", "nobody")


@app.post("/roles/{role_id}/stop-search")
def stop_search(request: Request, role_id: uuid.UUID):
    """Stop spending SERP credits on this role. Whoever was already found stays."""
    worker.stop_discovery(role_id, actor=request.state.actor)
    return RedirectResponse(f"/roles/{role_id}?more=stopped", status_code=303)


@app.post("/roles/{role_id}/enrich")
def start_reading(
    request: Request,
    role_id: uuid.UUID,
    limit: int = Form(1),
    scope: str = Form("passed"),
):
    """Read the next few LinkedIn profiles for this role (IMPLEMENTATION.md 1.11d).

    Every refusal below is a preflight rather than a failed job, because the whole point
    of the button is that a recruiter finds out *now* — a queued job that dies twenty
    seconds later on a wall they cannot see is worse than the command panel it replaced.

    The preflight is advisory, not the guard: `linkedin_profile.enrich` holds the mode C
    advisory lock for the run itself, so a race between two clicks is still refused by the
    database rather than by the order the checks happened to run in.
    """
    from hi import lock
    from hi.adapters import linkedin_profile as lp

    if matching.spec_of(role_id) is None:
        return HTMLResponse("<h1>We couldn't find that role.</h1>", status_code=404)

    def back(outcome: str) -> RedirectResponse:
        return RedirectResponse(f"/roles/{role_id}?reading={outcome}", status_code=303)

    # An unknown scope reads the gate's own answer rather than guessing wider: a typo in
    # a form field must never spend the day's quota on people the gate refused.
    gate_state = worker.GATE_SCOPES.get(scope, "passed")

    if lp.policy_blocked():
        return back("off")
    if lp.budget_remaining() <= 0:
        return back("spent")
    if not lp.browser_attached():
        return back("no-browser")
    # One LinkedIn account, so a read for *another* role blocks this one too.
    if worker.pending("enrich") or lock.held():
        return back("busy")

    todo = [p for p in lp.waiting_for_role(role_id, gate_state) if not p["enriched"]]
    if not todo:
        return back("nobody")

    # Never queue a number the run would silently truncate.
    count = max(1, min(limit, len(todo), lp.budget_remaining()))
    payload = {"role_id": str(role_id), "limit": count}
    if gate_state != "passed":
        payload["scope"] = scope
    if worker.enqueue("enrich", payload) is None:
        return back("busy")
    return back("started")


@app.get("/roles/{role_id}/reading", response_class=HTMLResponse)
def reading_panel(request: Request, role_id: uuid.UUID):
    """HTMX polling target for the reading panel — a run is minutes long."""
    return templates.TemplateResponse(
        request, "_reading.html", _collect_context(role_id) | {"reading": None}
    )


def _shortlist_context(role_id: uuid.UUID, show_ruled_out: bool) -> dict:
    from hi import discovery
    from hi.discovery import get_role

    title, spec = get_role(role_id)
    rows = matching.shortlist(role_id, include_excluded=True)

    # Three buckets, not two. "We checked and they do not match" and "we have not
    # looked at them yet" are different facts, and a gate verdict cannot tell them
    # apart on its own: an unread candidate has no evidence, so every gate reads
    # "no evidence for Python", which the recruiter sees as "No public sign of Python".
    # On role d956b79f that was 18 of 25 exclusions — real candidates a recruiter would
    # have discarded on a reason that was never true. `checked` comes first: an unread
    # person belongs in "not looked at yet" whatever their gates happen to say.
    unchecked = [r for r in rows if not r["checked"]]
    checked = [r for r in rows if r["checked"]]
    return {
        "role_id": role_id,
        "role_title": title,
        "spec": spec,
        "kept": [r for r in checked if r["passed_gates"]],
        "ruled_out": [r for r in checked if not r["passed_gates"]],
        "not_looked_at": unchecked,
        "show_ruled_out": show_ruled_out,
        "progress": matching.role_progress(role_id),
        # The people the snippet gate refused, with the reason. Without them the page
        # says "looked at 23 people" and lists 11, and the 12 missing are the ones a
        # recruiter most needs to see: a gate that is too tight is invisible otherwise.
        # Read means read: once someone overrides the gate and we open one of these
        # profiles, it stops being a person we did not open and joins the ranking above.
        "not_opened": [
            r
            for r in discovery.saved_refs(role_id, gate_state="failed")
            if r["candidate_id"] not in {c["candidate_id"] for c in checked}
        ],
        "enrichment_degraded": matching.enrichment_degraded(role_id),
    }


@app.get("/roles/{role_id}/rows", response_class=HTMLResponse)
def shortlist_rows(request: Request, role_id: uuid.UUID, show_ruled_out: bool = False):
    """HTMX polling target — streams results in while a search runs."""
    return templates.TemplateResponse(
        request, "_shortlist_rows.html", _shortlist_context(role_id, show_ruled_out)
    )


# --------------------------------------------------------------------------
# Screen 4 — Candidate detail (a panel, not a page)
# --------------------------------------------------------------------------


@app.get("/roles/{role_id}/candidates/{candidate_id}", response_class=HTMLResponse)
def candidate_panel(request: Request, role_id: uuid.UUID, candidate_id: uuid.UUID):
    detail = matching.candidate_detail(role_id, candidate_id)
    if detail is None:
        return HTMLResponse("<p>We couldn't find that person.</p>", status_code=404)
    return templates.TemplateResponse(
        request, "_candidate_panel.html", {"d": detail, "role_id": role_id}
    )


@app.post("/matches/{match_id}/action", response_class=HTMLResponse)
def act(request: Request, match_id: int, action: str = Form(...), note: str = Form("")):
    try:
        # actor comes from the session, never from the form — the `actor` columns
        # exist precisely so this stops being null (IMPLEMENTATION.md 1.12).
        matching.record_action(
            match_id, action, note=note or None, actor=request.state.actor
        )
    except ValueError as exc:
        return HTMLResponse(f"<p>{exc}</p>", status_code=400)
    return HTMLResponse(f'<span class="acted">Marked: {action.replace("_", " ")}</span>')


# --------------------------------------------------------------------------
# Admin (unlisted, deliberately plain) — 1.11a
# --------------------------------------------------------------------------


@app.get("/admin/health", response_class=HTMLResponse)
def health(request: Request):
    from hi.web.admin import adapter_health, cache_usage

    return templates.TemplateResponse(
        request,
        "admin_health.html",
        {
            "adapters": adapter_health(),
            "cache": cache_usage(),
            "queue": worker.queue_counts(),
        },
    )

import uuid

import pytest
from fastapi.testclient import TestClient

from hi import auth
from hi.web.app import app

USER = "recruiter@example.com"
PASSWORD = "correct horse battery staple"


@pytest.fixture()
def users(db_conn, monkeypatch):
    monkeypatch.setenv("HI_USERS", f"{USER}:{auth.hash_password(PASSWORD)}")


@pytest.fixture()
def anon(db_conn, users):
    return TestClient(app)


# --- passwords ---------------------------------------------------------------


def test_hash_and_verify_round_trip():
    encoded = auth.hash_password(PASSWORD)
    assert auth.verify_password(PASSWORD, encoded)
    assert not auth.verify_password("wrong", encoded)


def test_hash_is_salted():
    # Two hashes of the same password must differ, or a rainbow table works.
    assert auth.hash_password(PASSWORD) != auth.hash_password(PASSWORD)


def test_password_is_not_stored_in_the_hash():
    assert PASSWORD not in auth.hash_password(PASSWORD)


@pytest.mark.parametrize("bad", ["", "not-a-hash", "md5$1$aa$bb", "pbkdf2_sha256$x$y"])
def test_malformed_hash_never_verifies(bad):
    assert not auth.verify_password(PASSWORD, bad)


def test_authenticate(db_conn, users):
    assert auth.authenticate(USER, PASSWORD) == USER
    assert auth.authenticate(USER, "wrong") is None
    assert auth.authenticate("nobody@example.com", PASSWORD) is None


def test_email_is_case_insensitive(db_conn, users):
    assert auth.authenticate("Recruiter@Example.COM", PASSWORD) == USER


def test_no_configured_users_means_nobody_gets_in(db_conn, monkeypatch):
    # Fail closed: bricked is the correct direction for something holding personal data.
    monkeypatch.setenv("HI_USERS", "")
    assert auth.authenticate(USER, PASSWORD) is None


# --- sessions ----------------------------------------------------------------


def test_session_round_trip(db_conn):
    token = auth.create_session(USER)
    assert auth.actor_for(token) == USER


def test_logout_actually_invalidates(db_conn):
    token = auth.create_session(USER)
    auth.destroy_session(token)
    # Not merely "the cookie was cleared" — the session is gone server-side.
    assert auth.actor_for(token) is None


def test_expired_session_is_rejected(db_conn):
    token = auth.create_session(USER)
    db_conn.execute("update session set expires_at = now() - interval '1 hour' where id = %s", (token,))
    assert auth.actor_for(token) is None


def test_unknown_token_is_rejected(db_conn):
    assert auth.actor_for("made-up") is None
    assert auth.actor_for(None) is None


def test_purge_expired(db_conn):
    token = auth.create_session(USER)
    db_conn.execute("update session set expires_at = now() - interval '1 hour' where id = %s", (token,))
    assert auth.purge_expired() == 1


# --- no anonymous access anywhere --------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/", "/roles/new", "/admin/health", "/roles/{r}", "/roles/{r}/rows", "/roles/{r}/candidates/{c}"],
)
def test_every_screen_redirects_when_signed_out(anon, path):
    url = path.format(r=uuid.uuid4(), c=uuid.uuid4())
    r = anon.get(url, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_admin_page_is_not_anonymous(anon):
    # Unlisted is not the same as unprotected.
    assert anon.get("/admin/health", follow_redirects=False).status_code == 303


@pytest.mark.parametrize(
    "method,path", [("post", "/roles"), ("post", "/roles/read"), ("post", "/matches/1/action")]
)
def test_mutations_401_rather_than_redirect(anon, method, path):
    # A redirect here would let HTMX swap a login page into the shortlist.
    r = getattr(anon, method)(path, data={}, follow_redirects=False)
    assert r.status_code == 401


def test_healthz_stays_public(anon):
    assert anon.get("/healthz").status_code == 200


# --- login flow --------------------------------------------------------------


def test_login_page_renders(anon):
    r = anon.get("/login")
    assert r.status_code == 200
    assert "Sign in" in r.text


def test_login_page_explains_when_no_accounts_exist(db_conn, monkeypatch):
    monkeypatch.setenv("HI_USERS", "")
    r = TestClient(app).get("/login")
    assert "No accounts have been set up yet" in r.text
    assert "python -m hi.auth hash" in r.text


def test_successful_login_sets_a_hardened_cookie(anon):
    r = anon.post(
        "/login", data={"email": USER, "password": PASSWORD, "next": "/"}, follow_redirects=False
    )
    assert r.status_code == 303
    cookie = r.headers["set-cookie"]
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()


def test_failed_login_is_401_and_sets_nothing(anon):
    r = anon.post("/login", data={"email": USER, "password": "wrong", "next": "/"})
    assert r.status_code == 401
    assert "did not match" in r.text
    assert "set-cookie" not in {k.lower() for k in r.headers}


def test_login_then_reach_a_protected_page(anon):
    anon.post("/login", data={"email": USER, "password": PASSWORD, "next": "/"})
    assert anon.get("/").status_code == 200


def test_logout_ends_the_session(anon):
    anon.post("/login", data={"email": USER, "password": PASSWORD, "next": "/"})
    assert anon.get("/").status_code == 200

    anon.post("/logout", follow_redirects=False)
    assert anon.get("/", follow_redirects=False).status_code == 303


def test_open_redirect_is_refused(anon):
    r = anon.post(
        "/login",
        data={"email": USER, "password": PASSWORD, "next": "https://evil.example/steal"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/"


def test_protocol_relative_redirect_is_refused(anon):
    r = anon.post(
        "/login",
        data={"email": USER, "password": PASSWORD, "next": "//evil.example"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/"


def test_next_path_is_preserved_through_login(anon):
    r = anon.get("/admin/health", follow_redirects=False)
    assert r.headers["location"] == "/login?next=/admin/health"


def test_accounts_can_come_from_dotenv_not_only_the_environment(monkeypatch):
    """Until 2026-08-27 `configured_users` read only `os.environ`, so the `HI_USERS`
    line that `.env.example`, RUNBOOK entry 6 and the sign-in page all tell the
    operator to put in `.env` produced a silent 401.
    """
    from hi import config

    monkeypatch.delenv("HI_USERS", raising=False)
    monkeypatch.setattr(
        config.settings, "hi_users", f"dotenv@example.com:{auth.hash_password('pw')}"
    )
    assert auth.authenticate("dotenv@example.com", "pw") == "dotenv@example.com"


def test_an_explicitly_empty_env_var_still_means_nobody(monkeypatch):
    """`HI_USERS=""` is a deliberate lockout and must not fall through to `.env`."""
    from hi import config

    monkeypatch.setenv("HI_USERS", "")
    monkeypatch.setattr(
        config.settings, "hi_users", f"dotenv@example.com:{auth.hash_password('pw')}"
    )
    assert auth.configured_users() == {}
    assert auth.authenticate("dotenv@example.com", "pw") is None

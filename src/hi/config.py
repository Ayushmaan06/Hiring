from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql://hi:hi@localhost:5442/hi"

    # SERP discovery (ARCHITECTURE §7.4 mode A). Get a key at https://serpapi.com/manage-api-key
    serpapi_key: str = ""
    serpapi_results_per_query: int = 20

    # JD parsing — the ONLY sanctioned LLM call-site in M1 (AGENTS.md §2.1).
    # Optional: without it, JD parsing falls back to deterministic canon extraction.
    # Get a key at https://console.anthropic.com/settings/keys
    anthropic_api_key: str = ""
    jd_model: str = "claude-haiku-4-5"

    # Groq fallback when Anthropic key unavailable. Get one at https://console.groq.com/keys
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"

    # GitHub verification. OPTIONAL but strongly recommended: unauthenticated is
    # 60 requests/hour, a token gives 5,000/hour.
    # Get one at https://github.com/settings/tokens (no scopes needed — public data only)
    github_token: str = ""
    github_max_repos_per_user: int = 30

    # Match-label thresholds. In config, never in templates (IMPLEMENTATION.md 1.11),
    # so tuning what "Strong match" means is not a template edit.
    match_strong_min: float = 0.55
    match_good_min: float = 0.40
    match_possible_min: float = 0.25

    # Sign-in accounts, as `email:hash` pairs, comma separated for more than one
    # person. Generate a hash with `python -m hi.auth hash 'password'`. Empty means
    # nobody can sign in, which is the correct direction to fail.
    hi_users: str = ""

    # The queue is drained inside the web process, so `uvicorn hi.web:app` is the whole
    # application and clicking "Find candidates" actually searches. Set false to run
    # `python -m hi.worker` separately instead — safe either way, and safe with both at
    # once: `FOR UPDATE SKIP LOCKED` is exactly what makes two drainers harmless.
    inline_worker: bool = True

    # Set true in any deployment reachable over a network. Left false so the cookie
    # still works over plain http on localhost during development.
    cookie_secure: bool = False


settings = Settings()

"""The only place an LLM is called. See AGENTS.md.

One sanctioned call-site in M1: JD text -> draft role spec (AGENTS.md §2.1). Nothing
here ever sees candidate data, computes a score, merges a person, or decides a fetch.

The call returns raw extraction, never a finished spec: mapping names to canonical
skills and region codes is deterministic work done in `roles.py`, per AGENTS.md §2's
standing rule that a deterministic parser beats spending a token.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, Field

from hi.config import settings

PROMPTS_DIR = Path(__file__).parent / "prompts"


class LlmUnavailable(RuntimeError):
    """No API key configured. Callers fall back; they never fabricate a spec."""


class JDExtraction(BaseModel):
    """What the model may report from a JD. Names as written, never codes."""

    titles: list[str] = Field(default_factory=list)
    must_have_skills: list[str] = Field(default_factory=list)
    nice_to_have_skills: list[str] = Field(default_factory=list)
    min_years: int | None = None
    max_years: int | None = None
    location_names: list[str] = Field(default_factory=list)
    remote: str | None = None
    domains: list[str] = Field(default_factory=list)


def load_prompt(name: str) -> tuple[str, str]:
    """Return (version, body). `extractor_version` is f"{name}@{version}"."""
    text = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    match = re.match(r"\s*version:\s*(\S+)\s*\n\s*---\s*\n(.*)", text, re.DOTALL)
    if not match:
        raise ValueError(f"prompt {name} is missing its `version:` header")
    return match.group(1), match.group(2).strip()


def extractor_version(name: str) -> str:
    version, _ = load_prompt(name)
    return f"{name}@{version}"


def parse_jd(jd_text: str) -> tuple[JDExtraction, str]:
    """JD text -> (extraction, raw model output).

    Tries Anthropic first, falls back to Groq if Anthropic fails (e.g., insufficient
    credits). Raises LlmUnavailable only if both are unavailable or fail.

    Callers must degrade to deterministic draft — never a guessed spec.
    """
    _, system_prompt = load_prompt("parse_jd")

    # Try Anthropic first
    if settings.anthropic_api_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            response = client.messages.parse(
                model=settings.jd_model,
                max_tokens=4096,
                system=system_prompt,
                messages=[{"role": "user", "content": f"<job_description>\n{jd_text}\n</job_description>"}],
                output_format=JDExtraction,
            )
            raw = next((b.text for b in response.content if b.type == "text"), "")
            if response.parsed_output is None:
                raise ValueError("model returned no parseable extraction")
            return response.parsed_output, raw
        except Exception as e:
            # Fall through to try Groq if Anthropic fails (insufficient credits, etc.)
            if not settings.groq_api_key:
                raise

    # Fall back to Groq
    if settings.groq_api_key:
        try:
            from groq import Groq
            client = Groq(api_key=settings.groq_api_key)
            schema_hint = (
                "Respond with ONLY a JSON object matching this shape: "
                '{"titles": [str], "must_have_skills": [str], "nice_to_have_skills": [str], '
                '"min_years": int|null, "max_years": int|null, "location_names": [str], '
                '"remote": str|null, "domains": [str]}'
            )
            response = client.chat.completions.create(
                model=settings.groq_model,
                max_tokens=4096,
                messages=[
                    {"role": "system", "content": f"{system_prompt}\n\n{schema_hint}"},
                    {"role": "user", "content": f"<job_description>\n{jd_text}\n</job_description>"},
                ],
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content
            response_json = json.loads(raw)
            return JDExtraction.model_validate(response_json), raw
        except Exception:
            raise

    raise LlmUnavailable("ANTHROPIC_API_KEY and GROQ_API_KEY are both not set (see .env.example)")

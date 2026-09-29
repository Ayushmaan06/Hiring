# AI and Agent Boundaries

Read `docs/ARCHITECTURE.md` first. This file governs **where LLMs are allowed to act** and, more importantly, where they are not.

The system is conventional software with four LLM call-sites. It is **not** an agent. There is no autonomous loop, no tool-calling agent that decides its own next step, and no LLM in the ranking arithmetic.

---

## 1. The rule

> An LLM may **read unstructured text and emit structured data**, or **write prose a human will read**. It may not decide, rank, merge, or arithmetic.

Every LLM call must be able to fail without corrupting state. If an LLM output is unusable, the pipeline degrades to a lower-confidence result or surfaces the raw output for a human — never a silent guess.

---

## 2. The four sanctioned call-sites

**v1 status — only 2.1 is on.** Free-tier operation is a v1 requirement (`docs/PRD.md` §6). 2.2, 2.3
and 2.4 ship dark and turn on in M2 against the evidence that they are needed. Milestone 1 makes
**zero LLM calls against candidate data.** Two standing constraints when they do turn on:

- **A free tier whose terms permit training on inputs may only see a JD.** Candidate text goes to a
  local model or a paid API with a no-training term. Both are personal data; only one is ours.
- **If a deterministic parser can produce the row, the LLM does not run.** The GitHub API returns
  structured JSON — spending a token to read it is waste, not extraction.

### 2.1 JD → `role_spec` (judgement model)

- **In:** free-text job description.
- **Out:** draft `role_spec` JSON (schema in `docs/ARCHITECTURE.md` §4.5).
- **Human gate:** the recruiter edits and confirms before anything runs. The draft is never used directly.
- **Failure:** show the raw model output next to an empty form. A bad parse costs the recruiter two minutes, not a bad search.
- **Never:** invent must-have skills not implied by the JD, or infer a seniority range the JD does not support. Leave fields null and let the human fill them.

### 2.2 Query expansion (judgement model, once per role)

- **In:** confirmed `role_spec` + the skill canon.
- **Out:** additional search-query strings.
- **Bounded:** the canonical skill table is authoritative. The LLM may *propose* aliases, which land in `skill_alias_proposal` for human approval; it may never write `skill_alias`.
- **Failure:** fall back to canon-only queries. The search gets narrower, not wrong.

### 2.3 Page → evidence rows (extraction model, high volume)

The workhorse. This is the call-site that justifies using an LLM at all.

- **In:** one fetched document (HTML text, PDF text, README, bio).
- **Out:** a list of evidence rows conforming to a strict schema.
- **Hard requirement:** every row carries a `snippet` that is a **verbatim substring of the source text**. This is checked programmatically after generation. A row whose snippet is not found in the source is **dropped**, not corrected.
- **Never extract:** gender, caste, religion, age, marital status, nationality, photographs, or anything in `BANNED_SIGNALS`. Not "extract but don't rank" — do not collect.
- **Failure:** the whole document is quarantined to `extraction_failure` with the raw response. Never write partial rows from a malformed response.
- **Model choice:** cheap and fast (Haiku 4.5). This is a schema-filling task, not a reasoning task. Do not upgrade the model here to fix a prompt problem.

### 2.4 Rationale prose (judgement model, low volume)

- **In:** a candidate's evidence rows plus the already-computed score components.
- **Out:** two or three sentences explaining the ranking, citing evidence ids.
- **The score is computed before this call and is never modified by it.** If the rationale contradicts the score, the rationale is the thing that is wrong.
- **Validated after generation:** every cited id must belong to this candidate; no `BANNED_SIGNALS` terms; no restating or re-deriving the numeric score. On any violation the rationale is discarded and the card shows the component breakdown alone.

---

## 3. Explicitly forbidden LLM uses

| Forbidden | Why | What we do instead |
|---|---|---|
| Computing or adjusting a fit score | Must be reproducible, auditable, and identical across runs | Deterministic weighted sum, §5.2 |
| Deciding whether two profiles are the same person | A false merge corrupts data irreversibly and is hard to detect | Strong-key match; ambiguity → `identity_review` |
| Deciding whether to fetch a URL | Fetch policy is legal/compliance surface, not a judgement call | `source_policy` table + robots.txt |
| Ranking or re-ranking a shortlist | Same as scoring | Sort by the deterministic score |
| Inferring unstated attributes about a person | This is where bias and fabrication enter | Absent evidence is absent, not inferred |
| Writing to `skill_alias`, `scoring_weights`, or `source_policy` | These are human-governed config | Proposal tables + human approval |
| An autonomous multi-step research loop | Unbounded cost, unbounded blast radius, unreproducible | A fixed pipeline with fixed stages |

---

## 4. Prompt and schema discipline

- Every call-site has a versioned prompt in `src/hi/prompts/` and a pydantic output model. `extractor_version` on an evidence row is `"{prompt_name}@{version}"`.
- Bump the version when the prompt or schema changes. Because `(fetch_id, extractor_version)` is unique, a bump is what makes re-extraction over cached bodies possible. Editing a prompt without bumping silently mixes outputs from two different extractors in one table — treat it as a data-corrupting change.
- Structured output is enforced by schema, not by asking nicely. A response that does not validate is retried once, then quarantined.
- Prompts are checked into git and reviewed like code.
- ~~Temperature 0 for extraction.~~ **Superseded 2026-08-26.** The `anthropic` 1.x SDK
  removed sampling parameters (`temperature`, `top_p`, `top_k`) from the Messages API, and
  current models reject them outright, so this is no longer settable. Do not reintroduce it
  through `extra_body` — a 400 on every call is worse than no knob. Reproducibility for the
  JD call-site now rests on four things instead: the enforced output schema, a prompt that
  forbids inference, deterministic post-processing (canon mapping, and the JD text
  overruling the model on year counts — `roles.py`), and Human Gate 1. Note this makes the
  draft *stable in practice, not guaranteed byte-identical* — which is acceptable precisely
  because a human confirms it. Scoring remains byte-identical because no LLM touches it.

---

## 5. Cost and rate discipline

- Extraction runs per *document*, not per candidate — cap documents per candidate (default 8) or one candidate with 300 repos costs more than the rest of the search.
- Token counts per role search are logged to `pipeline_event`. A role search that exceeds a configured ceiling **pauses and asks**, rather than continuing to spend.
- No LLM call inside a retry loop without a hard attempt cap.

---

## 6. For coding agents working in this repo

- Do not introduce an agent framework, a tool-calling loop, or a "let the model decide" branch. If a task looks like it needs one, it needs a deterministic stage instead — say so rather than building it.
- Do not add an LLM call-site without adding it to §2 of this file in the same change.
- Do not use an LLM to make a decision that a `WHERE` clause can make.
- When an extraction is unreliable, fix the prompt or the parser. Do not add a second LLM to check the first one.

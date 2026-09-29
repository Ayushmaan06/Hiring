# Hiring Intelligence — Master Project & Scraping Documentation

> **Last Updated:** September 2026  

---

## Executive Summary

**Hiring Intelligence** is an internal recruitment automation tool built to transform job requirements into a ranked, evidence-backed shortlist of candidates. 

Instead of manually screening hundreds of resume piles or scrolling endlessly through job board search results, a recruiter defines role requirements (skills, seniority, location, domain) once. The system then automatically discovers matching candidate profiles, enriches their employment history, verifies their claims against real-world work (e.g. public code repositories or web evidence), and calculates a transparent, auditable fit score.

### Core Philosophy
> *"A profile tells you who to consider; an artefact tells you whether the profile is true."*

* **Discovery (LinkedIn Spine):** Extracts who candidates claim to be, their past employers, job titles, and employment dates.
* **Verification (Public Artefacts):** Verifies self-reported skills against demonstrable work (GitHub, HuggingFace, CodeForces, company team pages).
* **Deterministic Matching:** Scores candidates using fixed mathematical rules—never an LLM black box guessing numbers.

---

## 1. How the System Works (End-to-End Pipeline)

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐     ┌──────────────────┐
│  Job Description│ ──► │  SERP Discovery  │ ──► │  Snippet Gate   │ ──► │ Candidate ID     │
│  (JD Text)      │     │  (Mode A Search) │     │  (Free Pre-Filter)│   │ Resolution       │
└─────────────────┘     └──────────────────┘     └─────────────────┘     └──────────────────┘
                                                                                  │
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐              ▼
│ Ranked Shortlist│ ◄── │  Deterministic   │ ◄── │ Claim           │ ◄── ┌──────────────────┐
│ & Evidence Card │     │  Fit Scorer      │     │ Verification    │     │ LinkedIn         │
└─────────────────┘     └──────────────────┘     └─────────────────┘     │ Enrichment (Mode C)
                                                                         └──────────────────┘
```

### Step-by-Step Workflow:
1. **Role Spec Creation:** The recruiter submits a Job Description (JD). An LLM parses it into structured requirements (`must_have_skills`, `seniority`, `locations`, `domains`), which the recruiter confirms or edits (**Human Gate 1**).
2. **Discovery (`linkedin_serp` / Mode A):** The system generates Google search queries targeted at LinkedIn profiles (`site:linkedin.com/in/`) matching the required titles, skills, and locations.
3. **The Snippet Gate:** Before fetching expensive profile pages, candidate search snippets (headline, title, employer, location) are evaluated against the spec. Candidates who don't match are immediately ruled out with a recorded reason.
4. **Candidate ID Resolution:** Surviving candidates are resolved into candidate entities using strong keys (LinkedIn profile URL, GitHub username, email hash).
5. **LinkedIn Enrichment (Mode C):** The system fetches detailed employment history (`position_title`, `institution_name`, `from_date`, `to_date`, `location`) from LinkedIn.
6. **Claim Verification:** Candidate skills are cross-referenced with public evidence sources (GitHub commits/repos, company team pages, published interviews).
7. **Deterministic Scoring:** A pure mathematical engine computes candidate fit scores across 5 allowlisted components.
8. **Recruiter Shortlist:** Candidates appear on a recruiter web dashboard sorted by score, with every claim clickable and badged by evidence tier (**Human Gate 2**).

---

## 2. How Scraping Works & How Candidate IDs Are Found

Sourcing candidates from LinkedIn requires balancing data depth with platform anti-bot protections. The system uses a multi-mode strategy designed to minimize account risk while maximizing accurate candidate discovery.

### 2.1 The Four LinkedIn Modes

| Mode | Name | How It Works | Data Depth | Account / Ban Risk |
|---|---|---|---|---|
| **Mode A** | `serp_only` | Searches Google SERP (`site:linkedin.com/in/`). Extracts candidate name, headline, title, employer, city, and LinkedIn URL. | High-level snippet metadata | **ZERO Risk** (Touches no LinkedIn servers) |
| **Mode B** | `public_logged_out` | Fetches public profile web pages with no cookies or login session. | Public profile view (frequently truncated dates/experiences) | **ZERO Account Risk** (Unauthenticated requests) |
| **Mode C** | `attached_browser` | Playwright connects via Chrome DevTools Protocol (CDP) to the operator’s **real, already-logged-in** personal browser. | Complete experience, titles, employment dates | **Managed Risk** (Uses real signed-in session; capped & slow) |
| **Mode D** | `people_search` | Direct automation of LinkedIn's internal search UI. | Deepest recall & filter options | **High Risk** (Policed heavily; trigger-gated, not built) |

> **Current Operational Setup:** **Mode A** performs all initial candidate discovery, and **Mode C** performs enrichment on candidates that pass the snippet pre-filter.

### 2.2 Finding & Resolving Candidate IDs

When scraping LinkedIn and public sources, the system extracts and generates several types of IDs:
* **Canonical Profile Slugs:** LinkedIn URLs are normalized from localized or legacy formats (e.g., `in.linkedin.com/in/john-doe-123` $\rightarrow$ `linkedin.com/in/john-doe-123`).
* **Strong Identity Keys:**
  * LinkedIn profile URL (`kind="linkedin_url"`)
  * Canonical GitHub handle (`kind="github_handle"`)
  * Cryptographic SHA-256 hash of email (`kind="email_sha256"`)
* **Internal Candidate UUID:** A unique system UUID assigned in PostgreSQL to group all evidence rows for a candidate.
* **Identity Merging Strategy:** To avoid false merges (uniting two different people with similar names), identity resolution requires an **exact match on strong keys**. Weak or ambiguous matches are placed in a human review queue.

### 2.3 Text-Block DOM Extraction (Overcoming CSS Obfuscation)

Modern websites like LinkedIn frequently change their HTML structure and use auto-generated, hashed CSS class names (e.g., `.cc605e5a`). Traditional web scrapers using CSS selectors break almost daily when class names change.

**Our Workaround:** Instead of relying on CSS class names, our extractor reads the rendered text blocks of the profile's Experience and Education sections:
* It anchors on date-range pattern structures (e.g., `"Jan 2021 – Present · 3 yrs 8 mos"`).
* It extracts the position title and company name from the text lines immediately preceding the date anchor.
* This makes the scraper resilient against frontend UI updates and CSS class obfuscation.

---

## 3. Why LinkedIn Scraping Is So Slow (Speed Bottlenecks & Design Choices)

Non-technical users often ask: *"Why does it take minutes to scrape 15–20 candidates when Google takes seconds?"*

Scraping LinkedIn profile data quickly is trivial; scraping it **without getting the user's account banned or blocked by CAPTCHAs** requires deliberate slowness.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                      OPERATIONAL RATE LIMITS                            │
├───────────────────────────────┬─────────────────────────────────────────┤
│ Max Profiles Per Day          │ 30 profiles / day / account             │
│ Max Concurrency               │ 1 request at a time (Serial processing) │
│ Minimum Inter-Request Delay   │ 20 seconds + random jitter              │
│ Execution Mode                │ Headful browser (visible window only)   │
│ Concurrency Lock              │ PostgreSQL Advisory Lock (hi/lock.py)   │
└───────────────────────────────┴─────────────────────────────────────────┘
```

### Key Reasons for Slowness:

1. **Human-like Request Pacing:**
   Automated bots send requests milliseconds apart. To mimic human behavior, the system waits **at least 20 seconds plus random jitter** between profile visits. Scraped in bulk, 10 profiles take at least 3.5 to 5 minutes.
2. **Strict Daily Profile Budget (30 Profiles/Day):**
   LinkedIn tracks account commercial use limits and profile viewing velocity. We enforce a hard ceiling of 30 profile enrichments per account per day. Reaching this limit automatically stops the scraper until the rolling window clears.
3. **Single-Threaded Serial Execution (Concurrency = 1):**
   Opening 5 profile tabs simultaneously triggers instant bot detection. Enrichment runs sequentially through a single browser session. Database-level advisory locks prevent background workers or UI buttons from running parallel scraping sessions.
4. **Visible Browser Window (Headless Banned):**
   Headless browsers (hidden Chrome instances) emit distinct browser environment signals easily flagged by anti-bot engines. Mode C uses CDP attachment to a visible, real desktop browser.
5. **Caching Discipline:**
   The fetch layer maintains a 24-hour content-addressed disk cache. If a candidate URL was scraped earlier in the day, the system loads the cached body instantly rather than hitting LinkedIn again.

---

## 4. Problems Faced & Technical Workarounds

Building an automated sourcing pipeline over defended third-party platforms presented several architectural and operational challenges:

### 1. Risk of Account Restrictions & CAPTCHAs
* **The Problem:** Aggressive scraping leads to IP blocks, HTTP 999 authwalls, CAPTCHAs, or account restrictions.
* **The Workaround:** 
  * We **do not use fake burner account fleets** (which violate platform policies and expose egress IPs).
  * We attach via CDP to the operator's real browser session.
  * The system automatically self-disables (`source_policy.enabled = false`) on the first authwall challenge or HTTP 999 code, alerting a human operator rather than repeatedly retrying.

### 2. "Says So" vs. "Proven" Skills (Self-Reported Skill Inflation)
* **The Problem:** Candidates list skills on profiles that they may only have basic awareness of.
* **The Workaround:** We implement a **3-Tier Evidence System**:
  * Tier 1 (`artifact_backed`, 1.0 weight): Verified code/repos (GitHub, HuggingFace).
  * Tier 2 (`third_party_stated`, 0.6 weight): External mentions (company team pages, conference agendas).
  * Tier 3 (`self_reported`, 0.35 weight): LinkedIn profile assertions.
  * *Result:* Two candidates with identical LinkedIn text will be separated in score by their proven artefact evidence.

### 3. Non-Technical & MBA Role Scoring (The "Zero Score" Bug)
* **The Problem:** Non-technical candidates (e.g., Operations, Product, Sales) lack GitHub repositories, causing artefact-heavy scoring models to assign them near-zero scores.
* **The Workaround:** 
  * Dual scoring weights (`v1` for engineering vs. `v1-business` for non-tech). Non-tech roles zero out code artefact weights and re-attribute scoring weight to `skill_match` (0.45) and `seniority_fit` (0.40).
  * Added `serp_web` adapter to search third-party web evidence (news articles, speaker bios, team pages) with a controlled domain wildcard policy.

### 4. Legal Compliance & Indian Privacy Law (DPDP Act 2023)
* **The Problem:** Storing named individual candidate records, employment histories, and emails creates data protection compliance obligations.
* **The Workaround:**
  * **Data Minimization:** We collect only evidence necessary to evaluate fit for active roles.
  * **Hashed Credentials:** Candidate emails are stored strictly as SHA-256 hashes—never in plaintext.
  * **Banned Attributes:** We do not collect, infer, or store photographs, age, gender, caste, or religion.
  * **Cascade Right-to-Erasure:** A dedicated erasure endpoint cascades deletions through PostgreSQL tables and disk cache files upon candidate request.

### 5. Algorithmic Fairness & Bias Prevention
* **The Problem:** Ranking algorithms can introduce indirect proxies for protected demographic attributes (e.g., age bias from graduation years, caste proxies from college tiers).
* **The Workaround:**
  * Strict `FEATURE_ALLOWLIST` in code. Only 5 validated components can enter the fit score calculation (`skill_match`, `skill_depth`, `seniority_fit`, `activity_recency`, `availability`).
  * `BANNED_SIGNALS` (college tier, graduation year, photo, gender, name tokens) are mathematically barred from entering score inputs.
  * Education institutions may be stored for human recruiter viewing if explicit in the JD, but have zero mathematical impact on candidate rank.

---

## 5. Summary Reference for Stakeholders

| Component / Concept | Technical Detail | Non-Technical / Business Impact |
|---|---|---|
| **Discovery (Mode A)** | Google SERP API (`site:linkedin.com/in/`) | Finds matching candidate profiles without spending scarce LinkedIn access limits or risking account bans. |
| **Snippet Gate** | Evaluates headline/location text pre-fetch | Filters out ~80% of non-matching profiles for free before wasting scraping budgets. |
| **Enrichment (Mode C)** | Playwright CDP attach to Chrome/Edge | Scrapes employment history, titles, and dates from 30 profiles/day safely. |
| **DOM Parsing** | Text-block structure parsing (Date anchors) | Scraper stays reliable even when LinkedIn redesigns frontend CSS class names. |
| **Scraping Speed** | 20s+ delay, serial queue, 30/day budget | Ensures recruiter account safety and zero bot detection, sacrificing speed for security. |
| **Evidence Tiers** | `artifact_backed` vs `self_reported` | Separates candidates who *actually build* from candidates who *just list keywords*. |
| **Fit Score** | Deterministic 5-component weighted sum | Transparent, reproducible candidate ranking with zero LLM hallucination in scores. |
| **Privacy & DPDP** | Hashed emails, cascade deletion, banned demographics | Full compliance with India's DPDP Act 2023 and fair hiring practices. |

---


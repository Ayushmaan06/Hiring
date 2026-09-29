# **Option B: Internal Profile Scraper for Hiring** 

#### **Requirements in → Ranked shortlist out** 

A standalone internal tool: set role requirements (skills, experience, location, notice period, budget) once, and get back a ranked list of the right candidates for that role, sourced by scraping public candidate/professional profiles — instead of manually screening resumes or scrolling job-board search results. 

## **B1. Objective** 

**Define requirements once → system continuously surfaces and ranks matching candidates → recruiter reviews a shortlist, not a resume pile** 

## **B2. Core Problem** 

Search by criteria such as: skills/tech stack, years of experience (total and relevant), current role/seniority, location and remote/relocation preference, notice period, current and expected compensation, education, and demonstrated skill signals (GitHub activity, competitive programming ratings, project history). Today this is manual — scrolling profile search results, screening resumes one at a time, no persistent ranked view across searches or roles. 

As with the school platform, **almost every one of these filters is more ambiguous than it looks** , and here the wrong assumption doesn't just produce a bad lead — it can produce a legally risky filter or a privacy problem. Section B5 goes deep on this. 

## **B3. Architecture** 

CANDIDATE PROFILE SOURCES 

│ ┌─────────────────┼─────────────────┐ ↓                 ↓                                     ↓ Professional        Public developer   Job board / 

networking sites    profiles (GitHub,  resume databases 

(profile scraping)  Stack Overflow) 

└─────────────────┼─────────────────┘ ↓ DATA INGESTION (async, Celery) 

↓ 

#### NORMALIZATION → ENTITY RESOLUTION → ENRICHMENT 

↓ ┌─────────────────┐ │ PostgreSQL      │ │ Candidate DB    │ └────────┬────────┘ ↓ MATCHING ENGINE 

┌──────────────┼──────────────┐ ↓                                    ↓                                      ↓ Requirement    Skill Verification                      Fit Score 

Scraping runs async/background against a defined set of target sources, never live on a recruiter's search — same architectural discipline as the school platform (§3). 

### **Stack** 

Same base stack as Option A: FastAPI + PostgreSQL + Redis + Celery for the backend, Scrapy/Playwright for collection, React/Next.js for the recruiter portal. No reason to diverge — the pipeline shape is identical even though the domain and compliance profile differ sharply. 

## **B4. Data Model** 

### **candidates** 

id, full_name, canonical_name, current_title, current_company, location, 

willing_to_relocate, remote_preference, total_experience_years, 

relevant_experience_years, notice_period_days, current_ctc_band, 

expected_ctc_band, education, primary_skills, source, source_url, 

last_active_at, last_verified_at, created_at 

### **candidate_skills** 

id, candidate_id, skill_name, canonical_skill, proficiency_signal, 

source (profile-listed / GitHub-verified / assessment-verified), confidence 

### **roles (the requirement filter set)** 

id, title, must_have_skills, nice_to_have_skills, min_experience, 

max_experience, location, remote_ok, budget_band, notice_period_max, status 

### **candidate_matches** 

id, candidate_id, role_id, fit_score, fit_breakdown (json), matched_at, 

recruiter_action (shortlisted / rejected / contacted / no_action) 

### **source_compliance_log** 

id, source_domain, scraping_allowed, tos_reviewed_at, rate_limit_policy, notes 

Given the source is profile data specifically, this table needs to exist before the first scraper targets a domain, not be retrofitted after — see B5.1. 

## **B5. Edge Cases & Definitional Ambiguities** 

### **B5.1 Profile scraping and platform ToS — the central risk to design around** 

Most professional networking sites explicitly prohibit automated scraping of profile data in their Terms of Service, and this has been actively litigated — the hiQ Labs v. LinkedIn case is the best-known example, and the legal landscape around it has continued to shift since. Because this scrapes named individuals' personal/professional data rather than institutional data, the exposure is different in kind from the school platform, **even though the intended use here is purely internal hiring, not resale or a client-facing product.** Internal-only use reduces some risk (no data resale, no third-party liability) but does not eliminate the ToS/legal exposure of the scraping activity itself. 

Practical implications for the build: 

- Log every target domain's scraping posture in source_compliance_log before the first scraper touches it — don't decide this ad hoc per source. 

- Favor sources with public APIs or bulk-export mechanisms (GitHub public API, Stack Overflow public API, any job board that offers legitimate data access) over scraping rendered profile pages, wherever an equivalent legitimate path exists. 

- Rate-limit aggressively per domain regardless of ToS status — this also protects the scraper itself from IP bans, which is the practical failure mode you'll hit first even before the legal one. 

- This is worth a specific legal/compliance conversation before Phase 1 scraper targets are finalized — internal-tool framing doesn't remove the need for that conversation, it just changes who needs to sign off. 

### **B5.2 Personal data handling under DPDP Act 2023** 

Every candidate record is personal data of a named individual. Even for purely internal use, collecting and storing this data — name, contact info, employment history, compensation signals — falls under India's DPDP Act. Practical baseline: collect only what's needed to evaluate fit for actual open roles, don't retain data on people you're not actively considering beyond a defined retention window, and have a clear internal answer for "what happens to a candidate's data if they ask us to delete it." 

### **B5.3 "Skills" — self-reported vs. verified, and name variants** 

- Skill names fragment badly across profiles: "React," "ReactJS," "React.js" are the same skill written differently — needs a canonical-skill mapping table, same pattern as school-name normalization in Option A. 

- Self-reported skills (from a profile) and verified skills (GitHub commit history, contribution activity) are different confidence tiers — never silently merge them into one number. Surface both and weight verified signals higher by default, with that weighting visible in the fit breakdown, not buried in the score. 

### **B5.4 Experience — total vs. relevant, and title inflation** 

- "5 years experience" filters usually mean _relevant_ experience in the specific stack, not total career length — model total_experience_years and relevant_experience_years as separate fields. 

- Job titles aren't standardized across companies ("Software Engineer II" at one company ≠ the same seniority as "Senior Software Engineer" at another) — derive a normalized seniority band from experience + scope signals rather than filtering on title strings directly. 

### **B5.5 Location — metro-area ambiguity and remote-work granularity** 

- Same NCR-style problem as the school platform's §6.2 — a "Delhi" location filter usually needs to mean Delhi NCR, not the state boundary. 

- "Remote" needs to split into fully remote / hybrid-fixed-days / remote-with-occasional-travel / willing-to-relocate as separate fields — collapsing these into one boolean loses exactly the information a recruiter is filtering for. 

### **B5.6 Compensation — bands, not points** 

Current CTC and expected CTC are two different numbers with different reliability (expected CTC is often a negotiating position, not a hard constraint). Store as ranges, never present a 

single figure as fact, and treat this field with the same handling care as B5.2 — it's sensitive personal financial data on top of being generally unreliable. 

### **B5.7 Fairness and legal exposure in filtering — the highest-risk feature category here** 

- Filters that look neutral can act as indirect proxies for protected characteristics — filtering by graduation year as an age proxy, or by college names that correlate with caste/religious demographics in parts of India, creates real legal and reputational exposure even when unintentional. 

- If the fit-score model is trained on or tuned against historical "successful hire" outcomes, audit for whether that historical pattern itself reflects bias unrelated to job performance before letting the model reproduce it. 

- Keep protected/sensitive attributes out of the ranking model's inputs entirely, not just out of the visible filter list — a feature can leak a protected attribute indirectly (e.g., certain colleges, certain name patterns) even when it isn't labeled as one. 

- Make the fit_score explainable (breakdown by skill match, experience match, verified-vs-self-reported weighting, location/comp fit) rather than a black-box number — this is both a trust requirement and a practical way to catch a biased weighting before it does damage. 

### **B5.8 Duplicate candidates and staleness** 

- The same person can appear via multiple profiles/sources under slightly different name spellings or contact details. False-positive merges here are a real data-integrity problem, not just a lost lead — route ambiguous matches to manual review, never auto-merge. 

- Profile "availability"/"open to work" signals decay fast. Time-decay this data and don't surface a match as currently available without a recent verification signal. 

## **B6. Phased Roadmap** 

#### **Phase Scope** 

|**1 — Core pipeline,**<br>**narrow source set**|Pick 1–2 sources with the clearest legitimate access path (public<br>APIs where they exist), basic requirement filters, skill normalization,<br>fit score with visible breakdown|
|---|---|
|**2 — Broader**<br>**sourcing**|Add additional sources per B5.1's compliance review, entity<br>resolution/dedup with manual-review queue (B5.8)|
|**3 — Verified skill**<br>**signals**|GitHub/Stack Overflow activity-based skill verification layered onto<br>self-reported profile data|
|**4 — Matching**<br>**intelligence**|Refined fit-score model, fairness audit (B5.7) formalized as a gate<br>before any weighting change ships|
|**5 — Continuous**<br>**sourcing**|Ongoing discovery against standing role requirements,<br>staleness-aware re-ranking, recruiter feedback loop|



Legal/compliance review on scraping targets (B5.1) and data handling (B5.2) should happen **before Phase 1 sources are finalized** , not after — this is the one place where getting the foundation wrong means rebuilding the ingestion layer, not just patching a policy. 


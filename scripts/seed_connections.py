"""Load a LinkedIn connections export (Connections.csv) as pre-made roles.

Creates ~20 roles with a generic developer JD and files every person in the CSV under
one of them (some under two) as a `candidate_ref` with adapter `manual`. Juniors
(interns, students, trainees) are filed as ruled out, with the reason. Nothing is
fetched and no candidate rows are created — reading a profile is still the normal
"Read them now" step, and "Find more people" searches SERP as for any role.

    python scripts/seed_connections.py [path/to/Connections.csv]

Idempotent: re-running reuses the roles (matched on title) and skips people already filed.

ponytail: role assignment is keyword-on-position plus random fill, not real matching.
"""

from __future__ import annotations

import csv
import json
import random
import sys
from pathlib import Path

from hi.adapters.linkedin_serp import normalise_linkedin_url
from hi.db import pool
from hi.discovery import MANUAL_ADAPTER, create_role
from hi.models import RoleSpec

ADAPTER = MANUAL_ADAPTER
MAX_PER_ROLE = 260
SECOND_ROLE_SHARE = 0.12
JUNIOR_WORDS = ("intern", "student", "trainee", "fresher", "junior", "jr ", "jr.", "apprentice", "graduate")

# title, must-have skills, position keywords that route someone here
ROLES = [
    ("Backend Engineer (Python)", ["Python"], ["python", "django", "back end", "backend"]),
    ("Backend Engineer (Java)", ["Java"], ["java", "spring"]),
    ("Backend Engineer (Go)", ["Go"], ["golang", " go "]),
    ("Node.js Developer", ["Node.js"], ["node"]),
    ("Frontend Engineer (React)", ["React"], ["frontend", "front end", "front-end", "react", "ui "]),
    ("Full Stack Engineer", ["JavaScript"], ["full stack", "fullstack", "full-stack", "mern"]),
    ("Android Developer", ["Kotlin"], ["android"]),
    ("iOS Developer", ["Swift"], ["ios"]),
    ("Data Engineer", ["SQL"], ["data engineer", "etl", "big data"]),
    ("Data Scientist", ["Python"], ["data scien", "analyst", "analytics"]),
    ("Machine Learning Engineer", ["Python"], ["machine learning", "ml ", "ml engineer", "research"]),
    ("AI Engineer", ["Python"], ["ai ", "artificial intelligence", "genai", "llm", "forward deployed"]),
    ("DevOps Engineer", ["Kubernetes"], ["devops", "sre", "reliability", "infra"]),
    ("Cloud Engineer (AWS)", ["AWS"], ["cloud", "aws"]),
    ("QA / SDET Engineer", ["Java"], ["qa", "test", "sdet", "quality"]),
    ("Platform Engineer", ["Go"], ["platform", "system software", "embedded"]),
    ("SDE-2 (Product Engineering)", ["Java"], ["sde 2", "sde-2", "sde2", "engineer 2", "engineer ii", "sde ii", "technical staff"]),
    ("Senior Software Engineer", ["Python"], ["senior", "staff", "lead", "sde 3", "engineer iii", "engineer 3"]),
    ("Engineering Manager", ["Python"], ["manager", "head", "director", "vp", "cto"]),
    ("Founding Engineer", ["TypeScript"], ["founder", "founding", "co-founder", "ceo"]),
]

JD_TEMPLATE = """{title}

We are hiring a {title} to design, build and ship reliable software for our product.

What you will do
- Build and maintain production services and features end to end.
- Write clean, tested, well-reviewed code and own it in production.
- Work with product and design to turn requirements into shipped work.

What we are looking for
- Strong fundamentals in data structures, algorithms and system design.
- Hands-on experience with {skills}.
- Comfortable with Git, CI/CD, SQL databases and cloud platforms.
- 2+ years of professional software development experience.

Location: India (hybrid).
"""


def _is_junior(position: str) -> bool:
    p = f" {position.lower()} "
    return any(w in p for w in JUNIOR_WORDS)


def _role_for(position: str) -> int | None:
    p = f" {position.lower()} "
    hits = [i for i, (_, _, words) in enumerate(ROLES) if any(w in p for w in words)]
    return random.choice(hits) if hits else None


def _ref(row: dict) -> tuple | None:
    canonical = normalise_linkedin_url(row["URL"].strip())
    if canonical is None:
        return None
    name = " ".join(f"{row['First Name']} {row['Last Name']}".split())
    position, company = row["Position"].strip(), row["Company"].strip()
    headline = " - ".join(p for p in (position, company) if p) or None
    # SERP-shaped so `discovery.ref_from_row` reads it like any other ref.
    raw = json.dumps(
        {
            "title": " - ".join(p for p in (name, position, company) if p) + " | LinkedIn",
            "snippet": "",
            "link": f"https://www.{canonical}",
            "extensions": [],
            "source": "Connections.csv",
            "connected_on": row.get("Connected On", ""),
        },
        ensure_ascii=False,
    )
    display = f"{position} at {company}" if position and company else headline
    return canonical, name or None, display, raw, _is_junior(position), position


def _role_ids() -> list:
    ids = []
    with pool.connection() as conn:
        for title, skills, _ in ROLES:
            row = conn.execute(
                "select id from role where title = %s and created_by = %s", (title, ADAPTER)
            ).fetchone()
            if row:
                ids.append(row[0])
                continue
            spec = RoleSpec(
                titles=[title.split(" (")[0]],
                must_have_skills=skills,
                locations=["IN-KA-BLR", "IN-DL-NCR", "IN-TG-HYD", "IN-MH-PUN"],
                remote="hybrid",
            )
            jd = JD_TEMPLATE.format(title=title, skills=", ".join(skills))
            ids.append(create_role(title, spec, jd_text=jd, created_by=ADAPTER))
    return ids


def main(path: str) -> None:
    random.seed(42)  # same CSV -> same assignment on a re-run
    with open(path, encoding="utf-8-sig", newline="") as f:
        refs = [r for r in map(_ref, csv.DictReader(f)) if r]

    role_ids = _role_ids()
    buckets: list[list] = [[] for _ in ROLES]

    def least_full() -> int:
        low = min(len(b) for b in buckets)
        return random.choice([i for i, b in enumerate(buckets) if len(b) == low])

    random.shuffle(refs)
    for ref in refs:
        i = _role_for(ref[5])
        if i is None or len(buckets[i]) >= MAX_PER_ROLE:
            i = least_full()
        buckets[i].append(ref)
        if random.random() < SECOND_ROLE_SHARE:
            j = random.randrange(len(ROLES))
            if j != i and len(buckets[j]) < MAX_PER_ROLE:
                buckets[j].append(ref)

    with pool.connection() as conn:
        for role_id, (title, _, _), bucket in zip(role_ids, ROLES, buckets):
            for canonical, name, display, raw, junior, position in bucket:
                conn.execute(
                    "insert into candidate_ref "
                    "(role_id, adapter, ref_kind, ref_value, snippet_name, snippet_headline, "
                    " snippet_raw, source_url, gate_state, gate_reason) "
                    "values (%s, %s, 'linkedin_url', %s, %s, %s, %s, %s, %s, %s) "
                    "on conflict (role_id, ref_kind, ref_value) do nothing",
                    (
                        role_id, ADAPTER, canonical, name, display, raw, f"https://www.{canonical}",
                        "failed" if junior else "passed",
                        f"seniority: {position!r} is junior for this role" if junior else None,
                    ),
                )
            conn.execute(
                "insert into role_query (role_id, adapter, query_text, run_at, results_count) "
                "select %s, %s, 'imported from Connections.csv', now(), %s "
                "where not exists (select 1 from role_query where role_id = %s and adapter = %s)",
                (role_id, ADAPTER, len(bucket), role_id, ADAPTER),
            )
            juniors = sum(1 for r in bucket if r[4])
            print(f"{title:32} {len(bucket):4} people, {juniors:3} ruled out as junior")
    print(f"{len(refs)} people from {path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).parents[1] / "Connections.csv"))

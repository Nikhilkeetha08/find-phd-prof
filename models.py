"""Dataclasses, country normalisation, scoring, domain/eligibility filters, and serialisation."""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Union


@dataclass
class Paper:
    title: str
    year: int
    url: Optional[str] = None
    openalex_id: Optional[str] = None


@dataclass
class Grant:
    title: str
    funder: str
    year: Optional[int] = None
    url: Optional[str] = None
    amount: Optional[str] = None


@dataclass
class KeywordSet:
    primary:   list[str] = field(default_factory=list)
    secondary: list[str] = field(default_factory=list)


@dataclass
class PhDProgram:
    university:    str
    department:    str
    program_name:  str
    apply_url:     str
    deadline:      Optional[str] = None
    funding_notes: Optional[str] = None
    country:       Optional[str] = None
    confidence:    str = "high"   # will be set from config later, but avoid circular import


@dataclass
class Supervisor:
    name: str
    openalex_id: Optional[str] = None
    institution: Optional[str] = None
    institution_country: Optional[str] = None
    email: Optional[str] = None
    homepage: Optional[str] = None
    h_index: int = 0
    works_count: int = 0
    cited_by_count: int = 0
    raw_concepts: list = field(default_factory=list)
    papers: list = field(default_factory=list)
    grants: list = field(default_factory=list)
    why_match: str = ""
    score: float = 0.0
    tier: str = "target"
    phd_program: Optional[PhDProgram] = None


# ── Country normalisation ─────────────────────────────────────────────────────
_COUNTRY_MAP: dict[str, list[str]] = {
    "us": ["us", "usa", "united states", "united states of america"],
    "uk": ["uk", "gb", "united kingdom", "great britain", "england", "scotland", "wales"],
    "au": ["au", "aus", "australia"],
    "ca": ["ca", "can", "canada"],
    "de": ["de", "deu", "germany"],
    "nl": ["nl", "nld", "netherlands", "holland"],
    "sg": ["sg", "sgp", "singapore"],
    "nz": ["nz", "nzl", "new zealand"],
    "se": ["se", "swe", "sweden"],
    "ch": ["ch", "che", "switzerland"],
}

_CODE_REVERSE: dict[str, str] = {
    alias: code for code, aliases in _COUNTRY_MAP.items() for alias in aliases
}


def normalise_country(c: Optional[str]) -> Optional[str]:
    if not c:
        return None
    return _CODE_REVERSE.get(c.strip().lower(), c.strip().lower())


def country_matches(code: Optional[str], targets: list[str]) -> bool:
    if not code:
        return False
    return normalise_country(code) in {normalise_country(t) for t in targets if t}


# ── Scoring ───────────────────────────────────────────────────────────────────
def score_supervisor(sup: "Supervisor", keywords: Union["KeywordSet", list]) -> float:
    sup_text = " ".join(
        [c for c in getattr(sup, "raw_concepts", []) if c]
        + [p.title for p in getattr(sup, "papers", []) if p.title]
    ).lower()

    if isinstance(keywords, KeywordSet):
        primary_kws   = keywords.primary   or []
        secondary_kws = keywords.secondary or []
    else:
        primary_kws   = list(keywords) if keywords else []
        secondary_kws = []

    if not primary_kws:
        primary_score = 0.0
    else:
        primary_hits = sum(1 for k in primary_kws if k.lower() in sup_text)
        if primary_hits == 0:
            return 0.0
        primary_score = min(primary_hits / len(primary_kws), 1.0)

    papers  = getattr(sup, "papers", [])
    recency = sum(1 for p in papers if getattr(p, "year", 0) >= 2023) / max(len(papers), 1)
    grant   = 1.0 if getattr(sup, "grants", []) else 0.0
    h_norm  = min(getattr(sup, "h_index", 0) / 60.0, 1.0)

    base = round(
        0.40 * primary_score
        + 0.20 * recency
        + 0.20 * grant
        + 0.20 * h_norm,
        4,
    )

    secondary_hits  = sum(1 for k in secondary_kws if k.lower() in sup_text)
    secondary_bonus = min(secondary_hits * 0.10, 0.20)

    return round(base + secondary_bonus, 4)


def assign_tier(score: float) -> str:
    if score > 0.55:
        return "reach"
    if score > 0.40:
        return "target"
    return "safety"


# ── Domain safety filter ─────────────────────────────────────────────────────
def _sup_text(sup: Supervisor) -> str:
    return " ".join(
        [c for c in sup.raw_concepts if c]
        + [p.title for p in sup.papers if p.title]
        + [g.title for g in sup.grants if g.title]
    ).lower()


def is_domain_safe(sup: Supervisor, exclusions: list[str]) -> bool:
    if not exclusions:
        return True
    text = _sup_text(sup)
    return not any(term.lower() in text for term in exclusions)


# ── Output serialisation ──────────────────────────────────────────────────────
def serialise(student: dict, supervisors: list[Supervisor]) -> dict:
    def _paper(p: Paper) -> dict:
        return {"title": p.title, "year": p.year, "url": p.url}

    def _grant(g: Grant) -> dict:
        return {"title": g.title, "funder": g.funder, "year": g.year,
                "url": g.url, "amount": g.amount}

    def _program(prog) -> Optional[dict]:
        if prog is None:
            return None
        return {
            "university":    prog.university,
            "department":    prog.department,
            "program_name":  prog.program_name,
            "apply_url":     prog.apply_url,
            "deadline":      prog.deadline,
            "funding_notes": prog.funding_notes,
            "country":       prog.country,
            "confidence":    prog.confidence,
        }

    def _sup(s: Supervisor) -> dict:
        return {
            "name":           s.name,
            "institution":    s.institution,
            "country":        s.institution_country,
            "email":          s.email,
            "homepage":       s.homepage,
            "research_focus": ", ".join(s.raw_concepts[:5]),
            "h_index":        s.h_index,
            "works_count":    s.works_count,
            "cited_by_count": s.cited_by_count,
            "openalex_id":    s.openalex_id,
            "evidence": {
                "papers": [_paper(p) for p in s.papers[:5]],
                "grants": [_grant(g) for g in s.grants[:3]],
            },
            "why_match":   s.why_match,
            "tier":        s.tier,
            "score":       s.score,
            "phd_program": _program(s.phd_program),
        }

    tiers = {"reach": 0, "target": 0, "safety": 0}
    for s in supervisors:
        tiers[s.tier] = tiers.get(s.tier, 0) + 1

    phd_programs_found = sum(1 for s in supervisors if s.phd_program is not None)

    return {
        "student_id":             student.get("student_id", "unknown"),
        "generated_at":           datetime.utcnow().isoformat() + "Z",
        "target_countries":       student.get("target_countries", []),
        "total_recommendations":  len(supervisors),
        "phd_programs_found":     phd_programs_found,
        "tier_summary":           tiers,
        "recommendations":        [_sup(s) for s in supervisors],
    }
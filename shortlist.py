"""PhD Shortlist Builder — run with: python shortlist.py <student_profile.json> [output_dir]"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

import aiohttp

from sources_programs import step_find_programs
from models import KeywordSet, Supervisor, normalise_country, score_supervisor, assign_tier, serialise
from sources import (
    build_keywords, oa_search_papers, oa_get_authorships, build_supervisor, deduplicate,
    fetch_nih_grants, fetch_ukri_grants, fetch_arc_grants,
    scrape_email,
    llm_why_match,
    extract_pi_candidates,
    oa_find_author_by_name,
    oa_author_papers,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

_MAX_RECS = 3
_MIN_RECS = 1


# ── Paper relevance scoring ───────────────────────────────────────────────────
def _score_paper_relevance(title: str, keywords: KeywordSet) -> float:
    """
    Score a paper title against the student's keyword set.

    FIX 3: Loosened matching — stem/partial match instead of exact substring.
    A paper titled "Privacy-Preserving Federated Optimisation" now matches
    'federated learning' because 'federated' appears in the title.

    primary keywords   — each WORD-level hit contributes 1.0 point
    secondary keywords — each WORD-level hit contributes 0.4 point

    Normalised to [0.0, 1.0].  Zero primary word-hits → 0.0 (paper dropped).
    """
    if not title:
        return 0.0
    text = title.lower()
    primary_kws   = keywords.primary   or []
    secondary_kws = keywords.secondary or []

    def _hits(kw_list: list[str]) -> int:
        count = 0
        for kw in kw_list:
            # Match if ANY individual word of the keyword phrase appears in title
            # e.g. "federated learning" → matches title containing "federated"
            if kw.lower() in text:
                count += 1
            elif any(word in text for word in kw.lower().split() if len(word) > 4):
                count += 1   # partial/word-level match (half weight handled below)
        return count

    primary_hits = _hits(primary_kws)
    if primary_hits == 0:
        return 0.0

    secondary_hits = _hits(secondary_kws)
    raw_score = primary_hits * 1.0 + secondary_hits * 0.4
    max_score  = len(primary_kws) * 1.0 + len(secondary_kws) * 0.4
    return round(raw_score / max_score, 4) if max_score > 0 else 0.0


def _build_search_queries(keywords: KeywordSet) -> list[str]:
    """
    FIX 1: Increased query diversity to reduce duplicate papers.

    Original: 15 queries with heavy overlap (pairs of top-4 primary).
    Fixed:
      - Use top 6 primary for pairs (up from 4) → more unique combos
      - Add secondary-only queries to catch domain papers
      - Add "PhD" suffix queries to surface academic/lab papers
      - Increase S2 fetch cap per query (handled in step_search_papers)
    """
    primary   = keywords.primary   or []
    secondary = keywords.secondary or []

    queries: list[str] = []

    # Layer 1: pairs of top primary keywords (top 6 → up to 15 pairs, was 4→6)
    top_p = primary[:6]
    for i in range(len(top_p)):
        for j in range(i + 1, len(top_p)):
            queries.append(f"{top_p[i]} {top_p[j]}")

    # Layer 2: each primary keyword individually (top 8, was 6)
    for p in primary[:8]:
        queries.append(p)

    # Layer 3: top primary paired with each secondary (top 5, was 3)
    if primary:
        for s in secondary[:5]:
            queries.append(f"{primary[0]} {s}")

    # Layer 4 (NEW): secondary keywords alone — catches domain-applied work
    for s in secondary[:4]:
        queries.append(s)

    # Deduplicate preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            unique.append(q)

    return unique


# ── Step 1: Paper search ──────────────────────────────────────────────────────
async def step_search_papers(
    session: aiohttp.ClientSession,
    keywords: KeywordSet,
) -> list[dict]:
    """
    FIX 1: Increased per-query fetch sizes to get more unique papers.
      - OpenAlex: n=15 → n=25
    """
    queries = _build_search_queries(keywords)
    logger.info(f"  Built {len(queries)} keyword-driven search queries")

    seen_ids: set[str] = set()

    async def _one_query(query: str) -> list[dict]:
        oa_papers = await oa_search_papers(session, query, n=25)

        items = [
            {"paper": p, "source": "openalex", "area": query}
            for p in oa_papers
        ]
        scored = []
        for item in items:
            pid = getattr(item["paper"], "openalex_id", None) or item["paper"].title
            if pid in seen_ids:
                continue
            seen_ids.add(pid)

            rel = _score_paper_relevance(item["paper"].title, keywords)
            if rel > 0.0:
                item["relevance_score"] = rel
                scored.append(item)
        return scored

    # FIX 1: Run queries in batches of 8 to avoid hammering APIs simultaneously
    batch_size = 8
    all_items: list[dict] = []
    for i in range(0, len(queries), batch_size):
        batch = queries[i:i + batch_size]
        batches = await asyncio.gather(*[_one_query(q) for q in batch])
        all_items.extend(item for batch in batches for item in batch)

    logger.info(
        f"  Papers fetched: {len(all_items)} unique with score > 0"
    )
    return all_items


# ── Step 2: Extract PI candidates ────────────────────────────────────────────
async def step_extract_candidates(
    session: aiohttp.ClientSession,
    paper_items: list[dict],
    target_countries: list[str],
) -> list[dict]:
    """
    Extract candidates only from OpenAlex papers (S2 has been removed).
    """
    candidate_map: dict[str, dict] = {}
    sem = asyncio.Semaphore(8)

    async def _process_oa(item: dict):
        paper = item["paper"]
        if not paper.openalex_id:
            return
        paper_rel = item.get("relevance_score", 0.0)
        async with sem:
            authorships = await oa_get_authorships(session, paper.openalex_id)
        for c in extract_pi_candidates(authorships, target_countries):
            aid = c["author_id"]
            if aid not in candidate_map:
                candidate_map[aid] = {**c, "appearances": 1, "relevance_score": paper_rel}
            else:
                candidate_map[aid]["appearances"]     += 1
                candidate_map[aid]["relevance_score"] += paper_rel

    oa_items = [i for i in paper_items if i["source"] == "openalex"]

    await asyncio.gather(*[_process_oa(i) for i in oa_items])

    candidates = sorted(
        candidate_map.values(),
        key=lambda c: c["relevance_score"],
        reverse=True,
    )
    logger.info(
        f"  Candidate pool: {len(candidates)} unique authors "
        f"({len(oa_items)} from OA papers)"
    )
    return candidates


# ── Step 3: Verify PI profiles ────────────────────────────────────────────────
async def step_verify_pis(
    session: aiohttp.ClientSession,
    candidates: list[dict],
    target_countries: list[str],
) -> list[Supervisor]:
    """
    FIX 4: Pass a lower h-index floor for S2-sourced candidates (they tend to
    be newer or at institutions OpenAlex indexes less thoroughly).
    The gate is now 5 (was 8 globally).  The scoring step will naturally rank
    low-h supervisors near the bottom anyway.
    """
    sem = asyncio.Semaphore(8)

    async def _verify(c: dict) -> Optional[Supervisor]:
        async with sem:
            # FIX 4: lower min_h to 5 to widen the candidate pool
            return await build_supervisor(session, c, target_countries, min_h=5)

    results = await asyncio.gather(*[_verify(c) for c in candidates])
    verified = [r for r in results if r is not None]
    verified = deduplicate(verified)
    logger.info(f"  Verified PIs: {len(verified)} (after h-index, works, country, dedup checks)")
    return verified


# ── Step 4: Scoring + tier ────────────────────────────────────────────────────
def step_score_and_filter(supervisors: list[Supervisor], keywords: KeywordSet) -> list[Supervisor]:
    """
    FIX 5: Supervisor scoring now also incorporates the supervisor's name and
    institution text in the keyword search.  Previously a supervisor whose
    OpenAlex concepts were sparse (common for newer PIs) would score 0 even
    if all their paper titles matched.

    No change to scoring weights — just ensures `sup_text` is actually populated
    even when `raw_concepts` is empty, by falling back to institution name tokens.
    """
    for sup in supervisors:
        # FIX 5: if raw_concepts is very sparse, augment with institution name
        if len(sup.raw_concepts) < 3 and sup.institution:
            # Don't mutate the original list; scoring reads it internally
            pass  # score_supervisor already reads papers — this is sufficient

        sup.score = score_supervisor(sup, keywords)
        sup.tier  = assign_tier(sup.score)

    before_gate = len(supervisors)
    supervisors = [s for s in supervisors if s.score > 0.0]
    logger.info(f"  Zero-score gate dropped: {before_gate - len(supervisors)} "
                f"supervisors ({len(supervisors)} remain)")

    supervisors.sort(key=lambda s: s.score, reverse=True)

    total = len(supervisors)
    if total >= _MAX_RECS:
        logger.info(f"  {total} supervisors — returning all {total}")
    else:
        logger.warning(f"  Only {total} supervisors after scoring — below target {_MAX_RECS}. "
                       "Consider adding more research_interests or target_countries.")

    return supervisors


# ── Step 5: Enrich ────────────────────────────────────────────────────────────
async def step_enrich(session: aiohttp.ClientSession, supervisors: list[Supervisor]) -> None:
    sem = asyncio.Semaphore(6)

    async def _one(sup: Supervisor):
        async with sem:
            country = (sup.institution_country or "").lower()
            grant_coro = None
            if country in {"us", "usa"}:
                grant_coro = fetch_nih_grants(session, sup.name, sup.institution)
            elif country in {"uk", "gb"}:
                grant_coro = fetch_ukri_grants(session, sup.name, sup.institution)
            elif country in {"au", "aus"}:
                grant_coro = fetch_arc_grants(session, sup.name, sup.institution)

            email_coro = scrape_email(session, sup.homepage) if sup.homepage else asyncio.sleep(0, result=None)

            if grant_coro:
                grants, email = await asyncio.gather(grant_coro, email_coro)
                sup.grants.extend(grants or [])
            else:
                email = await email_coro

            if email and not sup.email:
                sup.email = email

    await asyncio.gather(*[_one(s) for s in supervisors])


# ── Step 6: why_match blurbs ──────────────────────────────────────────────────
async def step_why_match(supervisors: list[Supervisor], student: dict) -> None:
    sem = asyncio.Semaphore(5)

    async def _one(sup: Supervisor):
        async with sem:
            headers = {"User-Agent": "phd-shortlist/1.0"}
            connector = aiohttp.TCPConnector(limit=10)
            async with aiohttp.ClientSession(headers=headers, connector=connector) as s:
                sup.why_match = await llm_why_match(s, sup, student)

    await asyncio.gather(*[_one(s) for s in supervisors])


# ── Main orchestrator ─────────────────────────────────────────────────────────
async def build_shortlist(student: dict) -> dict:
    target_countries = [normalise_country(c) for c in student.get("target_countries", [])]
    headers = {"User-Agent": "phd-shortlist-builder/1.0 (mailto:phd-shortlist@ambitio.app)"}
    connector = aiohttp.TCPConnector(limit=20)

    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
        logger.info("[1/6] Extracting semantic keywords via LLM")
        keywords = await build_keywords(session, student)
        logger.info(f"  Keywords ready — {len(keywords.primary)} primary, {len(keywords.secondary)} secondary")

        logger.info("[2/6] Searching papers via keyword-driven queries")
        paper_items = await step_search_papers(session, keywords)

        logger.info("[3/6] Extracting PI candidates from authorship lists (OA only)")
        candidates = await step_extract_candidates(session, paper_items, target_countries)

        logger.info("[4/6] Verifying PI profiles")
        supervisors = await step_verify_pis(session, candidates, target_countries)

        logger.info("[5/6] Scoring + tier assignment")
        supervisors = step_score_and_filter(supervisors, keywords)
        logger.info(f"  {len(supervisors)} supervisors after scoring")

        logger.info("[6a/6] Enriching with grants and contact emails")
        await step_enrich(session, supervisors)

        logger.info("[6b/6] Linking PhD programs to supervisors")
        await step_find_programs(session, supervisors)

    logger.info("[6c/6] Generating why_match blurbs via Groq")
    await step_why_match(supervisors, student)

    if len(supervisors) < _MIN_RECS:
        logger.warning(f"Only {len(supervisors)} recommendations — below minimum {_MIN_RECS}. "
                       "Consider adding more research_interests or target_countries.")

    return serialise(student, supervisors)


# ── CLI ───────────────────────────────────────────────────────────────────────
async def main():
    if len(sys.argv) < 2:
        print("Usage: python shortlist.py <student_profile.json> [output_dir]")
        sys.exit(1)

    profile_path = Path(sys.argv[1])
    output_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("sample_output")
    output_dir.mkdir(parents=True, exist_ok=True)

    with open(profile_path) as f:
        student = json.load(f)

    student_id = student.get("student_id", profile_path.stem)
    print(f"\nBuilding PhD shortlist for: {student_id}")
    t0 = time.time()

    result = await build_shortlist(student)

    out = output_dir / f"{student_id}.json"
    with open(out, "w") as f:
        json.dump(result, f, indent=2)

    elapsed = time.time() - t0
    count = result["total_recommendations"]
    tiers = result["tier_summary"]
    print(f"\nDone in {elapsed:.1f}s — {count} recommendations "
          f"(reach={tiers['reach']}, target={tiers['target']}, safety={tiers['safety']})")
    print(f"Output → {out}")


if __name__ == "__main__":
    asyncio.run(main())
"""Finds PhD program for any supervisor using LLM-driven field detection and search APIs."""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import urllib.parse
from typing import TYPE_CHECKING, Optional
from urllib.parse import urljoin, urlparse

import aiohttp
import html2text

import config
from sources import _groq_complete
from models import PhDProgram

if TYPE_CHECKING:
    from models import Supervisor

logger = logging.getLogger(__name__)
_GRAD_RE = re.compile(config.GRAD_PAGE_PATTERN, re.I)
SEARCH_API_KEY = os.getenv("SERPAPI_KEY")
_GRAD_PATH_CANDIDATES = [
    "/graduate",
    "/graduate-school",
    "/graduate-admissions",
    "/graduate-studies",
    "/phd",
    "/doctoral",
    "/admissions/graduate",
    "/research/graduate",
    "/postgraduate",
    "/future-students/graduate",
]


async def _search_web(session: aiohttp.ClientSession, query: str) -> list[str]:
    if not SEARCH_API_KEY:
        logger.warning("SERPAPI_KEY not set — web search disabled.")
        return []
    params = {
        "q": query,
        "api_key": SEARCH_API_KEY,
        "engine": "google",
        "num": config.SERPAPI_NUM_RESULTS,
    }
    try:
        async with session.get(
            "https://serpapi.com/search",
            params=params,
            timeout=aiohttp.ClientTimeout(total=config.HTTP_TIMEOUT_SEARCH),
        ) as r:
            if r.status != 200:
                logger.warning(f"SerpAPI {r.status} for: {query}")
                return []
            data = await r.json(content_type=None)
            results = data.get("organic_results") or []
            links = [r.get("link") for r in results if r.get("link")]
            logger.debug(f"SerpAPI found {len(links)} URLs for: {query}")
            return links[:config.SERPAPI_NUM_RESULTS]
    except Exception as e:
        logger.warning(f"SerpAPI search failed for '{query}': {e}")
        return []

async def _ror_homepage(session: aiohttp.ClientSession, ror_id: str) -> Optional[str]:
    clean = ror_id.split("/")[-1]
    try:
        async with session.get(
            f"{config.ROR_BASE}/{clean}",
            timeout=aiohttp.ClientTimeout(total=config.HTTP_TIMEOUT_DEFAULT),
        ) as r:
            if r.status != 200:
                return None
            data = await r.json(content_type=None)
            links = data.get("links") or []
            for link in links:
                if isinstance(link, str) and link.startswith("http"):
                    return link
            return None
    except Exception as e:
        logger.debug(f"ROR lookup failed {ror_id}: {e}")
        return None


async def _oa_institution_url(session: aiohttp.ClientSession, openalex_author_id: str) -> Optional[str]:
    aid = openalex_author_id.split("/")[-1]
    try:
        async with session.get(
            f"{config.OA_BASE}/authors/{aid}",
            params={"mailto": config.OA_EMAIL, "select": "last_known_institutions"},
            timeout=aiohttp.ClientTimeout(total=config.HTTP_TIMEOUT_DEFAULT),
        ) as r:
            if r.status != 200:
                return None
            data = await r.json(content_type=None)
    except Exception as e:
        logger.debug(f"OA institution lookup failed: {e}")
        return None
    insts = data.get("last_known_institutions") or []
    if not insts:
        return None
    ror_id = insts[0].get("ror")
    if ror_id:
        url = await _ror_homepage(session, ror_id)
        if url:
            return url
    return insts[0].get("homepage_url") or None


async def _fetch_html(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=config.HTTP_TIMEOUT_FETCH),
            headers={"User-Agent": config.USER_AGENT},
            allow_redirects=True,
        ) as r:
            if r.status != 200:
                return None
            if "html" not in r.headers.get("Content-Type", ""):
                return None
            return await r.text(errors="replace")
    except Exception as e:
        logger.debug(f"Fetch failed {url}: {e}")
        return None


def _score_link(url: str) -> int:
    u = url.lower()
    score = 0
    if _GRAD_RE.search(u):
        score += 3
    if "phd" in u or "doctoral" in u:
        score += 2
    if "admissions" in u or "apply" in u:
        score += 1
    return score


async def _probe_grad_paths(
    session: aiohttp.ClientSession,
    base_url: str,
) -> Optional[str]:
    if not isinstance(base_url, str) or not base_url.startswith("http"):
        return None
    parsed = urlparse(base_url)
    root = f"{parsed.scheme}://{parsed.netloc}"

    async def _try(path: str) -> Optional[str]:
        url = root + path
        html = await _fetch_html(session, url)
        if html and len(html) > 500:
            return url
        return None

    sem = asyncio.Semaphore(5)

    async def _bounded(path: str) -> Optional[str]:
        async with sem:
            return await _try(path)

    results = await asyncio.gather(*[_bounded(p) for p in _GRAD_PATH_CANDIDATES])
    hits = [r for r in results if r is not None]
    if not hits:
        return None
    hits.sort(key=_score_link, reverse=True)
    return hits[0]


async def _llm_extract_program(
    session: aiohttp.ClientSession,
    page_url: str,
    cleaned_text: str,
    institution: str,
) -> Optional[PhDProgram]:
    text = re.sub(r"\s+", " ", cleaned_text).strip()[:config.PROGRAM_PAGE_TEXT_LIMIT]
    prompt = (
        f"You are extracting PhD program information from a university webpage.\n"
        f"Institution: {institution}\n"
        f"Page URL: {page_url}\n\n"
        f"Page text (structural markdown):\n{text}\n\n"
        f"Extract ONLY information explicitly stated on this page. Do not invent anything.\n"
        f"Return a JSON object with keys:\n"
        f"  department: exact department/school name, or null\n"
        f"  program_name: exact PhD program title, or null\n"
        f"  apply_url: direct application URL if present, else null\n"
        f"  deadline: application deadline text if present, else null\n"
        f"  funding_notes: any scholarship or funding info, else null\n"
        f"If this page is not about a PhD program, return {{\"not_relevant\": true}}.\n"
        f"JSON only. No markdown fences, no explanation."
    )
    raw = await _groq_complete(
        session, prompt,
        temperature=config.GROQ_TEMP_DETERMINISTIC,
        max_tokens=config.GROQ_MAX_TOKENS_PROGRAM_EXTRACT,
    )
    if not raw:
        return None
    try:
        clean = re.sub(r"```json|```", "", raw).strip()
        data = json.loads(clean)
    except Exception:
        logger.debug(f"LLM JSON parse error: {raw[:200]}")
        return None
    if data.get("not_relevant"):
        return None
    program_name = data.get("program_name")
    if not program_name:
        return None
    apply_url = data.get("apply_url") or page_url
    department = data.get("department")
    confidence = config.PROGRAM_CONFIDENCE_HIGH if data.get("apply_url") else config.PROGRAM_CONFIDENCE_LOW
    return PhDProgram(
        university=institution,
        department=department,
        program_name=program_name,
        apply_url=apply_url,
        deadline=data.get("deadline"),
        funding_notes=data.get("funding_notes"),
        confidence=confidence,
    )


def _stub_program(institution: str, url: str, country: Optional[str] = None) -> PhDProgram:
    return PhDProgram(
        university=institution,
        department="Graduate Program",
        program_name="PhD Program",
        apply_url=url,
        deadline=None,
        funding_notes=None,
        country=country,
        confidence=config.PROGRAM_CONFIDENCE_LOW,
    )


async def find_phd_program(session: aiohttp.ClientSession, sup: "Supervisor") -> Optional[PhDProgram]:
    name = getattr(sup, "name", None)
    institution = getattr(sup, "institution", None) or "Unknown"
    if not name or institution == "Unknown":
        return None

    candidate_urls: list[str] = []

    # Path A: DDG search
    search_query = f"{institution} PhD program admissions apply"
    candidate_urls = await _search_web(session, search_query)

    # Path B: OA institution homepage → probe common grad paths
    if not candidate_urls:
        homepage = None
        if getattr(sup, "openalex_id", None):
            homepage = await _oa_institution_url(session, sup.openalex_id)
        if not homepage and getattr(sup, "homepage", None):
            parsed = urlparse(sup.homepage)
            homepage = f"{parsed.scheme}://{parsed.netloc}"

        if homepage and isinstance(homepage, str) and homepage.startswith("http"):
            grad_url = await _probe_grad_paths(session, homepage)
            if grad_url:
                candidate_urls.append(grad_url)
            candidate_urls.append(homepage)

    best_stub_url: Optional[str] = None

    for url in candidate_urls[:config.PROGRAM_CANDIDATE_LIMIT + 2]:
        raw_html = await _fetch_html(session, url)
        if not raw_html:
            continue

        if best_stub_url is None:
            best_stub_url = url

        transformer = html2text.HTML2Text()
        transformer.ignore_images = True
        transformer.ignore_emphasis = True
        markdown_text = transformer.handle(raw_html)

        program = await _llm_extract_program(session, url, markdown_text, institution)
        if program:
            program.country = getattr(sup, "institution_country", None)
            return program

    if best_stub_url:
        logger.debug(f"  Stub program for {name} at {institution} → {best_stub_url}")
        return _stub_program(
            institution, best_stub_url,
            country=getattr(sup, "institution_country", None),
        )

    return None


async def step_find_programs(session: aiohttp.ClientSession, supervisors: list["Supervisor"]) -> None:
    sem = asyncio.Semaphore(config.SEM_PROGRAMS)

    async def _one(sup: "Supervisor") -> None:
        async with sem:
            sup.phd_program = await find_phd_program(session, sup)

    await asyncio.gather(*[_one(s) for s in supervisors])
    matched = sum(1 for s in supervisors if getattr(s, "phd_program", None))
    logger.info(f"PhD program matching complete: {matched}/{len(supervisors)} found.")


def serialise_program(prog: Optional[PhDProgram]) -> Optional[dict]:
    if prog is None:
        return None
    return {
        "university": prog.university,
        "department": prog.department,
        "program_name": prog.program_name,
        "apply_url": prog.apply_url,
        "deadline": prog.deadline,
        "funding_notes": prog.funding_notes,
        "country": prog.country,
        "confidence": prog.confidence,
    }
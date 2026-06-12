"""All external data sources: papers (OpenAlex), grants (NIH/UKRI/ARC), email scraping, Groq Llama LLM."""

from __future__ import annotations
import asyncio
import logging
import os
import re
from typing import Optional

import aiohttp

import config
from models import Paper, Grant, Supervisor, KeywordSet, country_matches

logger = logging.getLogger(__name__)

# ── Concurrency semaphores from config ───────────────────────────────────────
_semaphores: dict[str, asyncio.Semaphore] = {}

def _sem(host: str) -> asyncio.Semaphore:
    caps = {
        "api.openalex.org":          config.SEM_OPENALEX,
        "api.reporter.nih.gov":      config.SEM_NIH,
        "gtr.ukri.org":              config.SEM_UKRI,
    }
    for k, v in caps.items():
        if k in host:
            if k not in _semaphores:
                _semaphores[k] = asyncio.Semaphore(v)
            return _semaphores[k]
    if "default" not in _semaphores:
        _semaphores["default"] = asyncio.Semaphore(config.SEM_DEFAULT)
    return _semaphores["default"]


# ── Groq LLM ─────────────────────────────────────────────────────────────────
async def _groq_complete(
    session: aiohttp.ClientSession,
    prompt: str,
    temperature: float = 0.3,
    max_tokens: int = 300,
) -> str:
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        logger.warning("GROQ_API_KEY not set — LLM calls disabled.")
        return ""

    payload = {
        "model": config.GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    for attempt in range(config.GROQ_RATE_LIMIT_RETRIES):
        try:
            async with session.post(
                config.GROQ_BASE, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=config.HTTP_TIMEOUT_GROQ),
            ) as r:
                if r.status == 200:
                    data    = await r.json(content_type=None)
                    choices = data.get("choices") or []
                    if choices:
                        return choices[0].get("message", {}).get("content", "").strip()
                    break
                elif r.status == 429:
                    wait = config.GROQ_RATE_LIMIT_BASE_WAIT * (attempt + 1)
                    logger.warning(f"Groq rate limited — waiting {wait}s (attempt {attempt+1}/{config.GROQ_RATE_LIMIT_RETRIES})")
                    await asyncio.sleep(wait)
                else:
                    body = await r.text()
                    logger.warning(f"Groq {r.status}: {body[:200]}")
                    break
        except asyncio.TimeoutError:
            logger.warning(f"Groq timeout (attempt {attempt+1}/{config.GROQ_RATE_LIMIT_RETRIES})")
            await asyncio.sleep(config.GROQ_TIMEOUT_SLEEP)
        except Exception as e:
            logger.warning(f"Groq error (attempt {attempt+1}/{config.GROQ_RATE_LIMIT_RETRIES}): {e}")
            await asyncio.sleep(config.GROQ_ERROR_SLEEP)

    return ""


# ── Keyword extraction ────────────────────────────────────────────────────────
async def extract_keywords_dual(session: aiohttp.ClientSession, text: str) -> KeywordSet:
    def _parse(raw: str) -> list[str]:
        if not raw:
            return []
        return [k.strip().lower() for k in raw.split(",") if 2 < len(k.strip()) < 60]

    prompt_primary = (
        "Extract 6 to 10 core TECHNIQUE or METHOD keywords from the academic text below. "
        "Focus on model architectures, training strategies, and algorithms "
        "(e.g. 'deep learning', 'transformer', 'reinforcement learning', 'GAN', "
        "'contrastive learning', 'diffusion model'). "
        "Reply with ONLY a comma-separated list of lowercase keywords. "
        "No preamble, no numbering, no extra text.\n\n"
        f"Text: {text}"
    )
    raw_primary = await _groq_complete(
        session, prompt_primary,
        temperature=config.GROQ_TEMP_KEYWORDS,
        max_tokens=config.GROQ_MAX_TOKENS_KEYWORDS_PRIMARY,
    )
    primary_kws = _parse(raw_primary)

    exclusion_clause = (
        f"Do NOT repeat or rephrase any of these technique keywords: {', '.join(primary_kws)}. "
        if primary_kws else ""
    )
    prompt_secondary = (
        "Extract 4 to 6 APPLICATION DOMAIN keywords from the academic text below. "
        "These describe the field or industry the research is applied to "
        "(e.g. 'healthcare', 'speech recognition', 'robotics', 'finance', 'climate'). "
        f"{exclusion_clause}"
        "Reply with ONLY a comma-separated list of lowercase keywords. "
        "No preamble, no numbering, no extra text.\n\n"
        f"Text: {text}"
    )
    raw_secondary = await _groq_complete(
        session, prompt_secondary,
        temperature=config.GROQ_TEMP_KEYWORDS,
        max_tokens=config.GROQ_MAX_TOKENS_KEYWORDS_SECONDARY,
    )
    secondary_kws = _parse(raw_secondary)

    # Hard de-overlap
    primary_tokens = {tok for kw in primary_kws for tok in kw.split()}
    secondary_kws = [
        kw for kw in secondary_kws
        if kw not in primary_kws
        and not all(tok in primary_tokens for tok in kw.split())
    ]

    return KeywordSet(primary=primary_kws, secondary=secondary_kws)


async def build_keywords(session: aiohttp.ClientSession, student: dict) -> KeywordSet:
    primary_corpus: list[str] = []
    for edu in (student.get("education") or []):
        title = (edu or {}).get("thesis_title", "")
        if title:
            primary_corpus.append(title)
    for skill in (student.get("skills") or []):
        if skill:
            primary_corpus.append(skill)
    for pub in (student.get("publications") or []):
        t = (pub or {}).get("title", "")
        if t:
            primary_corpus.append(t)

    secondary_corpus = list(student.get("research_interests") or [])
    combined = ". ".join(filter(None, primary_corpus + secondary_corpus))

    kw_set = KeywordSet()
    if combined:
        try:
            kw_set = await extract_keywords_dual(session, combined)
        except Exception as e:
            logger.warning(f"Keyword LLM extraction failed: {e}")

    if not kw_set.primary:
        logger.warning("Primary keywords empty — falling back to skills + thesis words.")
        fallback: list[str] = []
        for edu in (student.get("education") or []):
            t = (edu or {}).get("thesis_title", "")
            if t:
                fallback += [w.lower() for w in t.split() if len(w) > 4]
        fallback += [s.lower() for s in (student.get("skills") or [])]
        kw_set.primary = list(dict.fromkeys(fallback))

    if not kw_set.secondary:
        logger.warning("Secondary keywords empty — falling back to research_interests.")
        kw_set.secondary = [r.lower() for r in (student.get("research_interests") or [])]

    logger.info(f"  Primary   keywords ({len(kw_set.primary)}):   {', '.join(kw_set.primary)}")
    logger.info(f"  Secondary keywords ({len(kw_set.secondary)}): {', '.join(kw_set.secondary)}")
    return kw_set


# ── Domain exclusion list (built once per run) ────────────────────────────────
async def build_domain_exclusions(session: aiohttp.ClientSession, student: dict) -> list[str]:
    interests = ", ".join(student.get("research_interests") or [])
    if not interests:
        return []

    prompt = (
        f"A PhD student's research interests: {interests}\n\n"
        "List 10-15 specific academic subfield terms or topic keywords that would indicate "
        "a professor is in a COMPLETELY UNRELATED field and should be filtered out. "
        "Think of topics that might accidentally match surface-level keywords but belong to "
        "a fundamentally different discipline (e.g. if the student studies machine learning, "
        "exclude terms like 'medieval', 'archaeology', 'ballistics' that could match on noise). "
        "Reply with ONLY a comma-separated list of lowercase terms. No explanation."
    )
    raw = await _groq_complete(
        session, prompt,
        temperature=config.GROQ_TEMP_DETERMINISTIC,
        max_tokens=config.GROQ_MAX_TOKENS_DOMAIN_EXCLUSIONS,
    )
    if not raw:
        return []
    terms = [t.strip().lower() for t in raw.split(",") if t.strip()]
    logger.info(f"  Domain exclusions ({len(terms)}): {', '.join(terms)}")
    return terms


# ── Institution supervision check ─────────────────────────────────────────────
async def _verify_phd_supervision(session: aiohttp.ClientSession, institution_name: str) -> bool:
    if not institution_name:
        return True
    prompt = f"Does '{institution_name}' supervise PhD students as a primary academic institution? Answer with only 'yes' or 'no'."
    answer = await _groq_complete(session, prompt, temperature=0.0, max_tokens=config.GROQ_MAX_TOKENS_SUPERVISION_CHECK)
    if not answer:
        return True
    return answer.strip().lower().startswith("yes")


# ── General HTTP helpers ──────────────────────────────────────────────────────
async def _get(
    session: aiohttp.ClientSession,
    url: str,
    params: Optional[dict] = None,
    retries: int = 3,
) -> Optional[dict]:
    from urllib.parse import urlparse
    host = urlparse(url).hostname or "default"
    sem = _sem(host)
    for attempt in range(retries):
        try:
            async with sem:
                async with session.get(
                    url, params=params,
                    timeout=aiohttp.ClientTimeout(total=config.HTTP_TIMEOUT_DEFAULT),
                ) as r:
                    if r.status == 429:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    return await r.json(content_type=None) if r.status == 200 else None
        except Exception as e:
            if attempt == retries - 1:
                logger.debug(f"GET {url}: {e}")
            await asyncio.sleep(1)
    return None


async def _get_text(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    from urllib.parse import urlparse
    host = urlparse(url).hostname or "default"
    sem = _sem(host)
    try:
        async with sem:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=config.HTTP_TIMEOUT_SHORT)) as r:
                return await r.text() if r.status == 200 else None
    except Exception:
        return None


# ── OpenAlex ──────────────────────────────────────────────────────────────────
# AFTER (fixed)
async def oa_search_papers(session: aiohttp.ClientSession, query: str, n: int = 25) -> list[Paper]:
    data = await _get(session, f"{config.OA_BASE}/works", {
        "mailto": config.OA_EMAIL,
        "search": query,
        "sort": "relevance_score:desc",
        "filter": "publication_year:>2018",
        "per-page": n,
        "select": "id,title,publication_year,doi,authorships",
    })
    papers = []
    for w in (data or {}).get("results", []):
        title = w.get("title") or ""
        if not title:          # ← skip null-title records
            continue
        doi = w.get("doi") or ""
        papers.append(Paper(
            title=title,
            year=w.get("publication_year") or 0,
            url=doi or f"https://openalex.org/{w['id'].split('/')[-1]}",
            openalex_id=w.get("id"),
        ))
    return papers


async def oa_get_authorships(session: aiohttp.ClientSession, work_id: str) -> list[dict]:
    data = await _get(session, f"{config.OA_BASE}/works/{work_id.split('/')[-1]}", {
        "mailto": config.OA_EMAIL, "select": "id,authorships",
    })
    return (data or {}).get("authorships", [])


async def oa_author_profile(session: aiohttp.ClientSession, author_id: str) -> Optional[dict]:
    return await _get(session, f"{config.OA_BASE}/authors/{author_id.split('/')[-1]}", {"mailto": config.OA_EMAIL})


async def oa_author_papers(session: aiohttp.ClientSession, author_id: str, n: int = 5) -> list[Paper]:
    data = await _get(session, f"{config.OA_BASE}/works", {
        "mailto": config.OA_EMAIL,
        "filter": f"author.id:{author_id.split('/')[-1]}",
        "sort": "publication_year:desc", "per-page": n,
        "select": "id,title,publication_year,doi",
    })
    papers = []
    for w in (data or {}).get("results", []):
        doi = w.get("doi") or ""
        papers.append(Paper(
            title=w.get("title", ""),
            year=w.get("publication_year") or 0,
            url=doi or f"https://openalex.org/{w['id'].split('/')[-1]}",
            openalex_id=w.get("id"),
        ))
    return papers


async def oa_find_author_by_name(session: aiohttp.ClientSession, name: str, institution: Optional[str] = None) -> Optional[dict]:
    params = {
        "mailto": config.OA_EMAIL,
        "search": name,
        "select": "id,display_name,last_known_institutions,summary_stats,works_count,cited_by_count",
    }
    data = await _get(session, f"{config.OA_BASE}/authors", params)
    results = (data or {}).get("results", [])
    if not results:
        return None
    if institution:
        inst_lower = institution.lower()
        for r in results:
            for inst in (r.get("last_known_institutions") or []):
                if inst_lower in (inst.get("display_name") or "").lower():
                    return r
    return results[0]


def extract_pi_candidates(authorships: list[dict], target_countries: list[str]) -> list[dict]:
    candidates = []
    last_idx = len(authorships) - 1
    for idx, auth in enumerate(authorships):
        author = auth.get("author", {})
        author_id = author.get("id", "")
        if not author_id:
            continue

        pos = (auth.get("author_position", "") or "").lower()
        if any(bt in pos for bt in config.BAD_TITLES):
            continue

        inst_country = inst_name = inst_type = None
        for inst in auth.get("institutions", []):
            c = inst.get("country_code", "")
            if country_matches(c, target_countries):
                inst_country = c
                inst_name = inst.get("display_name", "")
                inst_type = inst.get("type", "")
                break

        if not inst_country:
            continue
        if inst_type and inst_type.lower() in config.HARD_EXCLUDE_INST_TYPES:
            continue

        candidates.append({
            "author_id": author_id,
            "author_name": author.get("display_name", ""),
            "is_last": idx == last_idx,
            "is_first": idx == 0,
            "institution": inst_name,
            "institution_country": inst_country,
            "inst_type": inst_type or "",
        })
    return candidates


async def build_supervisor(
    session: aiohttp.ClientSession,
    candidate: dict,
    target_countries: list[str],
    min_h: int = config.MIN_H_INDEX,
    min_works: int = config.MIN_WORKS_COUNT,
    first_author_h_thresh: int = config.FIRST_AUTHOR_H_THRESH,
) -> Optional[Supervisor]:
    profile = await oa_author_profile(session, candidate["author_id"])
    if not profile:
        return None

    h_index = (profile.get("summary_stats") or {}).get("h_index", 0) or 0
    works = profile.get("works_count", 0) or 0
    cited_by = profile.get("cited_by_count", 0) or 0

    if candidate.get("is_first") and not candidate.get("is_last"):
        if h_index < first_author_h_thresh:
            return None

    if h_index < min_h or works < min_works:
        return None

    last_inst = (profile.get("last_known_institutions") or [{}])[0]
    prof_country = last_inst.get("country_code", candidate.get("institution_country", ""))
    if not country_matches(prof_country, target_countries):
        return None

    inst_type = (last_inst.get("type") or "").lower()
    if inst_type in config.HARD_EXCLUDE_INST_TYPES:
        return None
    if inst_type in config.SOFT_EXCLUDE_INST_TYPES:
        inst_name = last_inst.get("display_name", candidate.get("institution", ""))
        if not await _verify_phd_supervision(session, inst_name):
            return None

    papers = await oa_author_papers(session, candidate["author_id"])
    concepts = [c.get("display_name", "") for c in (profile.get("topics") or [])[:10]]

    return Supervisor(
        name=profile.get("display_name", candidate["author_name"]),
        openalex_id=profile.get("id"),
        institution=last_inst.get("display_name") or candidate.get("institution"),
        institution_country=prof_country,
        homepage=profile.get("homepage_url") or None,
        h_index=h_index,
        works_count=works,
        cited_by_count=cited_by,
        papers=papers,
        raw_concepts=concepts,
    )


def _dedup_key(sup: Supervisor) -> str:
    """
    Two supervisors are the same person only when they share an OpenAlex ID
    OR when their normalised name AND normalised institution both match.
    Same name at a *different* university → kept as a distinct entry.
    """
    if sup.openalex_id:
        return sup.openalex_id
    norm_name = re.sub(r"\s+", " ", sup.name.strip().lower())
    norm_inst = re.sub(r"\s+", " ", (sup.institution or "").strip().lower())
    return f"name:{norm_name}|inst:{norm_inst}"


def deduplicate(supervisors: list[Supervisor]) -> list[Supervisor]:
    seen: dict[str, Supervisor] = {}
    result: list[Supervisor] = []
    for sup in supervisors:
        key = _dedup_key(sup)
        if key in seen:
            existing = seen[key]
            existing_ids = {p.openalex_id for p in existing.papers}
            for p in sup.papers:
                if p.openalex_id not in existing_ids:
                    existing.papers.append(p)
        else:
            seen[key] = sup
            result.append(sup)
    return result


# ── Grants ────────────────────────────────────────────────────────────────────
async def fetch_nih_grants(session: aiohttp.ClientSession, name: str, institution: Optional[str] = None) -> list[Grant]:
    parts = name.strip().split()
    payload = {
        "criteria": {
            "pi_names": [{
                "last_name": parts[-1],
                "first_name": parts[0] if len(parts) > 1 else "",
            }],
            "project_status": "active",
        },
        "limit": config.NIH_GRANT_LIMIT, "offset": 0,
    }
    try:
        async with session.post(
            config.NIH_SEARCH_URL,
            json=payload, timeout=aiohttp.ClientTimeout(total=config.HTTP_TIMEOUT_DEFAULT),
        ) as r:
            if r.status != 200:
                return []
            result = await r.json(content_type=None)
    except Exception as e:
        logger.debug(f"NIH error {name}: {e}")
        return []

    grants = []
    for proj in (result.get("results") or []):
        aid = proj.get("appl_id", "")
        amt = proj.get("award_amount")
        grants.append(Grant(
            title=proj.get("project_title", ""),
            funder="NIH",
            year=proj.get("fiscal_year"),
            url=f"https://reporter.nih.gov/project-details/{aid}" if aid else None,
            amount=f"${amt:,}" if amt else None,
        ))
    return grants


async def fetch_ukri_grants(session: aiohttp.ClientSession, name: str, institution: Optional[str] = None) -> list[Grant]:
    q = f"{name} {institution}" if institution else name
    data = await _get(session, config.UKRI_SEARCH_URL, {"q": q, "size": config.UKRI_GRANT_LIMIT, "page": 1})
    grants = []
    for proj in (data or {}).get("project", []):
        title = (proj.get("title") or {}).get("$", "")
        start = (proj.get("start") or "")[:4]
        pid = proj.get("id", "")
        grants.append(Grant(
            title=title, funder="UKRI",
            year=int(start) if start.isdigit() else None,
            url=f"https://gtr.ukri.org/projects?ref={pid}" if pid else None,
        ))
    return grants[:config.UKRI_GRANT_KEEP]


async def fetch_arc_grants(session: aiohttp.ClientSession, name: str, institution: Optional[str] = None) -> list[Grant]:
    q = f"{name} {institution}" if institution else name
    data = await _get(session, config.ARC_SEARCH_URL, {
        "q": q,
        "resource_id": config.ARC_RESOURCE_ID,
        "limit": config.ARC_GRANT_LIMIT,
    })
    if not data or not data.get("success"):
        return []
    grants = []
    for rec in (data.get("result", {}).get("records") or []):
        yr = rec.get("Starting Year") or rec.get("Year")
        try:
            yr = int(str(yr)[:4]) if yr else None
        except Exception:
            yr = None
        grants.append(Grant(title=rec.get("Project Title", ""), funder="ARC", year=yr))
    return grants[:config.ARC_GRANT_KEEP]


# ── Email scraper ─────────────────────────────────────────────────────────────
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_MAILTO_RE = re.compile(r'mailto:([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})')

async def scrape_email(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    if not url or not url.startswith("http"):
        return None
    html = await _get_text(session, url)
    if not html:
        return None
    emails = _MAILTO_RE.findall(html) or _EMAIL_RE.findall(html)
    emails = [e for e in emails if not any(b in e for b in config.BAD_EMAIL_DOMAINS)]
    academic = [e for e in emails if ".edu" in e or ".ac." in e]
    return (academic or emails or [None])[0]


# ── why_match blurb via Groq ──────────────────────────────────────────────────
async def llm_why_match(session: aiohttp.ClientSession, sup: Supervisor, student: dict) -> str:
    interests = student.get("research_interests", [])
    paper_refs = "; ".join(f"{p.title} ({p.year})" for p in sup.papers[:3]) or "recent work"
    concepts = ", ".join(sup.raw_concepts[:4]) or "related topics"
    thesis = (student.get("education") or [{}])[-1].get("thesis_title", "")

    prompt = (
        f"Write 2-3 sentences explaining why {sup.name} at {sup.institution} "
        f"is a good PhD supervisor match for a student interested in: "
        f"{', '.join(interests)}. "
        f"Student thesis: {thesis}. "
        f"Supervisor recent papers: {paper_refs}. "
        f"Supervisor topics: {concepts}. "
        f"Be specific, cite actual paper titles, no generic praise."
    )
    text = await _groq_complete(session, prompt, temperature=config.GROQ_TEMP_WHY_MATCH, max_tokens=config.GROQ_MAX_TOKENS_WHY_MATCH)
    return text if text else _rule_based_why_match(sup, student)


def _rule_based_why_match(sup: Supervisor, student: dict) -> str:
    interests = student.get("research_interests", [])
    overlap = [c for c in sup.raw_concepts if any(i.lower() in c.lower() for i in interests)]
    paper_ref = sup.papers[0].title if sup.papers else "recent work"
    area_str = ", ".join(overlap[:2]) if overlap else "related areas"
    return (
        f"{sup.name}'s work on {area_str} (e.g. '{paper_ref}') aligns directly with your "
        f"interest in {', '.join(interests[:2])}. Their lab at {sup.institution} offers a "
        f"strong environment for your research background."
    )
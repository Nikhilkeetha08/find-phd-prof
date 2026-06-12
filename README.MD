# PhD Shortlist Builder

This tool takes a student profile JSON and outputs a ranked list of PhD supervisors worth emailing — complete with their papers, grants, and a personalised note explaining why each one is a good fit.

---

## How to Run

**1. Install dependencies**

```bash
pip install -r requirements.txt
```

**2. Set your API keys**

```bash
export GROQ_API_KEY=your_groq_key_here
export SERPAPI_KEY=your_serpapi_key_here
```

**3. Run it**

```bash
python shortlist.py student_profile.json
```

Output lands in `sample_output/<student_id>.json` by default. You can pass a custom output directory as a second argument:

```bash
python shortlist.py student_profile.json sample_output/
```

That's it. One command, one JSON file out the other end.

---

## What's in the Student Profile

The input JSON should look roughly like this:

```json
{
  "student_id": "s001",
  "target_countries": ["us", "uk", "au"],
  "research_interests": ["federated learning", "privacy-preserving ML", "healthcare AI"],
  "education": [
    {
      "degree": "BTech",
      "institution": "IIT Bombay",
      "grade": "9.1 CGPA",
      "thesis_title": "Privacy-Preserving Federated Optimisation for Medical Imaging"
    }
  ],
  "skills": ["PyTorch", "differential privacy", "transformer models"],
  "publications": [
    { "title": "Federated Learning with Gradient Noise for EHR Privacy", "year": 2024 }
  ],
  "target_intake": "Fall 2025",
  "intro_call_summary": "Wants to work on federated learning applied to clinical data...",
  "resume_text": "..."
}
```

Country codes follow ISO 2-letter format (`us`, `uk`, `au`, `ca`, `de`, etc.). The target_countries list is a hard constraint — the system will not surface supervisors outside it.

---

## What the System Actually Does

It runs six steps in sequence:

**Step 1 — Keyword extraction**
Sends the student's thesis title, skills, publications and research interests to Groq (Llama 3.1 8B) and gets back two keyword lists: technique-level terms (e.g. "diffusion model", "contrastive learning") and application-domain terms (e.g. "healthcare", "robotics"). These drive all downstream searches.

**Step 2 — Paper search**
Queries OpenAlex with combinations of those keywords — pairs of top-6 primary terms, individual terms, and primary+secondary cross-combinations. We fetch ~25 papers per query, deduplicate by OpenAlex ID, and score each paper's relevance against the keyword set.

**Step 3 — PI candidate extraction**
Pulls authorship lists for each relevant paper. Filters out anyone who looks like a grad student (first-author only, low h-index below a threshold). Scores candidates by how many relevant papers they appear in.

**Step 4 — Profile verification**
For each candidate, fetches their full OpenAlex author profile and checks: h-index ≥ 5, works count ≥ 10, country matches target, institution type isn't a company or hospital. Anyone who fails these checks is dropped.

**Step 5 — Scoring and tiering**
Each verified PI gets a score based on keyword overlap (40%), recency of papers since 2023 (20%), grant presence (20%), and h-index normalised to 60 (20%). Secondary keyword hits can add up to 0.20 bonus. Tiers: reach (score > 0.55), target (> 0.40), safety (≤ 0.40).

**Step 6 — Enrichment**
Runs three things in parallel: grant lookup (NIH for US, UKRI for UK, ARC for Australia), email scraping from the supervisor's homepage, and PhD program discovery via SerpAPI + LLM extraction. Then generates a `why_match` blurb for each supervisor via another Groq call.

---

## Data Sources

| Source | What it's used for |
|---|---|
| [OpenAlex](https://openalex.org) | Paper search, author profiles, institution metadata |
| [NIH RePORTER](https://reporter.nih.gov) | Active grants for US-based supervisors |
| [UKRI Gateway to Research](https://gtr.ukri.org) | Grants for UK-based supervisors |
| [ARC](https://data.gov.au) | Grants for Australian supervisors |
| [ROR](https://ror.org) | Institution homepages (via OpenAlex institution IDs) |
| [Groq / Llama 3.1 8B](https://groq.com) | Keyword extraction, why_match blurbs, program page parsing |
| [SerpAPI](https://serpapi.com) | Web search for PhD program pages |

All sources are free-tier or open-access. No scraping of journal paywalls or restricted databases.

---

## Output Schema

See `schema.md` for the full documented schema. The top-level shape is:

```json
{
  "student_id": "s001",
  "generated_at": "2025-01-15T10:30:00Z",
  "target_countries": ["us", "uk"],
  "total_recommendations": 87,
  "phd_programs_found": 61,
  "tier_summary": { "reach": 12, "target": 43, "safety": 32 },
  "recommendations": [ ... ]
}
```

---

## Known Limitations

**Grant coverage is uneven.** NIH is comprehensive for active US grants. UKRI covers funded UK projects well. ARC coverage for Australia is patchier — the data.gov.au endpoint is less reliable and returns fewer results. Supervisors in Canada, Germany, Singapore, etc. won't have grant data at all right now.

**Program pages are hard.** University PhD program pages vary wildly in structure. The LLM extraction works well for most English-language pages, but some universities (especially European ones with non-standard layouts) return stub-quality results with low confidence.

**Email availability.** Many academics don't publish their email on a crawlable homepage. The scraper finds ~40–60% of emails in practice. The rest need manual lookup or can be inferred from institutional patterns.

---

## Project Structure

```
shortlist.py          # Main orchestrator and CLI entry point
sources.py            # All external data fetching (OA, grants, email, LLM)
sources_programs.py   # PhD program discovery logic
models.py             # Data classes, scoring, country normalisation, serialisation
config.py             # All constants, thresholds, URLs in one place
requirements.txt      # Python dependencies
schema.md             # Output JSON schema documentation
DECISIONS.md          # Design decisions and trade-off writeup
sample_output/        # Generated shortlists go here
```
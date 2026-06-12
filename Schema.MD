# Output JSON Schema

Every run produces a single JSON file at `sample_output/<student_id>.json`. Here's what's in it.

---

## Top-level object

```json
{
  "student_id":             "s001",
  "generated_at":           "2025-01-15T10:30:00Z",
  "target_countries":       ["us", "uk", "au"],
  "total_recommendations":  87,
  "phd_programs_found":     61,
  "tier_summary": {
    "reach":  12,
    "target": 43,
    "safety": 32
  },
  "recommendations": [ ... ]
}
```

| Field | Type | Description |
|---|---|---|
| `student_id` | string | Copied from the input profile |
| `generated_at` | string (ISO 8601) | UTC timestamp when the shortlist was generated |
| `target_countries` | string[] | Normalised country codes from input (e.g. "us", "uk", "au") |
| `total_recommendations` | integer | Total number of supervisors in the list |
| `phd_programs_found` | integer | How many supervisors have a linked PhD program |
| `tier_summary` | object | Count of reach / target / safety entries |
| `recommendations` | Supervisor[] | The ranked shortlist — see below |

---

## Supervisor object

Each entry in `recommendations` looks like this:

```json
{
  "name":           "Dr. Jane Smith",
  "institution":    "University of Edinburgh",
  "country":        "uk",
  "email":          "j.smith@ed.ac.uk",
  "homepage":       "https://homepages.inf.ed.ac.uk/jsmith/",
  "research_focus": "federated learning, privacy, healthcare AI, NLP, transformers",
  "h_index":        28,
  "works_count":    112,
  "cited_by_count": 4801,
  "openalex_id":    "https://openalex.org/A1234567890",
  "evidence": {
    "papers": [ ... ],
    "grants": [ ... ]
  },
  "why_match":   "Dr. Smith's work on privacy-preserving federated optimisation...",
  "tier":        "target",
  "score":       0.4812,
  "phd_program": { ... }
}
```

| Field | Type | Description |
|---|---|---|
| `name` | string | Supervisor's display name from OpenAlex |
| `institution` | string | Most recent known institution |
| `country` | string | ISO 2-letter country code |
| `email` | string or null | Email found by scraping their homepage, if available |
| `homepage` | string or null | OpenAlex homepage URL, if available |
| `research_focus` | string | Top 5 OpenAlex topics, comma-separated |
| `h_index` | integer | H-index from OpenAlex |
| `works_count` | integer | Total publications indexed in OpenAlex |
| `cited_by_count` | integer | Total citations in OpenAlex |
| `openalex_id` | string | Full OpenAlex author URL (use this as a stable identifier) |
| `evidence` | object | Papers and grants — see below |
| `why_match` | string | 2–3 sentence personalised match explanation |
| `tier` | string | One of: `"reach"`, `"target"`, `"safety"` |
| `score` | float | Raw relevance score, 0.0–1.2 (can exceed 1.0 with secondary bonuses) |
| `phd_program` | object or null | Linked PhD program — see below |

### Tier thresholds

| Tier | Score range | What it means |
|---|---|---|
| `reach` | > 0.55 | Strong keyword + evidence overlap. High-quality match, may be competitive. |
| `target` | 0.40 – 0.55 | Solid match. Student has relevant background and PI works in the area. |
| `safety` | ≤ 0.40 | Adjacent match. Worth emailing but expect lower response rates. |

---

## Evidence object

```json
"evidence": {
  "papers": [
    {
      "title": "Federated Learning with Differential Privacy for Medical Imaging",
      "year":  2023,
      "url":   "https://openalex.org/W1234567890"
    }
  ],
  "grants": [
    {
      "title":  "Privacy-Preserving Machine Learning for Clinical Decision Support",
      "funder": "NIH",
      "year":   2022,
      "url":    "https://reporter.nih.gov/project-details/10456789",
      "amount": "$412,000"
    }
  ]
}
```

Up to 5 papers and 3 grants are included. All papers have an OpenAlex URL. All grants link to the original funder database where available.

| Field | Type | Notes |
|---|---|---|
| `title` | string | Paper or grant title |
| `year` | integer | Publication or fiscal year |
| `url` | string or null | Direct link to paper or grant record |
| `funder` | string | Grant funder: `"NIH"`, `"UKRI"`, or `"ARC"` |
| `amount` | string or null | Award amount (NIH only; formatted as `"$412,000"`) |

---

## PhD Program object

```json
"phd_program": {
  "university":    "University of Edinburgh",
  "department":    "School of Informatics",
  "program_name":  "PhD in Informatics",
  "apply_url":     "https://www.ed.ac.uk/informatics/postgraduate/apply",
  "deadline":      "December 1, 2025",
  "funding_notes": "Fully funded studentships available for UK/international applicants",
  "country":       "uk",
  "confidence":    "high"
}
```

| Field | Type | Description |
|---|---|---|
| `university` | string | Institution name |
| `department` | string or null | Department or school name as written on the page |
| `program_name` | string | PhD program title |
| `apply_url` | string | Direct URL to the application page (or the grad school homepage if no direct link found) |
| `deadline` | string or null | Application deadline text, exactly as found on the page |
| `funding_notes` | string or null | Any scholarship, stipend, or eligibility info found on the page |
| `country` | string or null | ISO country code |
| `confidence` | string | `"high"` if a direct application URL was found; `"low"` if we fell back to a homepage or stub |

A `confidence: "low"` entry means the program page was reachable but the LLM couldn't extract structured data from it, or we fell back to the institution homepage. The `apply_url` is still a valid starting point for the student to investigate manually.

---

## Notes on null fields

- `email` is null for roughly 40–60% of supervisors — many academics don't publish it on a crawlable page.
- `phd_program` is null when we couldn't find a matching graduate program page after searching and probing common grad school URL paths.
- `deadline` and `funding_notes` inside phd_program are null when that information wasn't present on the page we found.

None of these nulls indicate a data error — they reflect what was publicly findable at the time of the run.
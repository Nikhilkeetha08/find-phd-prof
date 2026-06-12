"""Single source of truth for every constant, threshold, URL, and tuneable."""

from __future__ import annotations

# API endpoints & identifiers
OA_BASE  = "https://api.openalex.org"
OA_EMAIL = "phd-shortlist@ambitio.app"

GROQ_BASE  = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.1-8b-instant"

ROR_BASE   = "https://api.ror.org/organizations"
ORCID_BASE = "https://pub.orcid.org/v3.0"

SERPAPI_URL         = "https://serpapi.com/search"
SERPAPI_ENGINE      = "google"
SERPAPI_NUM_RESULTS = 3

NIH_SEARCH_URL  = "https://api.reporter.nih.gov/v2/projects/search"
UKRI_SEARCH_URL = "https://gtr.ukri.org/gtr/api/projects"
ARC_SEARCH_URL  = "https://data.gov.au/api/3/action/datastore_search"
ARC_RESOURCE_ID = "a5a27e0b-b5c9-4b78-afc3-c0c4f7e4b0b5"

# HTTP client settings
USER_AGENT           = "phd-shortlist-builder/1.0 (mailto:phd-shortlist@ambitio.app)"
HTTP_TIMEOUT_DEFAULT = 20
HTTP_TIMEOUT_GROQ    = 30
HTTP_TIMEOUT_FETCH   = 20
HTTP_TIMEOUT_SHORT   = 15
HTTP_TIMEOUT_SEARCH  = 10

TCP_CONNECTOR_LIMIT_MAIN      = 20
TCP_CONNECTOR_LIMIT_WHY_MATCH = 10

# Concurrency — semaphore caps
SEM_OPENALEX   = 5
SEM_NIH        = 3
SEM_UKRI       = 3
SEM_DEFAULT    = 6

SEM_CANDIDATES = 8
SEM_ENRICH     = 6
SEM_WHY_MATCH  = 5
SEM_PROGRAMS   = 5
SEM_S2_RESOLVE = 4   # kept for compatibility but not used (S2 removed)

# Groq / LLM call settings
GROQ_RATE_LIMIT_RETRIES   = 4
GROQ_RATE_LIMIT_BASE_WAIT = 10
GROQ_TIMEOUT_SLEEP        = 5
GROQ_ERROR_SLEEP          = 3

GROQ_MAX_TOKENS_KEYWORDS_PRIMARY   = 80
GROQ_MAX_TOKENS_KEYWORDS_SECONDARY = 60
GROQ_MAX_TOKENS_WHY_MATCH          = 200
GROQ_MAX_TOKENS_SUPERVISION_CHECK  = 5
GROQ_MAX_TOKENS_ELIGIBILITY        = 5
GROQ_MAX_TOKENS_DOMAIN_EXCLUSIONS  = 150
GROQ_MAX_TOKENS_FIELD_DETECT       = 30
GROQ_MAX_TOKENS_PROGRAM_EXTRACT    = 300

GROQ_TEMP_DETERMINISTIC = 0.0
GROQ_TEMP_KEYWORDS      = 0.1
GROQ_TEMP_WHY_MATCH     = 0.4

# Pipeline thresholds
MAX_RECOMMENDATIONS = 3
MIN_RECOMMENDATIONS = 1

MIN_H_INDEX           = 8
MIN_WORKS_COUNT       = 10
FIRST_AUTHOR_H_THRESH = 20

OA_PAPERS_PER_AREA = 20
OA_AUTHOR_PAPERS_N = 5

NIH_GRANT_LIMIT  = 10
UKRI_GRANT_LIMIT = 10
UKRI_GRANT_KEEP  = 5
ARC_GRANT_LIMIT  = 10
ARC_GRANT_KEEP   = 5

SCORE_TIER_REACH  = 0.55
SCORE_TIER_TARGET = 0.40

SCORE_WEIGHT_PRIMARY   = 0.40
SCORE_WEIGHT_RECENCY   = 0.20
SCORE_WEIGHT_GRANT     = 0.20
SCORE_WEIGHT_H_INDEX   = 0.20
SCORE_H_INDEX_CAP      = 60.0
SCORE_RECENCY_YEAR     = 2023
SCORE_SECONDARY_BONUS_PER_HIT = 0.10
SCORE_SECONDARY_BONUS_CAP     = 0.20

PROGRAM_PAGE_TEXT_LIMIT = 4000

# Candidate / institution filtering
BAD_TITLES: set[str] = {
    "phd student", "postdoc", "postdoctoral", "fellow", "resident",
    "graduate student", "doctoral student", "grad student",
}

HARD_EXCLUDE_INST_TYPES: set[str] = {"company", "healthcare"}
SOFT_EXCLUDE_INST_TYPES: set[str] = {"government", "nonprofit", "facility"}

# Grant country routing
GRANT_COUNTRY_MAP: dict[str, str] = {
    "us":  "nih",
    "usa": "nih",
    "uk":  "ukri",
    "gb":  "ukri",
    "au":  "arc",
    "aus": "arc",
}

# Email scraping
BAD_EMAIL_DOMAINS: set[str] = {
    "example.com",
    "sentry.io",
    "w3.org",
    "schema.org",
}

# PhD program finder
PROGRAM_CONFIDENCE_HIGH = "high"
PROGRAM_CONFIDENCE_LOW  = "low"

PROGRAM_CRAWLER_TOP_N   = 6
PROGRAM_CRAWLER_SUB_N   = 4
PROGRAM_CANDIDATE_LIMIT = 3
PROGRAM_MIN_SCORE_FOR_HOP = 2

GRAD_PAGE_PATTERN: str = (
    r"(graduate[.\-_/]?(school|program|studies|admissions|research)"
    r"|phd[.\-_/]?(program|admissions|application)"
    r"|doctoral[.\-_/]?(program|studies|admissions)"
    r"|postgraduate|research[.\-_/]?(degrees|programs))"
)
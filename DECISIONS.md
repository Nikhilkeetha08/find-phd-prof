# DECISIONS.md — Design Choices and Trade-offs

---

## 1. Anchoring identity to OpenAlex IDs, not names

Common names like "Wei Wang" or "Rong Zheng" can map to dozens of unrelated researchers. Rather than trusting names, every candidate is tracked by their OpenAlex author ID from the moment they appear in a paper's authorship list. Two entries with the same ID get merged; two people with the same name at different institutions stay separate. On top of that, every candidate goes through a relevance score — if none of their paper titles or research concepts overlap with the student's keywords, they're dropped entirely before reaching the output.

---

## 2. Using authorship position and h-index to filter out junior researchers

PhD students and postdocs publish papers just like faculty and show up in the same databases. To separate PIs from trainees, we look at where someone tends to appear in author lists — last authorship in STEM is a reliable PI signal. Anyone who only appears as a first author gets a stricter h-index floor of 20. For grant evidence, NIH F31/F32 awards are discarded when they're the only grant on record, since those are trainee fellowships rather than PI funding.

---

## 3. Two-tier keywords plus an LLM-generated exclusion list

A flat keyword search creates wrong-domain noise — a grant about "biodegradable plastic cartridges" can surface in a biomaterials search even though it's military R&D. We handle this with two layers. Keywords are split into technique-level terms (e.g. "federated learning", "diffusion model") and application-domain terms (e.g. "healthcare", "robotics") — a supervisor needs to hit at least one technique term to score above zero, which blocks most surface-level discipline mismatches. On top of that, Groq generates a domain exclusion list at the start of each run based on the student's interests, identifying terms that would flag a completely unrelated field. Any supervisor whose text contains those terms is filtered out.

---

## 4. Country filtering at the institution level, eligibility surfaced in free text

Hard-filtering by target country is applied at the supervisor's institution level using OpenAlex metadata, so a student targeting US/UK/AU will never see a supervisor outside those countries. For PhD ad eligibility text — citizenship restrictions, home fees clauses — the LLM program extraction picks up whatever it finds and stores it in the `funding_notes` field. The student sees it directly and can make their own call before emailing.

---

## 5. Zero-score gate to keep the list clean

Every supervisor must have at least one paper linked to an OpenAlex URL or a grant linked to a funder database. The scoring function returns 0 for anyone without primary keyword hits across their papers and concepts, and zero-scoring entries are dropped before output. This keeps the shortlist tight — a shorter, well-evidenced list is more useful than a padded one.



---

## Further Scope

**Closing the feedback loop.** Once students start emailing the shortlisted supervisors, the outcomes — whether they got a reply, an interview, an admission, or no response — are a valuable signal that the system currently does not use. Feeding that data back would let the system improve over time: a supervisor who repeatedly gets flagged as the wrong person can be deprioritised for similar profiles, one who consistently leads to positive replies can be ranked higher, and one who never responds across many students can be softly penalised. This would shift the shortlist from a static output into something that gets more accurate with every cohort that uses it.

---

## How Scoring Works

Every supervisor gets a score between 0.0 and 1.2 calculated as:

```
score = (0.40 × primary_score)
      + (0.20 × recency)
      + (0.20 × grant)
      + (0.20 × h_norm)
      + secondary_bonus
```

**primary_score** — how many of the student's technique keywords appear in the supervisor's paper titles and research concepts, divided by the total number of primary keywords, capped at 1.0. If this is zero, the supervisor is dropped immediately — no further calculation.

```
primary_score = min(hits_in_primary / total_primary_keywords, 1.0)
```

**recency** — fraction of the supervisor's papers published in 2023 or later.

```
recency = papers_since_2023 / total_papers
```

**grant** — binary. 1.0 if they have any active grant, 0.0 if not.

**h_norm** — h-index normalised against a cap of 60.

```
h_norm = min(h_index / 60.0, 1.0)
```

**secondary_bonus** — each application-domain keyword that appears in their text adds 0.10, capped at 0.20.

```
secondary_bonus = min(secondary_hits × 0.10, 0.20)
```

Tiers are then assigned: score > 0.55 → reach, > 0.40 → target, ≤ 0.40 → safety.

---

## How PI Verification Works

Before a candidate is accepted as a supervisor, they pass through a sequential filter — failing any step drops them entirely.

**Step 1 — Authorship position check.**
If the candidate appears only as a first author (never last or middle), they must have h-index ≥ 20. Last-authorship in STEM is a reliable PI signal; first-only with a low h-index almost always means a student or postdoc.

**Step 2 — Minimum profile thresholds.**
h-index ≥ 8 and works_count ≥ 10. Anyone below both is too early-career to reliably supervise.

**Step 3 — Country match.**
Institution country from OpenAlex must match one of the student's target countries.

**Step 4 — Institution type.**
Companies and healthcare institutions are hard-excluded — they cannot formally award PhDs. Government, nonprofit, and facility types go through an LLM check: Groq is asked whether the institution supervises PhD students, and if the answer is no, the candidate is dropped.

Only candidates who clear all four steps become a `Supervisor` object and enter scoring.
# Ideation Pipeline — Model Selection per Phase

Principle: **use code where code works, the cheapest model that passes eval for volume work, and the strongest model only for low-volume judgment.** Re-check pricing and model lineup before building — it changes often.

## Quick map

| Phase | Task | Use | Why |
|---|---|---|---|
| 0 | Repo scaffold, schema, config | **Claude Sonnet 5.5** (coding agent) | Strong at code, cheaper than Opus for routine scaffolding |
| 1 | Scraping (Reddit, Apify, RSS) | **No LLM** — Python scripts | Deterministic, free, reliable |
| 1 | Dedup / hashing | **No LLM** | Hashing is exact and free |
| 2 | Hand-labeling eval set | **You** (optionally Opus 5.5 as second opinion) | Ground truth must be human |
| 2 | Filter + extraction (thousands of items) | **Claude Haiku 5.5** via Batch API | High volume, simple classification, cheapest; batch ≈ half price |
| 2 | Prompt tuning / error analysis | **Claude Opus 5.5** | Reads failures and rewrites the prompt; low volume |
| 3 | Embeddings | **Dedicated embedding model** (e.g. Voyage or OpenAI text-embedding) | Chat models don't produce embeddings; cheap |
| 3 | Clustering | **No LLM** — HDBSCAN | Math, not language |
| 3 | Cluster naming / merge-split | **Claude Sonnet 5.5** | Needs synthesis over many items; moderate volume |
| 3 | Per-item severity rating | **Claude Haiku 5.5** (batch) | Rubric-based, high volume |
| 3 | Scoring formula | **No LLM** — SQL/Python | LLMs are bad at counting; must be reproducible |
| 4 | Run 2 | Same as Phases 1–3 | Config change only |
| 5 | Competitor research + spec writing | **Claude Opus 5.5** with web search | Hardest reasoning, few calls (10–20), quality matters most |
| 5 | Spec critique / "devil's advocate" pass | **Claude Sonnet 5.5** (separate call) | Independent check so the spec doesn't grade itself |
| 6 | Dashboard code | **Claude Sonnet 5.5** (coding agent) | Standard Next.js work |
| 6 | Outreach message drafts for validation | **Claude Sonnet 5.5** | Short writing, you edit before sending |

## Sub-agent layout

```
Orchestrator (Python, no LLM)
├── Scraper workers ............ scripts (no LLM)
├── Filter agent ............... Haiku 5.5, batch
├── Severity rater ............. Haiku 5.5, batch
├── Embedder ................... embedding model
├── Cluster labeler ............ Sonnet 5.5
├── Spec writer ................ Opus 5.5 + web search
└── Spec critic ................ Sonnet 5.5
```

Keep the orchestrator as plain code, not an LLM — the pipeline is fixed and sequential, so an agentic planner adds cost and unpredictability without benefit.

## Upgrade / downgrade rules

- **Filter precision stuck below 85% after prompt tuning** → try Sonnet 5.5 on a 200-item sample; switch only if the gain justifies ~several× cost.
- **Cluster labels vague or merging unrelated problems** → move cluster labeling to Opus 5.5 (volume is small).
- **Spec quality already good on Sonnet** → downgrade the spec writer to save cost.
- Always compare models on the **same eval set**, never by vibe.

## Cost shape

~95% of tokens go through Haiku (filter + severity). Opus touches only ~20 specs plus prompt-tuning sessions, so it stays a small share of the bill despite its higher price.

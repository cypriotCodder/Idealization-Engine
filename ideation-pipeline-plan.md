# Ideation Pipeline — Build Plan

Multi-agent pipeline that mines Reddit, G2/Capterra and job boards for B2B pain points and turns the strongest ones into Micro-SaaS specs.

**Strategy:** two runs.
- **Run 1 — Broad discovery:** generic sources; let the data reveal which industries hurt most.
- **Run 2 — Deep dive:** targeted scraping of the top 3–5 industries from Run 1.

Only up-front filter: skip industries that are heavily regulated or impossible to reach as a seller.

---

## Phase 0 — Setup (1–2 days)

**Goal:** repo skeleton, accounts, database.

```
ideation-pipeline/
├── ingest/        # scrapers
├── filter/        # filter agent
├── synth/         # clustering, scoring, specs
├── db/            # schema + migrations
├── config/        # sources.yaml, prompts/
├── eval/          # labeled data, metrics
└── dashboard/     # later
```

- Accounts/keys: Reddit script app, Anthropic API, Apify (free tier), embeddings provider.
- Postgres + pgvector (Docker locally, or Supabase/Neon).
- Secrets in `.env`; targets in `config/sources.yaml`, never hard-coded.

**Done when:** migrations create tables and a test script reads one Reddit post.

---

## Phase 1 — Ingestion (week 1)

**Goal:** 5–10k deduplicated raw items.

### Schema
```sql
sources(id, platform, target, run_label, active)
raw_items(
  id, source_id, platform, external_id UNIQUE,
  parent_id REFERENCES raw_items(id) NULL,  -- set for comments; NULL for posts/reviews
  url, author_hash, title, text, rating,
  score, num_comments, created_at, scraped_at,
  text_hash, processed BOOL DEFAULT false
)
scrape_runs(id, source_id, started_at, finished_at, items_new, errors)
```

### Reddit (build first)
- Run 1 subreddits: r/smallbusiness, r/Entrepreneur, r/SaaS, r/sysadmin, r/excel, r/msp, r/projectmanagement, r/accounting, r/realtors, r/logistics.
- Pull top (year/month), new, and loose keyword searches: "manual", "spreadsheet", "every week", "is there a tool", "workaround", "copy paste".
- Include top 20 comments per post as their own rows, with `parent_id` pointing to the post row.
- Min length 80 chars; skip deleted/removed; respect PRAW rate limits; run incrementally daily.

### G2 / Capterra (Apify)
- Run 1: ~15 broad categories (CRM, HR, accounting, PM, inventory, field service, property mgmt, scheduling, invoicing, helpdesk…).
- 1–3★ reviews only; keep "dislikes" / "problems solved" fields.
- Import Apify dataset JSON into `raw_items`.
- Note: their ToS forbid scraping — keep volume low, private research only.

### Job boards
- Postponed to Run 2.

### Hygiene
- `text_hash` = SHA-256 of normalized text for repost dedup.
- `author_hash` = salted hash; never store raw usernames.

**Done when:** 5k+ rows, no duplicates, `scrape_runs` logged per source.

---

## Phase 2 — Filter Agent (week 2)

**Goal:** raw text → structured problems at ≥85% precision.

### Eval set first
- Hand-label 200 random items (relevant? + one-line summary) → `eval/labeled.jsonl`.

### Prompt (`config/prompts/filter.md`)
- Role: strict product researcher.
- KEEP: operational bottlenecks, missing workflow features, manual data entry, integration gaps, reporting pain.
- DISCARD: UI looks, price, support, bugs, vague venting, self-promotion.
- Output enforced via structured output / tool use.
- 3 keep + 3 discard few-shot examples from the eval set.
- `problem_summary` must be tool-agnostic; use `null` instead of guessing time cost.

### Output schema
```json
{
  "relevant": true,
  "problem_category": "manual_data_entry | missing_workflow | integration_gap | reporting | other",
  "problem_summary": "one sentence, tool-agnostic",
  "persona": "e.g. property manager",
  "industry": "...",
  "current_workaround": "spreadsheets / copy-paste / none",
  "tools_mentioned": ["..."],
  "time_cost_stated": "4 hours/week | null",
  "money_signal": "would pay / switching tools / null",
  "evidence_quote": "short excerpt",
  "confidence": 0.0
}
```

### Table
```sql
problems(
  id, raw_item_id, prompt_version, model, run_label, relevant,
  category, problem_summary, persona, industry,
  workaround, tools_mentioned JSONB,
  time_cost, money_signal, evidence_quote,
  confidence, severity, embedding vector(1024)
)
```

**Embedding dimension:** `vector(1024)` assumes the default provider is Voyage (1024-dim). The dimension is set in `config/models.yaml`. If you switch providers (e.g. OpenAI text-embedding-3, which defaults to 1536 or 3072 but accepts a `dimensions` parameter), either request 1024 dimensions or add a migration and re-embed everything.

### Pipeline
- `filter/run.py`: unprocessed rows → Batch API → poll → write `problems`.
- `eval/score.py`: precision/recall vs labels. Iterate until precision ≥85% (recall ~70% is fine).

**Done when:** eval passes, all items processed (~10–20% relevant expected).

---

## Phase 3 — Clustering & Scoring (week 3)

**Goal:** ranked list of problem clusters.

1. **Embed** `problem_summary + persona` for relevant rows.
2. **Cluster** with HDBSCAN (min cluster size ~5).
3. **Label clusters** with an LLM: name, canonical statement, merge/split flags.
   Tables: `clusters(id, name, statement, persona, industry, prompt_version, model, run_label)`, `cluster_members(cluster_id, problem_id)`.
4. **Severity** per item, 1–5 rubric:
   1 = annoyance · 3 = recurring time cost · 5 = blocks work / costs money / causes errors.
5. **Score in code, not by LLM:**
   - `freq` = unique authors (log-scaled)
   - `severity` = mean
   - `cross_platform` = distinct platforms
   - `money` = share with money signal
   - `recency` = share from last 6 months
   - `score` = weighted sum of all five, each normalized to 0–1 (weights in `config/scoring.yaml`). Starting weights:

     | Component | Weight |
     |---|---|
     | freq | 0.30 |
     | severity | 0.25 |
     | cross_platform | 0.20 |
     | money | 0.15 |
     | recency | 0.10 |
     | **Total** | **1.00** |
6. **Run 1 report:** top industries/personas by total score → decides Run 2.

**Done when:** ranked cluster table + top-5 industry list.

---

## Phase 4 — Run 2 Deep Dive (week 4)

- Add industry-specific subreddits, G2 categories, job-board queries ("Advanced Excel", "VLOOKUP", "data entry" + role).
- Re-run Phases 1–3 with `run_label = 'run2'` — config change only.

---

## Phase 5 — Spec Generation (weeks 4–5)

- Input: top 10–20 clusters with ≥2 platforms and severity ≥3.5.
- Model with **web search** produces per cluster:
  1. Problem statement + 5 linked evidence quotes
  2. Persona & buyer
  3. Existing solutions and their gaps
  4. MVP (3–5 features)
  5. Integrations needed
  6. Pricing hypothesis
  7. Solo-dev build estimate
  8. Risks & kill criteria
- Stored as markdown in `specs(id, cluster_id, body_md, prompt_version, model, run_label, created_at)`.

**Done when:** 10+ specs with competitor research.

---

## Phase 6 — Review Dashboard & Validation (week 5)

- Minimal Next.js page: clusters, scores, evidence links, spec, pursue / maybe / reject + reason.
- Verdicts feed back into weights and prompts.
- For each "pursue": contact 5 original complainers or post in their subreddit before building.

---

## Timeline

| Week | Deliverable |
|---|---|
| 0 | Repo, DB, keys |
| 1 | Reddit + G2 ingestion, 5k+ items |
| 2 | Filter agent passing eval |
| 3 | Clusters, scores, Run 1 industry report |
| 4 | Run 2 deep scrape; spec generation starts |
| 5 | Specs finished, dashboard, first validations |

## Risks

- **Scrape bias** — rotate categories between runs.
- **Loud minorities** — count unique authors; require cross-platform evidence.
- **Already solved** — competitor search in Phase 5 is mandatory.
- **Blocking / ToS** — low volume, cache everything, Reddit first.

## Estimated cost (first full run)

Under ~$100 total: Haiku batch filtering is a few dollars, embeddings negligible, specs under ~$10, Apify free tier to ~$50/month.

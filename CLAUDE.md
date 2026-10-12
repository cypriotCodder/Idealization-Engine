# CLAUDE.md — Ideation Pipeline

Guidance for AI coding agents working in this repo. Read this file fully before making changes. The full build plan is in `docs/ideation-pipeline-plan.md`; model choices are in `docs/ideation-pipeline-models.md`.

## What this project is

A data pipeline that scrapes B2B complaints (Reddit, G2/Capterra, job boards), filters them with an LLM into structured problems, clusters and scores them, and generates Micro-SaaS specs for the strongest clusters.

```
ingest → raw_items → filter (Haiku) → problems → embed + cluster → score → specs (Opus) → dashboard
```

## Golden rules

1. **Follow the phase order.** Don't build Phase N+1 until Phase N meets its "Done when" criteria in the plan. Ask before skipping ahead.
2. **Code over LLM.** Scraping, dedup, clustering, counting and scoring are deterministic code. Never use an LLM to count, rank by formula, or do math.
3. **Config over code.** Sources, keywords, categories, score weights, model names and prompts live in `config/`. Never hard-code them.
4. **Every stage is idempotent and resumable.** Re-running a stage must not duplicate rows or re-spend API money on already-processed items.
5. **Small, reviewable changes.** One concern per change. Don't refactor unrelated code.
6. **Never commit secrets.** Keys only in `.env` (git-ignored). Provide `.env.example` with placeholder values.

## Stack

- Python 3.12+ for ingest, filter, synth (managed with `uv`)
- PostgreSQL 16 + pgvector
- Anthropic SDK (Batch API for bulk calls)
- PRAW (Reddit), Apify client (G2/Capterra)
- HDBSCAN + numpy for clustering
- Next.js + TypeScript for the dashboard (Phase 6 only)

Do not add new dependencies without stating why in the change summary.

## Repo layout

```
ingest/      one module per platform: reddit.py, apify_reviews.py, jobs.py
filter/      run.py (batch submit/poll/write), schema.py
synth/       embed.py, cluster.py, label.py, severity.py, score.py, spec.py
db/          migrations/ (numbered SQL), connection.py
config/      sources.yaml, scoring.yaml, models.yaml, prompts/*.md
eval/        labeled.jsonl, score.py
dashboard/   Next.js app (later)
docs/        plan + model docs
tests/       mirrors the package layout
```

## Database rules

- Schema changes only via new numbered migration files in `db/migrations/`. Never edit an applied migration.
- Use parameterized queries. No string-built SQL.
- Upserts keyed on natural keys (`external_id`, `text_hash`) for idempotency.
- Every derived row records what produced it (`prompt_version`, model name, `run_label`).

## Scraping rules

- Respect rate limits and ToS; Reddit first. Keep G2/Capterra volume low.
- Never store raw usernames — only salted `author_hash`.
- Skip deleted/removed content and texts under 80 chars.
- Log each run to `scrape_runs` (new items, errors).
- Cache raw responses; never re-scrape what's already stored.

## LLM rules

- Model names come from `config/models.yaml`, never inline. Defaults: Haiku for filter/severity, Sonnet for cluster labels and spec critique, Opus for spec writing.
- Prompts live in `config/prompts/` as versioned markdown. Changing a prompt means bumping its version.
- Enforce output with structured output / tool-use schemas; validate with Pydantic. Invalid output is logged and retried once, then marked failed — never silently dropped or guessed.
- Use the Batch API for any job over ~100 items.
- Log token usage and estimated cost per run.
- Before running a stage on the full dataset, run it on a sample of ~50 and report results.

## Evaluation rules

- `eval/labeled.jsonl` is human ground truth. Agents must not edit it.
- Any filter prompt or model change must be scored with `eval/score.py`; report precision and recall in the change summary. Target precision ≥ 0.85.
- Compare models on the same eval set, never by impression.

## Code standards

- Type hints everywhere; `ruff` for lint/format, `mypy` for types.
- Functions small and pure where possible; I/O at the edges.
- Structured logging (no bare `print` in pipeline code).
- Tests for: dedup, hashing, schema validation, scoring formula, clustering input/output shapes. Mock all network and LLM calls in tests.
- Each stage runnable as a CLI: `python -m ingest.reddit --run-label run1 --limit 50`.

## Before finishing any task

- [ ] Tests pass (`pytest`), lint and types clean
- [ ] Re-running the stage is safe (no duplicates, no repeat spend)
- [ ] Config, not code, holds any new targets/weights/models
- [ ] Migration added if schema changed
- [ ] Summary states what changed, how it was verified, and any cost incurred

## Ask the human before

- Spending more than ~$5 on API calls in one run
- Adding a new data source or platform
- Changing score weights or the eval set
- Deleting data or dropping tables
- Anything that touches ToS-sensitive scraping volume

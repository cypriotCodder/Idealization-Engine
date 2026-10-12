-- Phase 1 ingestion tables. pgvector is enabled now so later migrations can use vector(N).
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE sources (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    platform TEXT NOT NULL,
    target TEXT NOT NULL,
    run_label TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT true,
    UNIQUE (platform, target, run_label)
);

CREATE TABLE raw_items (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES sources (id),
    platform TEXT NOT NULL,
    external_id TEXT NOT NULL,
    -- set for comments; NULL for posts/reviews
    parent_id BIGINT REFERENCES raw_items (id),
    url TEXT,
    author_hash TEXT,
    title TEXT,
    text TEXT NOT NULL,
    rating SMALLINT,
    score INTEGER,
    num_comments INTEGER,
    created_at TIMESTAMPTZ NOT NULL,
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    text_hash TEXT NOT NULL,
    processed BOOLEAN NOT NULL DEFAULT false
);

-- Re-scrape guard: ids are only unique within a platform.
ALTER TABLE raw_items ADD CONSTRAINT raw_items_external_key UNIQUE (platform, external_id);
-- Repost guard: the same author posting the same text twice is one row; two authors
-- posting it stay separate, because cluster frequency counts unique authors.
ALTER TABLE raw_items
    ADD CONSTRAINT raw_items_repost_key UNIQUE NULLS NOT DISTINCT (author_hash, text_hash);

CREATE INDEX raw_items_unprocessed_idx ON raw_items (id) WHERE NOT processed;
CREATE INDEX raw_items_parent_idx ON raw_items (parent_id) WHERE parent_id IS NOT NULL;

CREATE TABLE scrape_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES sources (id),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    items_new INTEGER NOT NULL DEFAULT 0,
    errors JSONB NOT NULL DEFAULT '[]'
);

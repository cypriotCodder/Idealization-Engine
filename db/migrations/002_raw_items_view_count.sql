-- Stack Exchange exposes views per question, and the plan uses them as an engagement signal.
-- NULL for platforms that don't report it.
ALTER TABLE raw_items ADD COLUMN view_count INTEGER;

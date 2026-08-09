ALTER TABLE job
    ADD COLUMN IF NOT EXISTS is_test BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_job_visible_created
    ON job (is_test, erstellt_am DESC);

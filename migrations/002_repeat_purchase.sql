ALTER TABLE job
    ADD COLUMN IF NOT EXISTS wiederholt_von_purchase_id BIGINT REFERENCES purchase(id);

CREATE INDEX IF NOT EXISTS idx_job_wiederholt_von
    ON job(wiederholt_von_purchase_id);

-- SPDX-License-Identifier: AGPL-3.0-only
-- Copyright (C) 2026 Flashbibi
ALTER TABLE job
    ADD COLUMN IF NOT EXISTS wiederholt_von_purchase_id BIGINT REFERENCES purchase(id);

CREATE INDEX IF NOT EXISTS idx_job_wiederholt_von
    ON job(wiederholt_von_purchase_id);

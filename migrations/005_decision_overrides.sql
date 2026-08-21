-- SPDX-License-Identifier: AGPL-3.0-only
-- Copyright (C) 2026 Flashbibi
ALTER TABLE decision
    ADD COLUMN IF NOT EXISTS override_status TEXT
    CHECK (override_status IS NULL OR override_status IN ('pin', 'exclude'));

UPDATE decision
SET override_status = CASE
    WHEN status = 'bestaetigt' THEN 'pin'
    WHEN status = 'verworfen' THEN 'exclude'
    ELSE NULL
END
WHERE override_status IS NULL;

CREATE INDEX IF NOT EXISTS idx_decision_override
    ON decision(line_id, override_status)
    WHERE override_status IS NOT NULL;

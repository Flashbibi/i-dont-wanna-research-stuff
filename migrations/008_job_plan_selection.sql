ALTER TABLE job
    ADD COLUMN IF NOT EXISTS selected_assignments JSONB;

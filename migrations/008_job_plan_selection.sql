-- SPDX-License-Identifier: AGPL-3.0-only
-- Copyright (C) 2026 Flashbibi
ALTER TABLE job
    ADD COLUMN IF NOT EXISTS selected_assignments JSONB;

-- SPDX-License-Identifier: AGPL-3.0-only
-- Copyright (C) 2026 Flashbibi
ALTER TABLE offer
    ADD COLUMN IF NOT EXISTS lieferzeit_text TEXT,
    ADD COLUMN IF NOT EXISTS lager_text TEXT;

-- SPDX-License-Identifier: AGPL-3.0-only
-- Copyright (C) 2026 Flashbibi
ALTER TABLE shop
    ADD COLUMN IF NOT EXISTS profil_quelle_url TEXT,
    ADD COLUMN IF NOT EXISTS versand_text TEXT;

ALTER TABLE shop
    ALTER COLUMN lieferzeit_default_tage DROP NOT NULL;

ALTER TABLE shop
    ADD CONSTRAINT shop_profile_requires_source
    CHECK (
        (versand_chf IS NULL AND gratis_ab_chf IS NULL
         AND mindestbestellwert_chf IS NULL AND lieferzeit_default_tage IS NULL)
        OR NULLIF(BTRIM(profil_quelle_url), '') IS NOT NULL
    ) NOT VALID;

ALTER TABLE shop
    ADD CONSTRAINT shop_shipping_requires_text
    CHECK (
        versand_chf IS NULL
        OR NULLIF(BTRIM(versand_text), '') IS NOT NULL
    ) NOT VALID;

-- SPDX-License-Identifier: AGPL-3.0-only
-- Copyright (C) 2026 Flashbibi
ALTER TABLE offer
    ADD COLUMN IF NOT EXISTS beobachtungstag DATE;

UPDATE offer
SET beobachtungstag = (gesehen_am AT TIME ZONE 'Europe/Zurich')::date
WHERE beobachtungstag IS NULL;

ALTER TABLE offer
    ALTER COLUMN beobachtungstag SET DEFAULT CURRENT_DATE,
    ALTER COLUMN beobachtungstag SET NOT NULL;

ALTER TABLE offer
    DROP CONSTRAINT IF EXISTS offer_line_id_produkt_url_key;

ALTER TABLE offer
    ADD CONSTRAINT offer_line_url_beobachtungstag_key
    UNIQUE (line_id, produkt_url, beobachtungstag);

ALTER TABLE offer
    ADD CONSTRAINT offer_lieferzeit_requires_text
    CHECK (lieferzeit_tage IS NULL OR lieferzeit_text IS NOT NULL) NOT VALID;

ALTER TABLE offer
    VALIDATE CONSTRAINT offer_lieferzeit_requires_text;

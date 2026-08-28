ALTER TABLE offer
    ADD COLUMN IF NOT EXISTS provenienz_text TEXT;

ALTER TABLE offer
    ADD CONSTRAINT offer_provenienz_not_blank
    CHECK (provenienz_text IS NULL OR NULLIF(BTRIM(provenienz_text), '') IS NOT NULL)
    NOT VALID;

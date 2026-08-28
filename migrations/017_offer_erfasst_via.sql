-- NULL heisst weiterhin "von Hand bzw. via KI erfasst"; Werte schreibt
-- ausschliesslich die Engine.
ALTER TABLE offer
    ADD COLUMN IF NOT EXISTS erfasst_via TEXT;

ALTER TABLE offer
    ADD CONSTRAINT offer_erfasst_via_not_blank
    CHECK (erfasst_via IS NULL OR NULLIF(BTRIM(erfasst_via), '') IS NOT NULL)
    NOT VALID;

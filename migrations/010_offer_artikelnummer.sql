ALTER TABLE offer
    ADD COLUMN IF NOT EXISTS artikelnummer TEXT;

ALTER TABLE offer
    ADD CONSTRAINT offer_artikelnummer_not_blank
    CHECK (artikelnummer IS NULL OR NULLIF(BTRIM(artikelnummer), '') IS NOT NULL)
    NOT VALID;

CREATE INDEX IF NOT EXISTS idx_offer_artikelnummer ON offer(shop_id, artikelnummer);

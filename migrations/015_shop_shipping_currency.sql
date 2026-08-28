ALTER TABLE shop
    ADD COLUMN IF NOT EXISTS versand_original NUMERIC(12,2),
    ADD COLUMN IF NOT EXISTS gratis_ab_original NUMERIC(12,2),
    ADD COLUMN IF NOT EXISTS mindestbestellwert_original NUMERIC(12,2),
    ADD COLUMN IF NOT EXISTS versand_waehrung TEXT,
    ADD COLUMN IF NOT EXISTS versand_kurs NUMERIC(16,6),
    ADD COLUMN IF NOT EXISTS versand_kurs_am DATE,
    ADD COLUMN IF NOT EXISTS versand_kurs_quelle TEXT;

-- Bestandsprofile behalten ihre belegte CHF-Bedeutung; Fremdwaehrungsprofile
-- aktualisiert spaeter der Service.
UPDATE shop
SET versand_original = COALESCE(versand_original, versand_chf),
    gratis_ab_original = COALESCE(gratis_ab_original, gratis_ab_chf),
    mindestbestellwert_original = COALESCE(mindestbestellwert_original, mindestbestellwert_chf),
    versand_waehrung = COALESCE(versand_waehrung, 'CHF'),
    versand_kurs = COALESCE(versand_kurs, 1)
WHERE versand_waehrung IS NULL
   OR versand_kurs IS NULL
   OR (versand_chf IS NOT NULL AND versand_original IS NULL)
   OR (gratis_ab_chf IS NOT NULL AND gratis_ab_original IS NULL)
   OR (mindestbestellwert_chf IS NOT NULL AND mindestbestellwert_original IS NULL);

ALTER TABLE shop ALTER COLUMN versand_chf DROP NOT NULL;
ALTER TABLE shop ALTER COLUMN versand_waehrung SET DEFAULT 'CHF';

ALTER TABLE shop
    ADD CONSTRAINT shop_shipping_amount_pairs
    CHECK (
        (versand_original IS NULL) = (versand_chf IS NULL)
        AND (gratis_ab_original IS NULL) = (gratis_ab_chf IS NULL)
        AND (mindestbestellwert_original IS NULL) = (mindestbestellwert_chf IS NULL)
    ) NOT VALID;

ALTER TABLE shop
    ADD CONSTRAINT shop_shipping_originals_nonnegative
    CHECK (
        (versand_original IS NULL OR versand_original >= 0)
        AND (gratis_ab_original IS NULL OR gratis_ab_original >= 0)
        AND (mindestbestellwert_original IS NULL OR mindestbestellwert_original >= 0)
        AND (versand_kurs IS NULL OR versand_kurs > 0)
    ) NOT VALID;

ALTER TABLE shop
    ADD CONSTRAINT shop_foreign_shipping_requires_evidence
    CHECK (
        versand_waehrung IS NULL
        OR versand_waehrung = 'CHF'
        OR (
            versand_original IS NULL
            AND gratis_ab_original IS NULL
            AND mindestbestellwert_original IS NULL
        )
        OR (
            versand_kurs IS NOT NULL
            AND versand_kurs_am IS NOT NULL
            AND NULLIF(BTRIM(versand_kurs_quelle), '') IS NOT NULL
        )
    ) NOT VALID;

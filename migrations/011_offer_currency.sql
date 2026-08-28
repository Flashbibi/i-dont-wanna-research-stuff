ALTER TABLE offer
    ADD COLUMN IF NOT EXISTS preis_original NUMERIC(12,2),
    ADD COLUMN IF NOT EXISTS waehrung TEXT,
    ADD COLUMN IF NOT EXISTS kurs NUMERIC(16,6),
    ADD COLUMN IF NOT EXISTS kurs_am DATE,
    ADD COLUMN IF NOT EXISTS kurs_quelle TEXT;

-- Bestandszeilen sind CHF zum Kurs 1, damit die Regel unten lueckenlos gilt.
UPDATE offer
SET preis_original = COALESCE(preis_original, preis_chf),
    waehrung = COALESCE(waehrung, 'CHF'),
    kurs = COALESCE(kurs, 1)
WHERE preis_original IS NULL OR waehrung IS NULL OR kurs IS NULL;

ALTER TABLE offer ALTER COLUMN waehrung SET DEFAULT 'CHF';

-- Kein CHF-Wert ohne belegte Umrechnung.
ALTER TABLE offer
    ADD CONSTRAINT offer_foreign_currency_requires_evidence
    CHECK (
        waehrung IS NULL
        OR waehrung = 'CHF'
        OR (
            preis_original IS NOT NULL
            AND kurs IS NOT NULL
            AND kurs_am IS NOT NULL
            AND NULLIF(BTRIM(kurs_quelle), '') IS NOT NULL
        )
    ) NOT VALID;

ALTER TABLE offer
    ADD CONSTRAINT offer_kurs_positive
    CHECK (kurs IS NULL OR kurs > 0) NOT VALID;

ALTER TABLE offer
    ADD CONSTRAINT offer_preis_original_positive
    CHECK (preis_original IS NULL OR preis_original > 0) NOT VALID;

CREATE TABLE IF NOT EXISTS kurs (
    id BIGSERIAL PRIMARY KEY,
    waehrung TEXT NOT NULL,
    kurs NUMERIC(16,6) NOT NULL CHECK (kurs > 0),
    geholt_am DATE NOT NULL,
    quelle_url TEXT NOT NULL,
    erstellt_am TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (waehrung, geholt_am)
);

CREATE INDEX IF NOT EXISTS idx_kurs_waehrung_tag ON kurs(waehrung, geholt_am DESC);

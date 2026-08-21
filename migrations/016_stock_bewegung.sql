CREATE TABLE IF NOT EXISTS stock_bewegung (
    id BIGSERIAL PRIMARY KEY,
    stock_id BIGINT NOT NULL REFERENCES stock(id),
    line_id BIGINT REFERENCES bom_line(id) ON DELETE SET NULL,
    delta INTEGER NOT NULL CHECK (delta <> 0),
    grund TEXT NOT NULL CHECK (grund IN (
        'zugang_lieferung',
        'abgang_bestand',
        'korrektur',
        'rueckbuchung_job_geloescht',
        'uebernahme_migration'
    )),
    kommentar TEXT,
    erstellt_am TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (grund <> 'korrektur' OR (kommentar IS NOT NULL AND btrim(kommentar) <> ''))
);

CREATE INDEX IF NOT EXISTS idx_stock_bewegung_stock ON stock_bewegung(stock_id, erstellt_am DESC);
CREATE INDEX IF NOT EXISTS idx_stock_bewegung_line ON stock_bewegung(line_id);

INSERT INTO stock_bewegung (stock_id, delta, grund, kommentar)
SELECT s.id, s.menge, 'uebernahme_migration', 'Backfill Migration 016'
FROM stock s
WHERE s.menge > 0
  AND NOT EXISTS (SELECT 1 FROM stock_bewegung b WHERE b.stock_id = s.id);

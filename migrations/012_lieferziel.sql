-- SPDX-License-Identifier: AGPL-3.0-only
-- Copyright (C) 2026 Flashbibi
CREATE TABLE IF NOT EXISTS lieferziel (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    adresse TEXT NOT NULL,
    land TEXT NOT NULL,
    waehrung TEXT NOT NULL,
    -- Linus' eigener Aufwand: einmal Abholfahrt pro Ziel und Plan.
    aufschlag_chf NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (aufschlag_chf >= 0),
    -- Wartezeit bis zur Abholung, oben auf die Lieferzeit der Shops dieses Ziels.
    zuschlag_tage INTEGER NOT NULL DEFAULT 0 CHECK (zuschlag_tage >= 0),
    erstellt_am TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE shop
    ADD COLUMN IF NOT EXISTS lieferziel_id BIGINT REFERENCES lieferziel(id);

-- Die Heimadresse aus dem Bestand ableiten: alle bisherigen Shops sind CH und
-- liefern nach Hause. Keine fest verdrahteten Ziele - dieses eine entsteht aus
-- den vorhandenen Daten, alles Weitere legt Linus ueber die UI an.
INSERT INTO lieferziel(name, adresse, land, waehrung, aufschlag_chf, zuschlag_tage)
SELECT 'Zuhause (CH)', 'Heimadresse', 'CH', 'CHF', 0, 0
WHERE NOT EXISTS (SELECT 1 FROM lieferziel WHERE land = 'CH');

UPDATE shop
SET lieferziel_id = (SELECT id FROM lieferziel WHERE land = 'CH' ORDER BY id LIMIT 1)
WHERE lieferziel_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_shop_lieferziel ON shop(lieferziel_id);

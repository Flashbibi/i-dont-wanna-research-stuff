CREATE TABLE IF NOT EXISTS job (
    id BIGSERIAL PRIMARY KEY,
    erstellt_am TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    aktualisiert_am TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status TEXT NOT NULL DEFAULT 'offen' CHECK (status IN ('offen', 'in_arbeit', 'bereit', 'bestellt', 'abgeschlossen')),
    quelltext TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bom_line (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL REFERENCES job(id),
    position INTEGER NOT NULL,
    originaltext TEXT NOT NULL,
    suchtext TEXT NOT NULL,
    menge INTEGER NOT NULL DEFAULT 1 CHECK (menge > 0),
    status TEXT NOT NULL DEFAULT 'offen' CHECK (status IN ('offen', 'in_arbeit', 'kandidaten', 'bestand', 'nichts_gefunden', 'erledigt')),
    kommentar TEXT,
    UNIQUE (job_id, position)
);

CREATE TABLE IF NOT EXISTS shop (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL,
    domain TEXT NOT NULL UNIQUE,
    land TEXT NOT NULL CHECK (land = 'CH'),
    versand_chf NUMERIC(12,2) NOT NULL CHECK (versand_chf >= 0),
    gratis_ab_chf NUMERIC(12,2) CHECK (gratis_ab_chf IS NULL OR gratis_ab_chf >= 0),
    mindestbestellwert_chf NUMERIC(12,2) CHECK (mindestbestellwert_chf IS NULL OR mindestbestellwert_chf >= 0),
    lieferzeit_default_tage INTEGER NOT NULL CHECK (lieferzeit_default_tage > 0),
    status TEXT NOT NULL DEFAULT 'ungeprueft' CHECK (status IN ('ungeprueft', 'bestaetigt', 'gesperrt')),
    erstellt_am TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS offer (
    id BIGSERIAL PRIMARY KEY,
    line_id BIGINT NOT NULL REFERENCES bom_line(id),
    shop_id BIGINT NOT NULL REFERENCES shop(id),
    produktname TEXT NOT NULL,
    produkt_url TEXT NOT NULL,
    quelle_url TEXT NOT NULL,
    preis_chf NUMERIC(12,2) NOT NULL CHECK (preis_chf > 0),
    lieferzeit_tage INTEGER CHECK (lieferzeit_tage IS NULL OR lieferzeit_tage > 0),
    lager TEXT,
    gesehen_am TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (line_id, produkt_url)
);

CREATE TABLE IF NOT EXISTS decision (
    id BIGSERIAL PRIMARY KEY,
    line_id BIGINT NOT NULL REFERENCES bom_line(id),
    offer_id BIGINT NOT NULL REFERENCES offer(id),
    status TEXT NOT NULL CHECK (status IN ('bestaetigt', 'verworfen')),
    entschieden_am TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (offer_id)
);

CREATE TABLE IF NOT EXISTS purchase (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL REFERENCES job(id),
    variante JSONB NOT NULL,
    total_chf NUMERIC(12,2) NOT NULL CHECK (total_chf > 0),
    bestellt_am TIMESTAMPTZ NOT NULL,
    zugesagt_liefertage JSONB NOT NULL,
    angekommen_am TIMESTAMPTZ,
    erstellt_am TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS purchase_item (
    id BIGSERIAL PRIMARY KEY,
    purchase_id BIGINT NOT NULL REFERENCES purchase(id),
    line_id BIGINT NOT NULL REFERENCES bom_line(id),
    offer_id BIGINT NOT NULL REFERENCES offer(id),
    menge INTEGER NOT NULL CHECK (menge > 0),
    einzelpreis_chf NUMERIC(12,2) NOT NULL CHECK (einzelpreis_chf > 0)
);

CREATE TABLE IF NOT EXISTS stock (
    id BIGSERIAL PRIMARY KEY,
    bezeichnung TEXT NOT NULL,
    menge INTEGER NOT NULL CHECK (menge >= 0),
    einheit TEXT NOT NULL DEFAULT 'Stk',
    purchase_item_id BIGINT REFERENCES purchase_item(id),
    aktualisiert_am TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bom_line_job_status ON bom_line(job_id, status);
CREATE INDEX IF NOT EXISTS idx_offer_line_seen ON offer(line_id, gesehen_am DESC);
CREATE INDEX IF NOT EXISTS idx_purchase_job ON purchase(job_id);
CREATE INDEX IF NOT EXISTS idx_stock_bezeichnung ON stock(bezeichnung);

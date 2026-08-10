ALTER TABLE shop
    ADD COLUMN IF NOT EXISTS plattform TEXT,
    ADD COLUMN IF NOT EXISTS plattform_beleg TEXT,
    ADD COLUMN IF NOT EXISTS plattform_geprueft_am TIMESTAMPTZ;

ALTER TABLE shop
    ADD CONSTRAINT shop_platform_is_known
    CHECK (plattform IS NULL OR plattform IN ('opencart', 'woocommerce', 'shopify'))
    NOT VALID;

ALTER TABLE shop
    ADD CONSTRAINT shop_platform_requires_evidence
    CHECK (
        plattform IS NULL
        OR NULLIF(BTRIM(plattform_beleg), '') IS NOT NULL
    ) NOT VALID;

ALTER TABLE offer
    ADD COLUMN IF NOT EXISTS shop_produkt_id TEXT;

ALTER TABLE offer
    ADD CONSTRAINT offer_shop_produkt_id_not_blank
    CHECK (shop_produkt_id IS NULL OR NULLIF(BTRIM(shop_produkt_id), '') IS NOT NULL)
    NOT VALID;

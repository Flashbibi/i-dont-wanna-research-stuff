-- Mit den Lieferzielen ist das Land eine Eigenschaft des Ziels und nicht mehr des
-- Schemas, deshalb ersetzt eine Formatpruefung die CH-Festlegung aus Migration 001.
ALTER TABLE shop DROP CONSTRAINT IF EXISTS shop_land_check;

ALTER TABLE shop
    ADD CONSTRAINT shop_land_is_a_country_code
    CHECK (land ~ '^[A-Z]{2}$') NOT VALID;

-- Migration 001 hat shop.land auf 'CH' festgenagelt, weil es damals nur
-- Schweizer Shops gab. Mit den Lieferzielen ist das Land eine Eigenschaft des
-- Ziels geworden; welche Laender zulaessig sind, entscheidet jetzt die
-- Konfiguration der Lieferadressen und nicht mehr das Schema.
--
-- Die alte Regel wird durch eine Formatpruefung ersetzt. Das erweitert nur,
-- was erlaubt ist - keine Zeile verliert dadurch ihre Gueltigkeit.
ALTER TABLE shop DROP CONSTRAINT IF EXISTS shop_land_check;

ALTER TABLE shop
    ADD CONSTRAINT shop_land_is_a_country_code
    CHECK (land ~ '^[A-Z]{2}$') NOT VALID;

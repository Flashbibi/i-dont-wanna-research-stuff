-- SPDX-License-Identifier: AGPL-3.0-only
-- Copyright (C) 2026 Flashbibi
-- NULL heisst weiterhin "von Hand bzw. via KI erfasst". Werte schreibt
-- ausschliesslich die Engine, in der Form adapter:<id>.
ALTER TABLE offer
    ADD COLUMN IF NOT EXISTS erfasst_via TEXT;

ALTER TABLE offer
    ADD CONSTRAINT offer_erfasst_via_not_blank
    CHECK (erfasst_via IS NULL OR NULLIF(BTRIM(erfasst_via), '') IS NOT NULL)
    NOT VALID;

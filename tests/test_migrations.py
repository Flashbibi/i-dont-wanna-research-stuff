from pathlib import Path

import pytest

from app.migrations import MigrationError, apply_migrations, discover_migrations


class FakeConnection:
    def __init__(self, applied=()):
        self.applied = set(applied)
        self.statements = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.statements.append((normalized, params))
        if normalized.startswith("SELECT version FROM schema_migrations"):
            return FakeResult([(version,) for version in sorted(self.applied)])
        if normalized.startswith("INSERT INTO schema_migrations"):
            self.applied.add(params[0])
        return FakeResult([])

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


def test_discovers_numbered_sql_migrations_in_order(tmp_path: Path):
    (tmp_path / "002_second.sql").write_text("SELECT 2", encoding="utf-8")
    (tmp_path / "001_first.sql").write_text("SELECT 1", encoding="utf-8")
    (tmp_path / "notes.sql").write_text("ignored", encoding="utf-8")

    migrations = discover_migrations(tmp_path)

    assert [(m.version, m.path.name) for m in migrations] == [
        (1, "001_first.sql"),
        (2, "002_second.sql"),
    ]


def test_duplicate_migration_version_is_rejected(tmp_path: Path):
    (tmp_path / "001_first.sql").write_text("SELECT 1", encoding="utf-8")
    (tmp_path / "001_duplicate.sql").write_text("SELECT 2", encoding="utf-8")

    with pytest.raises(MigrationError, match="doppelte Migrationsnummer 1"):
        discover_migrations(tmp_path)


def test_applies_only_missing_migrations_and_records_each_version(tmp_path: Path):
    (tmp_path / "001_first.sql").write_text("CREATE TABLE first_table(id int);", encoding="utf-8")
    (tmp_path / "002_second.sql").write_text("CREATE TABLE second_table(id int);", encoding="utf-8")
    connection = FakeConnection(applied={1})

    applied = apply_migrations(connection, tmp_path)

    assert applied == [2]
    assert any("CREATE TABLE second_table" in sql for sql, _ in connection.statements)
    assert not any("CREATE TABLE first_table" in sql for sql, _ in connection.statements)
    assert connection.applied == {1, 2}
    assert connection.commits == 2  # Metadatentabelle, danach Migration 002
    assert connection.rollbacks == 0


def test_core_schema_is_additive_and_contains_long_term_fields():
    sql = Path("migrations/001_initial.sql").read_text(encoding="utf-8")
    for table in ("job", "bom_line", "shop", "offer", "decision", "purchase", "purchase_item", "stock"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    for field in (
        "gesehen_am",
        "quelle_url",
        "zugesagt_liefertage",
        "angekommen_am",
        "status TEXT NOT NULL DEFAULT 'ungeprueft'",
    ):
        assert field in sql
    assert "DROP " not in sql.upper()


def test_repeat_purchase_migration_is_additive():
    sql = Path("migrations/002_repeat_purchase.sql").read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS wiederholt_von_purchase_id" in sql
    assert "DROP " not in sql.upper()


def test_offer_source_text_migration_is_additive():
    sql = Path("migrations/003_offer_source_text.sql").read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS lieferzeit_text TEXT" in sql
    assert "ADD COLUMN IF NOT EXISTS lager_text TEXT" in sql
    assert "DROP " not in sql.upper()


def test_offer_daily_history_migration_preserves_observations_and_enforces_source_text():
    sql = Path("migrations/004_offer_daily_history.sql").read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS beobachtungstag DATE" in sql
    assert "UNIQUE (line_id, produkt_url, beobachtungstag)" in sql
    assert "lieferzeit_tage IS NULL OR lieferzeit_text IS NOT NULL" in sql
    assert "DELETE " not in sql.upper()
    assert "DROP TABLE" not in sql.upper()


def test_decision_override_migration_reuses_rows_additively():
    sql = Path("migrations/005_decision_overrides.sql").read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS override_status TEXT" in sql
    assert "'pin', 'exclude'" in sql
    assert "WHEN status = 'bestaetigt' THEN 'pin'" in sql
    assert "WHEN status = 'verworfen' THEN 'exclude'" in sql
    assert "DELETE " not in sql.upper()
    assert "DROP " not in sql.upper()


def test_e2e_job_marker_migration_is_additive():
    sql = Path("migrations/006_e2e_test_jobs.sql").read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS is_test BOOLEAN NOT NULL DEFAULT FALSE" in sql
    assert "DELETE " not in sql.upper()
    assert "DROP " not in sql.upper()


def test_shop_profile_source_migration_is_additive_and_enforces_provenance():
    sql = Path("migrations/007_shop_profile_sources.sql").read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS profil_quelle_url TEXT" in sql
    assert "ADD COLUMN IF NOT EXISTS versand_text TEXT" in sql
    assert "ALTER COLUMN lieferzeit_default_tage DROP NOT NULL" in sql
    assert "shop_profile_requires_source" in sql
    assert "shop_shipping_requires_text" in sql
    assert "DELETE " not in sql.upper()
    assert "DROP TABLE" not in sql.upper()


def test_job_plan_selection_migration_is_additive():
    sql = Path("migrations/008_job_plan_selection.sql").read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS selected_assignments JSONB" in sql
    assert "DELETE " not in sql.upper()
    assert "DROP " not in sql.upper()


def test_offer_artikelnummer_migration_is_additive():
    sql = Path("migrations/010_offer_artikelnummer.sql").read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS artikelnummer TEXT" in sql
    assert "offer_artikelnummer_not_blank" in sql
    assert "DELETE " not in sql.upper()
    assert "DROP TABLE" not in sql.upper()


def test_shop_platform_migration_is_additive_and_enforces_evidence():
    sql = Path("migrations/009_shop_platform_and_product_ids.sql").read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS plattform TEXT" in sql
    assert "ADD COLUMN IF NOT EXISTS plattform_beleg TEXT" in sql
    assert "ADD COLUMN IF NOT EXISTS shop_produkt_id TEXT" in sql
    assert "shop_platform_requires_evidence" in sql
    assert "'opencart', 'woocommerce', 'shopify'" in sql
    assert "DELETE " not in sql.upper()
    assert "DROP TABLE" not in sql.upper()

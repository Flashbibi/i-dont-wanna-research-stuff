from datetime import date, timedelta

from app.database import PostgresRepository, decode_database_value


class Result:
    def __init__(self, one=None, many=None):
        self.one = one
        self.many = many or []

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


class Connection:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        if "FROM bom_line WHERE id" in normalized:
            return Result(one={"id": 1, "job_id": 1, "suchtext": "Servo", "menge": 1})
        if "lower(%s)" in normalized:
            raise RuntimeError("PostgreSQL would infer the placeholder as bytea")
        return Result(many=[])


def test_check_line_explicitly_casts_case_insensitive_query_parameters_to_text():
    repository = PostgresRepository("unused")
    repository._connect = lambda: Connection()

    checked = repository.check_line(1)

    assert checked["stock"] == []
    assert checked["previous_purchases"] == []
    assert checked["cached_offers"] == []


def test_database_value_decoder_normalizes_sql_ascii_text_bytes():
    assert decode_database_value(b"bastelgarage.ch") == "bastelgarage.ch"
    assert decode_database_value("ungeprueft") == "ungeprueft"


class JobListConnection(Connection):
    def __init__(self):
        self.sql = ""

    def execute(self, sql, params=None):
        self.sql = " ".join(sql.split())
        return Result(many=[])


def test_job_list_excludes_marked_e2e_jobs():
    repository = PostgresRepository("unused")
    connection = JobListConnection()
    repository._connect = lambda: connection

    repository.list_jobs()

    assert "WHERE NOT j.is_test" in connection.sql


def test_job_selection_is_saved_as_jsonb():
    class SelectionConnection(Connection):
        def __init__(self):
            self.sql = ""
            self.params = None

        def execute(self, sql, params=None):
            self.sql = " ".join(sql.split())
            self.params = params
            return Result(one={"id": 7, "selected_assignments": {"10": 31}})

    repository = PostgresRepository("unused")
    connection = SelectionConnection()
    repository._connect = lambda: connection

    saved = repository.save_job_selection(7, {"10": 31})

    assert "selected_assignments = %s" in connection.sql
    assert saved["selected_assignments"] == {"10": 31}


def test_optimization_input_selects_the_columns_the_cart_handover_needs():
    """Ohne plattform_geprueft_am kann der Füllknopf nie verschwinden.

    Ein Fake-Repository liefert den Schlüssel immer mit, deshalb prüft dieser
    Test die tatsächliche Spaltenliste statt des Verhaltens.
    """
    statements = []

    class ShopColumnConnection(Connection):
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            statements.append(normalized)
            if "FROM job WHERE id" in normalized:
                return Result(one={"status": "in_arbeit", "selected_assignments": None})
            if "FROM offer o" in normalized:
                return Result(many=[{"id": 1, "line_id": 1, "shop_id": 5}])
            return Result(many=[])

    repository = PostgresRepository("unused")
    repository._connect = lambda: ShopColumnConnection()

    repository.optimization_input(1)

    shop_query = next(sql for sql in statements if "FROM shop s LEFT JOIN lieferziel" in sql)
    for column in (
        "plattform",
        "plattform_beleg",
        "plattform_geprueft_am",
        "lieferziel_name",
        "lieferziel_land",
        "lieferziel_aufschlag_chf",
        "lieferziel_zuschlag_tage",
        "versand_original",
        "gratis_ab_original",
        "mindestbestellwert_original",
        "versand_waehrung",
        "versand_kurs",
        "versand_kurs_am",
        "versand_kurs_quelle",
    ):
        assert column in shop_query
    offer_query = next(sql for sql in statements if "FROM offer o" in sql)
    for column in ("o.shop_produkt_id", "o.artikelnummer", "o.preis_original", "o.waehrung", "o.kurs"):
        assert column in offer_query


class HistoryConnection(Connection):
    def __init__(self):
        self.sql = ""
        self.params = None

    def execute(self, sql, params=None):
        self.sql = " ".join(sql.split())
        self.params = params
        return Result(
            many=[
                {
                    "purchase_id": 8,
                    "produktname": "Servo Pro",
                    "shop_id": 5,
                    "shop_name": "Swiss Shop",
                    "menge": 2,
                    "einzelpreis_chf": "12.00",
                    "bestellt_am": "2026-08-01T10:00:00Z",
                    "zugesagt_liefertage_pro_shop": {"5": 2},
                    "angekommen_am": "2026-08-03T10:00:00Z",
                }
            ]
        )


def test_search_history_matches_product_or_shop_and_resolves_promised_days():
    repository = PostgresRepository("unused")
    connection = HistoryConnection()
    repository._connect = lambda: connection

    rows = repository.search_history("servo")

    assert "o.produktname ILIKE '%%' ||" in connection.sql
    assert "s.name ILIKE '%%' ||" in connection.sql
    assert connection.params == ("servo", "servo")
    assert rows[0]["zugesagt_liefertage"] == 2
    assert "zugesagt_liefertage_pro_shop" not in rows[0]


class StockConnection(Connection):
    def __init__(self):
        self.sql = ""

    def execute(self, sql, params=None):
        self.sql = " ".join(sql.split())
        return Result(
            many=[
                {
                    "id": 4,
                    "bezeichnung": "Servo",
                    "menge": 3,
                    "einheit": "Stk",
                    "aktualisiert_am": "2026-08-03T10:00:00Z",
                }
            ]
        )


def test_get_stock_returns_only_positive_stock_in_stable_order():
    repository = PostgresRepository("unused")
    connection = StockConnection()
    repository._connect = lambda: connection

    rows = repository.get_stock()

    assert "WHERE menge > 0" in connection.sql
    assert "ORDER BY lower(bezeichnung), id" in connection.sql
    assert rows[0]["bezeichnung"] == "Servo"


def test_shop_writes_original_shipping_currency_and_rate_evidence():
    class ShopConnection(Connection):
        def __init__(self):
            self.statements = []

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.statements.append((normalized, params))
            return Result(one={"id": 8, **(params or {})})

    repository = PostgresRepository("unused")
    connection = ShopConnection()
    repository._connect = lambda: connection
    values = {
        "name": "Amazon.de", "url": "https://amazon.de", "domain": "amazon.de",
        "land": "DE", "lieferziel_id": 1,
        "versand_chf": "6.57", "gratis_ab_chf": "46.06",
        "mindestbestellwert_chf": None, "lieferzeit_default_tage": 5,
        "profil_quelle_url": "https://amazon.de/hilfe", "versand_text": "6,99 EUR",
        "versand_original": "6.99", "gratis_ab_original": "49.00",
        "mindestbestellwert_original": None, "versand_waehrung": "EUR",
        "versand_kurs": "0.94", "versand_kurs_am": date(2026, 8, 11),
        "versand_kurs_quelle": "https://api.frankfurter.app/latest",
    }

    repository.create_shop(**values)
    repository.update_shop_profile(8, **{key: value for key, value in values.items() if key not in {
        "name", "url", "domain", "land", "lieferziel_id"
    }})

    insert_sql, _ = connection.statements[0]
    update_sql, _ = connection.statements[1]
    for column in (
        "versand_original", "gratis_ab_original", "mindestbestellwert_original",
        "versand_waehrung", "versand_kurs", "versand_kurs_am", "versand_kurs_quelle",
    ):
        assert column in insert_sql
        assert column in update_sql


class OfferConnection:
    def __init__(self):
        self.statements = []
        self.current_day = date(2026, 8, 9)
        self.rows = {}
        self.next_id = 12

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def transaction(self):
        return self

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.statements.append((normalized, params))
        if normalized.startswith("INSERT INTO offer"):
            assert "ON CONFLICT (line_id, produkt_url, beobachtungstag)" in normalized
            values = dict(params or {})
            key = (values["line_id"], values["produkt_url"], self.current_day)
            existing = self.rows.get(key)
            row_id = existing["id"] if existing else self.next_id
            if not existing:
                self.next_id += 1
            row = {"id": row_id, "beobachtungstag": self.current_day, **values}
            self.rows[key] = row
            return Result(one=row)
        return Result()


def offer_values(**changes):
    values = {
        "line_id": 1,
        "shop_id": 5,
        "produktname": "Servo",
        "produkt_url": "https://shop.ch/servo",
        "quelle_url": "https://shop.ch/servo",
        "preis_chf": "63.62",
        "lieferzeit_tage": 4,
        "lieferzeit_text": "3-4 Tage, bei Lieferant an Lager",
        "lager_text": "Filiale rot; CH-Lieferant an Lager",
        "lager": "Filiale rot; CH-Lieferant an Lager",
        "artikelnummer": "SKU-99",
        "provenienz_text": "Verkauf und Versand durch Amazon",
        "preis_original": "63.62",
        "waehrung": "CHF",
        "kurs": "1",
        "kurs_am": None,
        "kurs_quelle": None,
    }
    return {**values, **changes}


def test_create_offer_updates_same_day_but_inserts_new_daily_observation():
    repository = PostgresRepository("unused")
    connection = OfferConnection()
    repository._connect = lambda: connection

    first = repository.create_offer(**offer_values())
    corrected = repository.create_offer(**offer_values(preis_chf="60.00"))
    connection.current_day += timedelta(days=1)
    next_day = repository.create_offer(**offer_values(preis_chf="59.00"))

    insert_sql = connection.statements[0][0]
    assert "lieferzeit_text, lager_text" in insert_sql
    assert "provenienz_text" in insert_sql
    assert "provenienz_text = EXCLUDED.provenienz_text" in insert_sql
    # Die Preishistorie fuehrt den Originalbetrag mit.
    assert "preis_original" in insert_sql
    assert first["id"] == corrected["id"]
    assert next_day["id"] != corrected["id"]
    assert len(connection.rows) == 2
    assert connection.rows[(1, "https://shop.ch/servo", date(2026, 8, 9))]["preis_chf"] == "60.00"
    assert connection.rows[(1, "https://shop.ch/servo", date(2026, 8, 10))]["preis_chf"] == "59.00"

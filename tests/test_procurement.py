from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.procurement import ProcurementService, ValidationError, parse_delivery_upper_days


class FakeProcurementRepository:
    def __init__(self):
        self.lieferziele = {
            1: {"id": 1, "name": "Zuhause (CH)", "adresse": "Heimadresse", "land": "CH",
                "waehrung": "CHF", "aufschlag_chf": Decimal("0"), "zuschlag_tage": 0},
            2: {"id": 2, "name": "Postfach (DE)", "adresse": "Grenzstrasse 1", "land": "DE",
                "waehrung": "EUR", "aufschlag_chf": Decimal("25.00"), "zuschlag_tage": 3},
        }
        self.shops = {
            1: {
                "id": 1,
                "name": "Servo Shop",
                "url": "https://shop.example.ch",
                "domain": "shop.example.ch",
                "status": "bestaetigt",
                "lieferziel_id": 1,
            },
            2: {
                "id": 2,
                "name": "Reichelt",
                "url": "https://www.reichelt.de",
                "domain": "reichelt.de",
                "status": "bestaetigt",
                "lieferziel_id": 2,
            },
        }
        self.lines = {10: {"id": 10, "job_id": 5, "suchtext": "Servo", "menge": 2}}
        self.offers = []
        self.marked = []
        self.purchases = []
        self.selected_assignments = None
        self.created_jobs = []
        self.kurse = {}
        self.saved_kurse = []

    def get_job(self, job_id):
        return {
            "id": job_id,
            "status": "in_arbeit",
            "lines": [
                {
                    "id": 10,
                    "position": 1,
                    "originaltext": "2x Servo",
                    "suchtext": "Servo",
                    "menge": 2,
                    "status": "kandidaten",
                    "kommentar": None,
                }
            ],
        }

    def search_history(self, text):
        return [
            {
                "produktname": "Servo Pro",
                "shop_name": "Swiss Shop",
                "bestellt_am": "2026-08-01T10:00:00Z",
                "einzelpreis_chf": "12.00",
                "zugesagt_liefertage": 2,
                "angekommen_am": "2026-08-03T10:00:00Z",
            }
        ]

    def get_stock(self):
        return [{"id": 4, "bezeichnung": "Servo", "menge": 3, "einheit": "Stk"}]

    def create_job(self, source_text, lines):
        self.created_jobs.append((source_text, lines))
        return 91

    def next_job(self):
        return {"id": 5, "status": "offen", "lines": [self.lines[10]]}

    def check_line(self, line_id):
        if line_id not in self.lines:
            return None
        return {"line": self.lines[line_id], "stock": [], "previous_purchases": [], "cached_offers": []}

    def list_lieferziele(self):
        return [dict(z) for z in self.lieferziele.values()]

    def get_lieferziel(self, lieferziel_id):
        return self.lieferziele.get(lieferziel_id)

    def lieferziele_fuer_land(self, land):
        return [dict(z) for z in self.lieferziele.values() if z["land"] == land]

    def create_lieferziel(self, **values):
        neu = max(self.lieferziele) + 1
        row = {"id": neu, **values}
        self.lieferziele[neu] = row
        return row

    def update_lieferziel(self, lieferziel_id, **values):
        self.lieferziele[lieferziel_id].update(values)
        return self.lieferziele[lieferziel_id]

    def get_kurs(self, waehrung):
        return self.kurse.get(waehrung)

    def save_kurs(self, waehrung, kurs, geholt_am, quelle_url):
        row = {"waehrung": waehrung, "kurs": kurs, "geholt_am": geholt_am, "quelle_url": quelle_url}
        self.kurse[waehrung] = row
        self.saved_kurse.append(row)
        return row

    def create_shop(self, **values):
        shop_id = max(self.shops) + 1
        result = {"id": shop_id, "status": "ungeprueft", **values}
        self.shops[shop_id] = result
        return result

    def get_shop(self, shop_id):
        return self.shops.get(shop_id)

    def update_shop_profile(self, shop_id, **values):
        if shop_id not in self.shops:
            raise ValueError("Shop unbekannt")
        self.shops[shop_id].update(values)
        return self.shops[shop_id]

    def get_line(self, line_id):
        return self.lines.get(line_id)

    def create_offer(self, **values):
        result = {"id": len(self.offers) + 20, "gesehen_am": "now", **values}
        self.offers.append(result)
        return result

    def mark_line(self, line_id, status, kommentar):
        self.marked.append((line_id, status, kommentar))
        return {"id": line_id, "status": status, "kommentar": kommentar}

    def optimization_input(self, job_id):
        return {
            "offers": [
                {
                    "id": 31,
                    "line_id": 10,
                    "shop_id": 1,
                    "preis_chf": "10",
                    "menge": 2,
                    "lieferzeit_tage": 2,
                    "lieferzeit_text": "2 Tage",
                    "lager_text": "5 Stück ab Lager",
                    "provenienz_text": "Verkauf und Versand durch Amazon",
                    "quelle_url": "https://shop.example.ch/mg996r",
                    "produktname": "MG996R Servo",
                    "produkt_url": "https://shop.example.ch/mg996r",
                    "suchtext": "Servo",
                    "position": 1,
                    "override_status": None,
                }
            ],
            "shops": [
                {"id": 1, "name": "Servo Shop", "url": "https://shop.example.ch", "versand_chf": "8", "gratis_ab_chf": None, "mindestbestellwert_chf": None, "lieferzeit_default_tage": 3}
            ],
            "required_line_ids": [10],
            "lines": [{"id": 10, "position": 1, "suchtext": "Servo"}],
            "selected_assignments": self.selected_assignments,
        }

    def save_job_selection(self, job_id, assignments):
        self.selected_assignments = assignments
        return {"job_id": job_id, "selected_assignments": assignments}

    def create_purchase(self, job_id, variant, ordered_at, promised_days):
        result = {"id": 90, "job_id": job_id, "variante": variant}
        self.purchases.append((job_id, variant, ordered_at, promised_days))
        return result


def service():
    return ProcurementService(FakeProcurementRepository())


def test_create_job_uses_shared_bom_parser_and_returns_confirmation_lines():
    repository = FakeProcurementRepository()
    procurement = ProcurementService(repository)

    result = procurement.create_job("2x MG996R Servo\nKabel")

    assert result == {
        "job_id": 91,
        "lines": [
            {"position": 1, "text": "MG996R Servo", "menge": 2},
            {"position": 2, "text": "Kabel", "menge": 1},
        ],
    }
    source_text, parsed = repository.created_jobs[0]
    assert source_text == "2x MG996R Servo\nKabel"
    assert [(line.suchtext, line.menge) for line in parsed] == [
        ("MG996R Servo", 2),
        ("Kabel", 1),
    ]


def test_create_job_rejects_empty_source_without_repository_write():
    repository = FakeProcurementRepository()

    with pytest.raises(ValidationError, match="mindestens eine Position"):
        ProcurementService(repository).create_job(" \n ")

    assert repository.created_jobs == []


def test_create_job_rejects_absurd_line_length():
    with pytest.raises(ValidationError, match="höchstens 500 Zeichen"):
        service().create_job("x" * 501)


def test_create_job_from_lines_rejects_empty_entries_and_excessive_line_count():
    procurement = service()

    with pytest.raises(ValidationError, match="Leere Zeilen"):
        procurement.create_job_from_lines(["Servo", "  "])
    with pytest.raises(ValidationError, match="höchstens 200 Positionen"):
        procurement.create_job_from_lines(["Servo"] * 201)


def test_get_job_adds_candidate_counts_and_scenario_availability_from_matrix():
    result = service().get_job(5)

    assert result["id"] == 5
    assert result["status"] == "in_arbeit"
    assert result["lines"][0]["candidate_count"] == 1
    assert result["scenarios_available"] is True


def test_get_job_rejects_unknown_job():
    repository = FakeProcurementRepository()
    repository.get_job = lambda _: None

    with pytest.raises(ValidationError, match="Job 999 ist unbekannt"):
        ProcurementService(repository).get_job(999)


def test_search_history_and_get_stock_are_read_only_repository_views():
    procurement = service()

    history = procurement.search_history("Servo")
    stock = procurement.get_stock()

    assert history[0]["shop_name"] == "Swiss Shop"
    assert history[0]["zugesagt_liefertage"] == 2
    assert stock == [{"id": 4, "bezeichnung": "Servo", "menge": 3, "einheit": "Stk"}]


def test_search_history_rejects_blank_and_absurd_queries():
    procurement = service()

    with pytest.raises(ValidationError, match="Suchtext fehlt"):
        procurement.search_history("  ")
    with pytest.raises(ValidationError, match="höchstens 200 Zeichen"):
        procurement.search_history("x" * 201)


def test_next_job_and_check_line_are_single_repository_calls():
    procurement = service()

    assert procurement.next_job()["id"] == 5
    checked = procurement.check_line(10)

    assert set(checked) == {"line", "stock", "previous_purchases", "cached_offers"}


def test_record_shop_maps_the_country_to_a_configured_delivery_target():
    procurement = service()

    shop = procurement.record_shop(
        "Neu",
        "https://neu.ch/",
        "CH",
        7.9,
        100,
        None,
        3,
        "https://neu.ch/versand",
        "Pauschale Versand CHF 7.90; Lieferung in 3 Arbeitstagen",
    )

    assert shop["status"] == "ungeprueft"
    assert shop["domain"] == "neu.ch"
    assert shop["profil_quelle_url"] == "https://neu.ch/versand"
    # Land wird auf das konfigurierte Ziel abgebildet, die Signatur bleibt gleich.
    assert shop["lieferziel_id"] == 1
    assert shop["land"] == "CH"

    deutsch = procurement.record_shop(
        "Reichelt DE", "https://www.reichelt.de/", "DE", 5.95, 100, None, 3,
        "https://www.reichelt.de/versand", "Versand 5,95 EUR",
    )
    assert deutsch["lieferziel_id"] == 2
    assert deutsch["land"] == "DE"

    # Ein Land ohne konfigurierte Adresse wird abgewiesen, nicht erfunden.
    with pytest.raises(ValidationError, match="Keine Lieferadresse für Land US"):
        procurement.record_shop(
            "US Shop", "https://us.example", "US", 5, None, None, 3,
            "https://us.example/versand", "Versand 5 USD",
        )
    with pytest.raises(ValidationError, match="Profil-Quelle"):
        procurement.record_shop(
            "Ohne Quelle", "https://ohne.ch", "CH", 5, None, None, None, "", "Versand CHF 5",
        )
    with pytest.raises(ValidationError, match="Versand-Originaltext"):
        procurement.record_shop(
            "Ohne Text", "https://ohne.ch", "CH", 5, None, None, None,
            "https://ohne.ch/versand", "",
        )


def test_shop_profile_audit_allows_unknown_default_days_but_never_unsourced_values():
    repository = FakeProcurementRepository()
    procurement = ProcurementService(repository)

    result = procurement.record_shop_profile(
        1,
        versand_chf=7.9,
        gratis_ab_chf=None,
        mindestbestellwert_chf=None,
        lieferzeit_default_tage=None,
        profil_quelle_url="https://shop.ch/versand",
        versand_text="B-Post Economy CHF 7.90; keine Lieferdauer genannt",
    )

    assert result["lieferzeit_default_tage"] is None
    assert result["profil_quelle_url"].endswith("/versand")


def test_delivery_text_parser_uses_conservative_range_upper_bound():
    assert parse_delivery_upper_days("3-4 Tage, bei Lieferant an Lager") == 4
    assert parse_delivery_upper_days("Lieferung in 2 Tagen") == 2
    assert parse_delivery_upper_days("sofort lieferbar") is None
    assert parse_delivery_upper_days(None) is None


def test_record_offer_stores_literal_source_text_and_parsed_upper_bound():
    procurement = service()

    offer = procurement.record_offer(
        10,
        1,
        "MG996R",
        "https://shop.example.ch/mg996r",
        12.5,
        "3-4 Tage, bei Lieferant an Lager",
        "Filiale rot; CH-Lieferant an Lager",
    )

    assert offer["quelle_url"] == "https://shop.example.ch/mg996r"
    assert offer["lieferzeit_tage"] == 4
    assert offer["lieferzeit_text"] == "3-4 Tage, bei Lieferant an Lager"
    assert offer["lager_text"] == "Filiale rot; CH-Lieferant an Lager"


def test_record_offer_stores_marketplace_provenance_text():
    procurement = service()

    offer = procurement.record_offer(
        10,
        1,
        "MG996R",
        "https://shop.example.ch/mg996r",
        12.5,
        "3-4 Tage",
        "lagernd",
        artikelnummer="SKU-99",
        provenienz_text="Verkauf und Versand durch Amazon",
    )

    assert offer["artikelnummer"] == "SKU-99"
    assert offer["provenienz_text"] == "Verkauf und Versand durch Amazon"


def test_record_offer_without_delivery_source_text_keeps_days_empty():
    procurement = service()

    offer = procurement.record_offer(
        10, 1, "MG996R", "https://shop.example.ch/mg996r", 12.5, None, "lagernd"
    )

    assert offer["lieferzeit_tage"] is None
    assert offer["lieferzeit_text"] is None


def test_record_offer_rejects_parsed_days_without_literal_source_text(monkeypatch):
    procurement = service()
    monkeypatch.setattr("app.procurement.parse_delivery_upper_days", lambda _: 2)

    with pytest.raises(ValidationError, match="Originaltext"):
        procurement.record_offer(
            10, 1, "MG996R", "https://shop.example.ch/mg996r", 12.5, None, "lagernd"
        )


def test_record_offer_validates_price_domain_shop_and_line():
    procurement = service()

    offer = procurement.record_offer(
        10, 1, "MG996R", "https://shop.example.ch/mg996r", 12.5, "2 Tage", "lagernd"
    )
    assert offer["quelle_url"] == "https://shop.example.ch/mg996r"

    with pytest.raises(ValidationError, match="Preis"):
        procurement.record_offer(10, 1, "MG996R", "https://shop.example.ch/x", 0)
    with pytest.raises(ValidationError, match="Shop-Domain"):
        procurement.record_offer(10, 1, "MG996R", "https://other.ch/x", 10)
    with pytest.raises(ValidationError, match="unbekannt"):
        procurement.record_offer(10, 999, "MG996R", "https://other.ch/x", 10)

    procurement.repository.shops[1]["status"] = "gesperrt"
    with pytest.raises(ValidationError, match="gesperrt"):
        procurement.record_offer(10, 1, "MG996R", "https://shop.example.ch/x", 10)


def test_mark_line_rejects_unknown_status():
    procurement = service()

    with pytest.raises(ValidationError, match="Status"):
        procurement.mark_line(10, "irgendwas")
    assert procurement.mark_line(10, "nichts_gefunden", "Keine CH-Quelle")["status"] == "nichts_gefunden"


def test_plan_order_calls_pure_optimizer_and_serializes_variant():
    procurement = service()

    variants = procurement.plan_order(5, 0.5)

    assert variants[0]["shop_ids"] == [1]
    assert variants[0]["assignments"] == {"10": 31}
    assert variants[0]["total_chf"] == "28.00"
    assert variants[0]["score"] == "43.000"


def test_plan_scenarios_serializes_unknown_shipping_without_stringifying_none():
    repository = FakeProcurementRepository()
    repository.optimization_input = lambda _: {
        **FakeProcurementRepository().optimization_input(5),
        "shops": [{
            "id": 1, "name": "Checkout", "url": "https://shop.example.ch",
            "versand_chf": None, "gratis_ab_chf": None,
            "mindestbestellwert_chf": None, "lieferzeit_default_tage": 3,
            "versand_original": None, "gratis_ab_original": None,
            "mindestbestellwert_original": None, "versand_waehrung": "USD",
            "versand_kurs": None, "versand_kurs_am": None,
            "versand_kurs_quelle": None,
        }],
    }

    result = ProcurementService(repository).plan_scenarios(5)
    scenario = result["scenarios"][0]

    assert scenario["contains_unknown_shipping"] is True
    assert scenario["shipping"]["1"] is None
    assert scenario["shops"][0]["versand_chf"] is None
    assert scenario["shops"][0]["versand_original"] is None
    assert scenario["shops"][0]["versand_waehrung"] == "USD"
    assert scenario["shops"][0]["versand_unbekannt"] is True


def test_plan_scenarios_groups_identical_presets_and_keeps_badges():
    """Ohne Auslandsangebote faellt «Nur Schweiz» mit dem Gesamtoptimum zusammen.

    Genau dann darf es keine eigene Karte werden, sondern muss in die
    Hauptkarte verschmelzen - eigenstaendig wird es erst, wenn es Angebote
    ausserhalb der Heimat gibt.
    """
    procurement = service()

    result = procurement.plan_scenarios(5)

    assert len(result["scenarios"]) == 1
    scenario = result["scenarios"][0]
    assert scenario["keys"] == ["cheapest", "fastest", "one_shop", "balanced", "only_ch"]
    assert scenario["labels"] == [
        "Am günstigsten",
        "Am schnellsten",
        "Ein Shop",
        "Ausgewogen",
        "Nur Schweiz",
    ]
    assert scenario["same_result_note"]
    # Reiner CH-Plan: kein Aufschlag, keine Aufschlagszeile.
    assert scenario["aufschlaege"] == []
    assert scenario["aufschlag_chf"] == "0.00"
    assert scenario["contains_estimates"] is False
    assert scenario["fastest_max_exclusively_estimated"] is False
    assert scenario["lines"][0]["lieferzeit_text"] == "2 Tage"
    assert scenario["lines"][0]["produkt_url"].endswith("/mg996r")
    assert scenario["complete"] is True
    assert scenario["incomplete"] is False
    assert scenario["shop_count"] == 1


def test_matrix_payload_contains_provenance_delivery_source_shop_breakdown_and_candidates():
    result = service().plan_scenarios(5)
    scenario = result["scenarios"][0]
    line = scenario["lines"][0]
    shop = scenario["shops"][0]
    candidate = result["lines"][0]["candidates"][0]

    assert result["ready"] is True
    assert line["lieferzeit_quelle"] == "produktseite"
    assert line["lieferzeit_chip"] == "2 Tage"
    assert line["lager_text"] == "5 Stück ab Lager"
    assert line["quelle_url"].endswith("/mg996r")
    assert shop["artikelanzahl"] == 1
    assert shop["gratis_ab_chf"] is None
    assert shop["versand_gratis"] is False
    assert candidate["offer_id"] == 31
    assert candidate["last_candidate"] is True
    assert candidate["lieferzeit_chip"] == "2 Tage"
    assert candidate["provenienz_text"] == "Verkauf und Versand durch Amazon"


def test_shop_breakdown_is_sorted_by_subtotal_descending_like_reference():
    repository = FakeProcurementRepository()
    data = repository.optimization_input(5)
    second_offer = {
        **data["offers"][0],
        "id": 32,
        "line_id": 11,
        "shop_id": 2,
        "preis_chf": "30",
        "menge": 1,
        "suchtext": "Netzteil",
        "position": 2,
        "produktname": "Testnetzteil",
    }
    data["offers"] = [data["offers"][0], second_offer]
    data["shops"].append(
        {
            "id": 2,
            "name": "Grösserer Warenkorb",
            "url": "https://shop2.example.ch",
            "versand_chf": "0",
            "gratis_ab_chf": None,
            "mindestbestellwert_chf": None,
            "lieferzeit_default_tage": 2,
        }
    )
    data["required_line_ids"] = [10, 11]
    data["lines"].append(
        {"id": 11, "position": 2, "suchtext": "Netzteil", "menge": 1}
    )
    repository.optimization_input = lambda job_id: data

    scenario = ProcurementService(repository).plan_scenarios(5)["scenarios"][0]

    assert [shop["id"] for shop in scenario["shops"]] == [2, 1]


def test_identical_tuned_result_has_verdict_and_no_extra_column():
    result = service().plan_scenarios(5, tempo=0.5)

    assert result["custom"] is None
    assert result["custom_verdict"] == (
        "Ändert bei diesem Angebots-Pool nichts: Am günstigsten bleibt die beste Lösung "
        "(max. 2 Tage). Teurere oder unsicherere Pläne werden nicht angezeigt."
    )


def test_selected_matrix_plan_is_validated_saved_and_returned_after_reload():
    repository = FakeProcurementRepository()
    procurement = ProcurementService(repository)
    plan = procurement.plan_scenarios(5)["scenarios"][0]

    saved = procurement.select_plan(5, plan["assignments"])
    reloaded = procurement.plan_scenarios(5)

    assert saved["selected_assignments"] == {"10": 31}
    assert repository.selected_assignments == {"10": 31}
    assert reloaded["selected_assignments"] == {"10": 31}
    assert reloaded["selected_key"] == plan["key"]

    with pytest.raises(ValidationError, match="Plan"):
        procurement.select_plan(5, {"10": 999})


def test_plan_delta_uses_same_server_optimizer_with_hypothetical_pin():
    repository = FakeProcurementRepository()
    base = repository.optimization_input(5)
    alternative = {
        **base["offers"][0],
        "id": 32,
        "preis_chf": "12",
        "produktname": "Alternativer Servo",
        "produkt_url": "https://shop.example.ch/alternative",
        "quelle_url": "https://shop.example.ch/alternative",
    }
    repository.optimization_input = lambda job_id: {
        **base,
        "offers": [base["offers"][0], alternative],
        "selected_assignments": repository.selected_assignments,
    }
    procurement = ProcurementService(repository)
    baseline = procurement.plan_scenarios(5)["scenarios"][0]

    delta = procurement.plan_delta(
        5,
        line_id=10,
        offer_id=32,
        base_assignments=baseline["assignments"],
        tempo=0.5,
    )

    assert delta["assignments"] == {"10": 32}
    assert delta["delta_chf"] == "4.00"
    assert delta["max_liefertage"] == 2


def test_fastest_marks_when_its_maximum_is_based_only_on_estimates():
    repository = FakeProcurementRepository()
    repository.optimization_input = lambda job_id: {
        **FakeProcurementRepository().optimization_input(job_id),
        "offers": [
            {
                **FakeProcurementRepository().optimization_input(job_id)["offers"][0],
                "lieferzeit_tage": None,
                "lieferzeit_text": None,
            }
        ],
    }
    procurement = ProcurementService(repository)

    scenario = procurement.plan_scenarios(5)["scenarios"][0]

    assert "fastest" in scenario["keys"]
    assert scenario["max_liefertage"] == 3
    assert scenario["fastest_max_exclusively_estimated"] is True
    assert scenario["max_delivery_only_estimated"] is True


def test_fastest_rejects_cheapest_unknown_delivery_and_serializes_it_as_unknown():
    repository = FakeProcurementRepository()
    base = repository.optimization_input(5)
    cheap_unknown = {
        **base["offers"][0],
        "id": 31,
        "shop_id": 1,
        "preis_chf": "1.00",
        "lieferzeit_tage": None,
        "lieferzeit_text": None,
    }
    known_alternative = {
        **base["offers"][0],
        "id": 32,
        "shop_id": 2,
        "preis_chf": "2.00",
        "lieferzeit_tage": None,
        "lieferzeit_text": None,
        "produktname": "MG996R Servo bekannte Lieferzeit",
        "produkt_url": "https://known.example.ch/mg996r",
    }
    repository.optimization_input = lambda job_id: {
        **base,
        "offers": [cheap_unknown, known_alternative],
        "shops": [
            {
                "id": 1,
                "name": "Unbekannt",
                "url": "https://unknown.example.ch",
                "versand_chf": "0",
                "gratis_ab_chf": None,
                "mindestbestellwert_chf": None,
                "lieferzeit_default_tage": None,
            },
            {
                "id": 2,
                "name": "Bekannt",
                "url": "https://known.example.ch",
                "versand_chf": "0",
                "gratis_ab_chf": None,
                "mindestbestellwert_chf": None,
                "lieferzeit_default_tage": 3,
            },
        ],
    }
    procurement = ProcurementService(repository)

    scenarios = procurement.plan_scenarios(5)["scenarios"]
    cheapest = next(item for item in scenarios if "cheapest" in item["keys"])
    fastest = next(item for item in scenarios if "fastest" in item["keys"])

    assert cheapest["assignments"] == {"10": 31}
    assert cheapest["contains_unknown_delivery"] is True
    assert cheapest["lines"][0]["lieferzeit_tage"] is None
    assert cheapest["lines"][0]["lieferzeit_geschaetzt"] is False
    assert fastest["assignments"] == {"10": 32}
    assert fastest["max_liefertage"] == 3
    assert fastest["contains_unknown_delivery"] is False


def test_plan_order_returns_no_partial_variant_when_a_required_line_lacks_confirmation():
    procurement = service()
    original = procurement.repository.optimization_input

    def incomplete(job_id):
        data = original(job_id)
        data["required_line_ids"] = [10, 11]
        return data

    procurement.repository.optimization_input = incomplete

    assert procurement.plan_order(5, 0.5) == []



def test_record_purchase_requires_valid_timestamp_shop_promises_and_variant():
    repository = FakeProcurementRepository()
    procurement = ProcurementService(repository)
    variant = procurement.plan_order(5, 0)[0]
    variant["labels"] = ["Am günstigsten"]
    variant["lines"] = [{"produktname": "Übersetztes Produkt"}]

    purchase = procurement.record_purchase(
        5,
        variant,
        datetime(2026, 8, 9, tzinfo=timezone.utc).isoformat(),
        {"1": 3},
    )

    assert purchase["id"] == 90
    stored_variant = repository.purchases[0][1]
    assert "labels" not in stored_variant
    assert "lines" not in stored_variant
    with pytest.raises(ValidationError, match="Liefertage"):
        procurement.record_purchase(5, variant, "2026-08-09T12:00:00+00:00", {})
    with pytest.raises(ValidationError, match="Variante"):
        procurement.record_purchase(5, {"shop_ids": [1]}, "2026-08-09T12:00:00+00:00", {"1": 3})


def test_record_purchase_rejects_a_plan_with_unknown_shipping():
    repository = FakeProcurementRepository()
    original = repository.optimization_input

    def with_unknown_shipping(job_id):
        data = original(job_id)
        data["shops"][0]["versand_chf"] = None
        return data

    repository.optimization_input = with_unknown_shipping
    procurement = ProcurementService(repository)
    variant = procurement.plan_order(5, 0)[0]

    with pytest.raises(ValidationError, match="Versandkosten sind unbekannt"):
        procurement.record_purchase(
            5,
            variant,
            "2026-08-09T12:00:00+00:00",
            {"1": 3},
        )

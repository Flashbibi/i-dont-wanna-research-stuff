"""Service-Pfad der Warenkorb-Übergabe.

Hier liegt die Entscheidung, *ob* ein Erkennungsergebnis persistiert wird.
Kein Test spricht mit einem echten Shop.
"""

import pytest

from app.cart import CartTemporaryError, CartVerificationError
from app.procurement import ProcurementService, ValidationError
from tests.test_cart import (
    DUPONT_URL,
    HOME_HTML,
    PRODUCT_HTML,
    FakeResponse,
    FakeSession,
    cart_html,
    cart_row,
)

SHOP_URL = "https://www.bastelgarage.ch/"
DUPONT_ROW = cart_row(
    DUPONT_URL, "Dupont Jumper Cable Set 10cm", 2, "CHF 5.90", "CHF 11.80", 42, "420027"
)


class CartRepository:
    def __init__(
        self, *, plattform=None, plattform_geprueft_am=None, shop_produkt_id=None
    ):
        self.shops = {
            1: {
                "id": 1,
                "name": "Bastelgarage",
                "url": SHOP_URL,
                "domain": "bastelgarage.ch",
                "status": "bestaetigt",
                "plattform": plattform,
                "plattform_beleg": None,
                "plattform_geprueft_am": plattform_geprueft_am,
            }
        }
        self.shop_produkt_id = shop_produkt_id
        self.saved_platforms = []
        self.saved_product_ids = []
        self.saved_artikelnummern = []
        self.selected_assignments = {"10": 31}

    def get_shop(self, shop_id):
        return self.shops.get(shop_id)

    def save_shop_platform(self, shop_id, plattform, plattform_beleg):
        if not plattform_beleg or not plattform_beleg.strip():
            raise ValueError("Plattform darf nicht ohne Beleg gespeichert werden")
        self.saved_platforms.append((shop_id, plattform, plattform_beleg))
        self.shops[shop_id].update(
            plattform=plattform,
            plattform_beleg=plattform_beleg,
            plattform_geprueft_am="jetzt",
        )
        return self.shops[shop_id]

    def save_offer_product_ids(self, produkt_ids):
        self.saved_product_ids.append(dict(produkt_ids))
        return len(produkt_ids)

    def save_offer_artikelnummern(self, artikelnummern):
        self.saved_artikelnummern.append(dict(artikelnummern))
        return len(artikelnummern)

    def optimization_input(self, job_id):
        return {
            "offers": [
                {
                    "id": 31,
                    "line_id": 10,
                    "shop_id": 1,
                    "preis_chf": "5.90",
                    "menge": 2,
                    "lieferzeit_tage": 2,
                    "lieferzeit_text": "2 Tage",
                    "lager_text": "ab Lager",
                    "quelle_url": DUPONT_URL,
                    "produktname": "Dupont Jumper Cable Set 10cm",
                    "produkt_url": DUPONT_URL,
                    "shop_produkt_id": self.shop_produkt_id,
                    "suchtext": "Dupont",
                    "position": 1,
                    "override_status": None,
                }
            ],
            "shops": [
                {
                    "id": 1,
                    "name": "Bastelgarage",
                    "url": SHOP_URL,
                    "versand_chf": "6.90",
                    "gratis_ab_chf": "80",
                    "mindestbestellwert_chf": None,
                    "lieferzeit_default_tage": 2,
                    "plattform": self.shops[1]["plattform"],
                    "plattform_beleg": self.shops[1]["plattform_beleg"],
                    "plattform_geprueft_am": self.shops[1]["plattform_geprueft_am"],
                }
            ],
            "required_line_ids": [10],
            "lines": [{"id": 10, "position": 1, "suchtext": "Dupont", "menge": 2}],
            "selected_assignments": self.selected_assignments,
        }


def session_factory(session):
    return lambda: session


def good_session(**kwargs):
    return FakeSession(
        pages={SHOP_URL: HOME_HTML, DUPONT_URL: PRODUCT_HTML},
        cart_page=cart_html(DUPONT_ROW, "CHF 11.80"),
        **kwargs,
    )


def test_one_press_detects_then_fills_and_verifies():
    repository = CartRepository()
    service = ProcurementService(repository)
    session = good_session()

    result = service.fill_cart(5, 1, session_factory=session_factory(session))

    assert result["status"] == "uebergabe"
    assert result["plattform"] == "opencart"
    assert result["verifiziert"] is True
    assert result["artikel_anzahl"] == 2
    assert result["total_chf"] == "11.80"
    assert result["cookie"] == {"name": "OCSESSID", "wert": "sess-abc-123"}
    # Erkennung und Füllen im selben Versuch, kein zweiter Klick.
    assert len(repository.saved_platforms) == 1
    assert repository.saved_platforms[0][1] == "opencart"
    assert repository.saved_product_ids == [{31: "96"}]
    # Die Artikelnummer kommt von derselben Produktseite und wird mitgecacht.
    assert repository.saved_artikelnummern == [{31: "420027"}]


def test_a_timeout_during_detection_persists_nothing_and_stays_repeatable():
    """Der Fall, der einen unterstützten Shop dauerhaft stummschalten würde."""
    repository = CartRepository()
    service = ProcurementService(repository)

    class TimingOutSession(FakeSession):
        def get(self, url):
            raise TimeoutError("read timeout")

    with pytest.raises(CartTemporaryError):
        service.fill_cart(5, 1, session_factory=session_factory(TimingOutSession()))

    assert repository.saved_platforms == []
    assert repository.shops[1]["plattform"] is None
    assert repository.shops[1]["plattform_geprueft_am"] is None
    # Der Shop bleibt füllbar, der Knopf verschwindet nicht.
    assert service.cart_shops(5)[0]["kann_fuellen"] is True


def test_an_error_response_during_detection_also_persists_nothing():
    repository = CartRepository()
    service = ProcurementService(repository)
    session = FakeSession(pages={SHOP_URL: FakeResponse("Wartung", status_code=503)})

    with pytest.raises(CartTemporaryError):
        service.fill_cart(5, 1, session_factory=session_factory(session))

    assert repository.saved_platforms == []
    assert service.cart_shops(5)[0]["kann_fuellen"] is True


def test_an_unsupported_shop_is_a_result_not_an_error_and_silences_the_button():
    repository = CartRepository()
    service = ProcurementService(repository)
    session = FakeSession(
        pages={
            SHOP_URL: '<html><body><link href="/wp-content/plugins/woocommerce/x.css"></body></html>'
        },
        cookies={"PHPSESSID": "x"},
    )

    result = service.fill_cart(5, 1, session_factory=session_factory(session))

    assert result["status"] == "nicht_unterstuetzt"
    assert result["plattform"] == "woocommerce"
    assert result["text"] == (
        "Plattform geprüft: WooCommerce – für diesen Shop bleibt die Bestellliste."
    )
    assert "cookie" not in result
    # Abgeschlossene Erkennung wird festgehalten, der Knopf verschwindet.
    assert repository.saved_platforms[0][1] == "woocommerce"
    assert service.cart_shops(5)[0]["kann_fuellen"] is False


def test_a_shop_without_known_markers_is_recorded_as_checked_with_evidence():
    repository = CartRepository()
    service = ProcurementService(repository)
    session = FakeSession(
        pages={SHOP_URL: "<html><body>Ein Shop</body></html>"},
        cookies={"PHPSESSID": "x"},
    )

    result = service.fill_cart(5, 1, session_factory=session_factory(session))

    assert result["status"] == "nicht_unterstuetzt"
    assert result["plattform"] is None
    assert "keine bekannte Plattform" in result["text"]
    shop_id, plattform, beleg = repository.saved_platforms[0]
    assert plattform is None
    assert beleg.strip()  # auch der negative Befund wird belegt
    assert service.cart_shops(5)[0]["kann_fuellen"] is False


def test_a_known_platform_skips_detection_and_does_not_rewrite_it():
    repository = CartRepository(plattform="opencart", plattform_geprueft_am="frueher")
    service = ProcurementService(repository)

    result = service.fill_cart(5, 1, session_factory=session_factory(good_session()))

    assert result["status"] == "uebergabe"
    assert repository.saved_platforms == []


def test_a_cached_product_id_is_not_written_again():
    repository = CartRepository(
        plattform="opencart", plattform_geprueft_am="frueher", shop_produkt_id="96"
    )
    service = ProcurementService(repository)
    session = FakeSession(
        pages={SHOP_URL: HOME_HTML, DUPONT_URL: PRODUCT_HTML},
        cart_page=cart_html(DUPONT_ROW, "CHF 11.80"),
    )

    result = service.fill_cart(5, 1, session_factory=session_factory(session))

    assert result["status"] == "uebergabe"
    # Die ID kam aus dem Cache, es gibt nichts nachzutragen. Die Produktseite
    # wird trotzdem einmal abgerufen - sie legt den Sprachkontext des Korbs
    # fest (Vorfall 2026-08-11).
    assert repository.saved_product_ids == []
    assert session.fetched.count(DUPONT_URL) == 1


def test_a_mismatching_cart_blocks_the_handover():
    repository = CartRepository(plattform="opencart", plattform_geprueft_am="frueher")
    service = ProcurementService(repository)
    session = FakeSession(
        pages={SHOP_URL: HOME_HTML, DUPONT_URL: PRODUCT_HTML},
        cart_page=cart_html(
            cart_row(
                DUPONT_URL,
                "Dupont Jumper Cable Set 10cm",
                2,
                "CHF 6.10",
                "CHF 12.20",
                42,
                "420027",
            ),
            "CHF 12.20",
        ),
    )

    with pytest.raises(CartVerificationError) as error:
        service.fill_cart(5, 1, session_factory=session_factory(session))

    assert "erfasst CHF 11.80" in str(error.value)
    assert "sess-abc-123" not in str(error.value)


def test_an_unknown_shop_is_rejected_before_any_request():
    service = ProcurementService(CartRepository())
    session = good_session()

    with pytest.raises(ValidationError):
        service.fill_cart(5, 99, session_factory=session_factory(session))

    assert session.fetched == []


def test_cart_shops_lists_the_selected_plan_per_shop():
    service = ProcurementService(CartRepository())

    shops = service.cart_shops(5)

    assert len(shops) == 1
    assert shops[0]["shop_id"] == 1
    assert shops[0]["shop_name"] == "Bastelgarage"
    assert shops[0]["plattform"] is None
    assert shops[0]["plattform_geprueft"] is False
    assert shops[0]["kann_fuellen"] is True

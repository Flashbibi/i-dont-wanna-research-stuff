"""Adapter-Tests gegen gemockte HTTP-Antworten.

Kein Test in dieser Datei spricht mit einem echten Shop. Die Fixtures bilden
OpenCart-3-Markup nach, wie es Bastelgarage ausliefert.
"""

from decimal import Decimal

import pytest

from app.cart import (
    PLATFORM_OPENCART,
    CartError,
    CartItem,
    CartTemporaryError,
    CartUnsupported,
    CartVerificationError,
    OpenCartAdapter,
    build_adapter,
    detect_platform,
    extract_opencart_product_id,
    parse_chf,
    parse_opencart_cart,
    start_guest_session,
)


SHOP_URL = "https://www.bastelgarage.ch/"
DUPONT_URL = "https://www.bastelgarage.ch/dupont-jumper-cable-set-10cm-60-pieces"
BREADBOARD_URL = "https://www.bastelgarage.ch/half-size-transparent-breadboard-zy-60"

HOME_HTML = """
<html><head><link href="catalog/view/theme/custom/stylesheet.css"></head>
<body><a href="index.php?route=checkout/cart">Warenkorb</a></body></html>
"""

PRODUCT_HTML = """
<html><body>
  <div class="product">
    <label for="input-quantity">Anzahl</label>
    <input type="text" name="quantity" value="1" id="input-quantity" />
    <input type="hidden" name="product_id" value="96" />
    <button type="button" id="button-cart">In den Warenkorb</button>
  </div>
  <div class="related">
    <button onclick="wishlist.add('227');">Merken</button>
    <button onclick="compare.add('2569');">Vergleichen</button>
  </div>
</body></html>
"""

BREADBOARD_HTML = PRODUCT_HTML.replace('value="96"', 'value="412"')


def cart_html(rows: str, zwischensumme: str) -> str:
    return f"""
    <html><body>
    <table class="table table-bordered"><tbody>{rows}</tbody></table>
    <table class="table table-bordered">
      <tr><td class="text-right"><strong>Zwischensumme:</strong></td>
          <td class="text-right">{zwischensumme}</td></tr>
      <tr><td class="text-right"><strong>Versandkosten:</strong></td>
          <td class="text-right">CHF 6.90</td></tr>
      <tr><td class="text-right"><strong>Total:</strong></td>
          <td class="text-right">CHF 25.60</td></tr>
    </table></body></html>
    """


# Wörtlich aus der echten Bastelgarage-Korbseite übernommen (Sondierung
# 2026-08-10). Entscheidend: die Zeilenpreise sind brutto, der Summenblock
# weist Sub-Total NETTO aus - 17.30 netto zu 18.70 brutto bei 8.1 % MWST.
BASTELGARAGE_CART_HTML = """
<html><body>
<table class="table table-bordered"><tbody>
  <tr>
    <td class="text-center"> <a href="https://www.bastelgarage.ch/dupont-jumper-cable-set-10cm-60-pieces"><img src="https://www.bastelgarage.ch/image/cache/catalog/Artikel/420021-420030/420027-969-47x47.jpg" alt="Dupont Jumper Cable Set 10cm 60 pieces." class="img-thumbnail" /></a> </td>
    <td class="text-left"><a href="https://www.bastelgarage.ch/dupont-jumper-cable-set-10cm-60-pieces">Dupont Jumper Cable Set 10cm 60 pieces.</a></td>
    <td class="text-left">420027</td>
    <td class="text-left"><div class="input-group btn-block" style="max-width: 200px;">
        <input type="text" name="quantity[550684]" value="2" size="1" class="form-control" />
        </div></td>
    <td class="text-right">CHF5.90</td>
    <td class="text-right">CHF11.80</td>
  </tr>
  <tr>
    <td class="text-center"> <a href="https://www.bastelgarage.ch/half-size-transparent-breadboard-zy-60"><img src="/image/x.jpg" class="img-thumbnail" /></a> </td>
    <td class="text-left"><a href="https://www.bastelgarage.ch/half-size-transparent-breadboard-zy-60">Half-Size Transparent Breadboard ZY-60</a></td>
    <td class="text-left">420467</td>
    <td class="text-left"><div class="input-group btn-block">
        <input type="text" name="quantity[550685]" value="1" size="1" class="form-control" />
        </div></td>
    <td class="text-right">CHF6.90</td>
    <td class="text-right">CHF6.90</td>
  </tr>
</tbody></table>
<table class="table table-bordered">
  <tr><td class="text-right"><strong>Sub-Total</strong></td> <td class="text-right">CHF17.30</td></tr>
  <tr><td class="text-right"><strong>8.1% Mwst</strong></td> <td class="text-right">CHF1.40</td></tr>
  <tr><td class="text-right"><strong>Total</strong></td> <td class="text-right">CHF18.70</td></tr>
</table>
</body></html>
"""

BG_DUPONT = "https://www.bastelgarage.ch/dupont-jumper-cable-set-10cm-60-pieces"
BG_BREADBOARD = "https://www.bastelgarage.ch/half-size-transparent-breadboard-zy-60"

# Echte Fehlantwort aus dem Vorfall vom 2026-08-11: derselbe Shop, dieselben
# Produkt-IDs, aber der Korb rendert die Links in der Shop-Default-Sprache.
# Die erfassten produkt_url sind die englischen Slugs - keiner davon kommt hier
# vor, obwohl es exakt dieselben Produkte sind.
GERMAN_CART_HTML = """
<html><body>
<table class="table table-bordered"><tbody>
  <tr>
    <td class="text-center"> <a href="https://www.bastelgarage.ch/breadboard-lochraster-steckplatine-half-size"><img src="https://www.bastelgarage.ch/image/cache/catalog/Artikel/420011-420020/420018-9816-47x47.jpg" alt="Breadboard / Lochraster Steckplatine Half-Size" class="img-thumbnail" /></a> </td>
    <td class="text-left"><a href="https://www.bastelgarage.ch/breadboard-lochraster-steckplatine-half-size">Breadboard / Lochraster Steckplatine Half-Size</a> </td>
    <td class="text-left">420018</td>
    <td class="text-left"><div class="input-group btn-block"><input type="text" name="quantity[550847]" value="1" size="1" class="form-control" /></div></td>
    <td class="text-right">CHF4.90</td>
    <td class="text-right">CHF4.90</td>
  </tr>
  <tr>
    <td class="text-center"> <a href="https://www.bastelgarage.ch/jumperkabel-dupont-set-10cm-60-stk"><img src="/image/x.jpg" class="img-thumbnail" /></a> </td>
    <td class="text-left"><a href="https://www.bastelgarage.ch/jumperkabel-dupont-set-10cm-60-stk">Jumperkabel Dupont Set 10cm 60 Stk</a> </td>
    <td class="text-left">420027</td>
    <td class="text-left"><div class="input-group btn-block"><input type="text" name="quantity[550848]" value="1" size="1" class="form-control" /></div></td>
    <td class="text-right">CHF5.90</td>
    <td class="text-right">CHF5.90</td>
  </tr>
</tbody></table>
<table class="table table-bordered">
  <tr><td class="text-right"><strong>Sub-Total</strong></td> <td class="text-right">CHF10.00</td></tr>
  <tr><td class="text-right"><strong>8.1% Mwst</strong></td> <td class="text-right">CHF0.80</td></tr>
  <tr><td class="text-right"><strong>Total</strong></td> <td class="text-right">CHF10.80</td></tr>
</table>
</body></html>
"""


def cart_row(url: str, name: str, menge: int, einzel: str, zeile: str, key: int) -> str:
    return f"""
    <tr>
      <td class="text-center"><a href="{url}"><img src="/image/x.jpg" alt="{name}" /></a></td>
      <td class="text-left"><a href="{url}">{name}</a></td>
      <td class="text-left">BG-{key}</td>
      <td class="text-left">
        <input type="text" name="quantity[{key}]" value="{menge}" size="1" class="form-control" />
      </td>
      <td class="text-right">{einzel}</td>
      <td class="text-right">{zeile}</td>
    </tr>
    """


DUPONT_ROW = cart_row(DUPONT_URL, "Dupont Jumper Cable Set 10cm", 2, "CHF 5.90", "CHF 11.80", 42)
BREADBOARD_ROW = cart_row(BREADBOARD_URL, "Breadboard Half Size", 1, "CHF 6.90", "CHF 6.90", 43)


def item(url=DUPONT_URL, *, offer_id=31, menge=2, preis="5.90", name="Dupont Jumper Cable Set 10cm",
         line_id=10, shop_produkt_id=None) -> CartItem:
    return CartItem(
        line_id=line_id,
        offer_id=offer_id,
        produktname=name,
        produkt_url=url,
        menge=menge,
        einzelpreis_chf=Decimal(preis),
        shop_produkt_id=shop_produkt_id,
    )


class FakeResponse:
    def __init__(self, text="", status_code=200, payload=None):
        self.text = text
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("keine JSON-Antwort")
        return self._payload


class FakeSession:
    """Attrappe. Kennt nur die Antworten, die ihr mitgegeben wurden."""

    def __init__(self, pages=None, add_responses=None, cart_page=None, cookies=None):
        self.pages = dict(pages or {})
        self.add_responses = list(add_responses or [])
        self.cart_page = cart_page
        self.cookies = dict(cookies if cookies is not None else {"OCSESSID": "sess-abc-123"})
        self.posted = []
        self.fetched = []

    def get(self, url):
        self.fetched.append(url)
        if "route=checkout/cart" in url:
            return FakeResponse(self.cart_page or "")
        if url in self.pages:
            page = self.pages[url]
            return page if isinstance(page, FakeResponse) else FakeResponse(page)
        if url.rstrip("/") == SHOP_URL.rstrip("/"):
            return FakeResponse(HOME_HTML)
        return FakeResponse("", status_code=404)

    def post(self, url, data):
        self.posted.append((url, dict(data)))
        if self.add_responses:
            return self.add_responses.pop(0)
        return FakeResponse(payload={"success": "Artikel hinzugefügt"})


def adapter(**kwargs) -> tuple[OpenCartAdapter, FakeSession]:
    session = FakeSession(**kwargs)
    return OpenCartAdapter(session), session


# ---------------------------------------------------------------------------
# Betragsformate und Plattform-Erkennung
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("CHF 5.90", Decimal("5.90")),
        ("CHF5,90", Decimal("5.90")),
        ("CHF 1'234.55", Decimal("1234.55")),
        ("CHF 1'234,55", Decimal("1234.55")),
        ("Fr. 12.00", Decimal("12.00")),
    ],
)
def test_swiss_amount_formats_are_read_correctly(text, expected):
    assert parse_chf(text) == expected


def test_platform_detection_reports_opencart_with_evidence():
    evidence = detect_platform(HOME_HTML, ["OCSESSID", "currency"])

    assert evidence is not None
    assert evidence.plattform == PLATFORM_OPENCART
    assert "OCSESSID" in evidence.beleg
    assert "catalog/view/theme" in evidence.beleg


def test_platform_detection_returns_nothing_instead_of_guessing():
    assert detect_platform("<html><body>Ein Shop</body></html>", ["PHPSESSID"]) is None


def test_unimplemented_and_unknown_platforms_keep_the_link_list():
    for plattform in ("woocommerce", "shopify", None, "magento"):
        with pytest.raises(CartUnsupported):
            build_adapter(plattform, FakeSession())


# ---------------------------------------------------------------------------
# product_id-Extraktion
# ---------------------------------------------------------------------------

def test_product_id_comes_from_the_cart_form_not_from_related_products():
    assert extract_opencart_product_id(PRODUCT_HTML, DUPONT_URL) == "96"


def test_product_id_extraction_fails_loudly_when_absent():
    with pytest.raises(CartError) as error:
        extract_opencart_product_id("<html><body>kein Formular</body></html>", DUPONT_URL)

    assert "keine product_id" in str(error.value)
    assert DUPONT_URL in str(error.value)


def test_product_id_extraction_refuses_to_choose_between_candidates():
    ambiguous = PRODUCT_HTML + '<input type="hidden" name="product_id" value="777" />'

    with pytest.raises(CartError) as error:
        extract_opencart_product_id(ambiguous, DUPONT_URL)

    assert "Mehrdeutig" in str(error.value)
    assert "96" in str(error.value) and "777" in str(error.value)


# ---------------------------------------------------------------------------
# Korb zurücklesen
# ---------------------------------------------------------------------------

def test_cart_readback_yields_positions_and_the_totals_block():
    entries, totals = parse_opencart_cart(cart_html(DUPONT_ROW + BREADBOARD_ROW, "CHF 18.70"))

    assert [entry.menge for entry in entries] == [2, 1]
    assert entries[0].name == "Dupont Jumper Cable Set 10cm"
    assert entries[0].zeilensumme_chf == Decimal("11.80")
    assert totals["Zwischensumme"] == Decimal("18.70")


def test_an_unreadable_cart_page_is_refused_rather_than_assumed():
    with pytest.raises(CartError) as error:
        parse_opencart_cart("<html><body>Fehlerseite</body></html>")

    assert "nicht lesbar" in str(error.value)


def test_a_row_without_an_amount_is_refused():
    row = '<tr><td class="text-left">Ding</td><td><input name="quantity[7]" value="1" /></td></tr>'

    with pytest.raises(CartError) as error:
        parse_opencart_cart(f"<table><tbody>{row}</tbody></table>")

    assert "keinen Betrag" in str(error.value)


# ---------------------------------------------------------------------------
# Netto-Summenzeile: die Falle aus der Live-Abnahme
# ---------------------------------------------------------------------------

def test_the_real_cart_page_exposes_a_net_subtotal_next_to_gross_line_prices():
    """Beleg für die Basis: Sub-Total ist netto, die Zeilenpreise sind brutto."""
    entries, totals = parse_opencart_cart(BASTELGARAGE_CART_HTML)

    brutto = sum(entry.zeilensumme_chf for entry in entries)
    assert brutto == Decimal("18.70")
    assert totals["Sub-Total"] == Decimal("17.30")
    assert totals["Total"] == Decimal("18.70")
    # 8.1 % MWST - genau der Faktor, der die Live-Abnahme scheitern liess.
    assert (totals["Total"] / Decimal("1.081")).quantize(Decimal("0.01")) == totals["Sub-Total"]


def test_a_net_subtotal_no_longer_fakes_a_price_change():
    """Regression der Live-Abnahme: CHF 17.60 erfasst, Sub-Total meldete 16.28.

    Verglichen wird brutto gegen brutto, deshalb geht der Korb durch.
    """
    cart, _ = adapter(
        pages={BG_DUPONT: PRODUCT_HTML, BG_BREADBOARD: BREADBOARD_HTML},
        cart_page=BASTELGARAGE_CART_HTML,
    )

    result = cart.fill(
        SHOP_URL,
        [
            item(BG_DUPONT, offer_id=31, menge=2, preis="5.90",
                 name="Dupont Jumper Cable Set 10cm 60 pieces."),
            item(BG_BREADBOARD, offer_id=32, menge=1, preis="6.90",
                 name="Half-Size Transparent Breadboard ZY-60", line_id=11),
        ],
    )

    assert result.verifiziert is True
    assert result.artikel_anzahl == 3
    # Brutto, nicht der Netto-Sub-Total von 17.30.
    assert result.total_chf == Decimal("18.70")


def test_a_real_price_change_still_names_the_affected_position():
    """Kein Aufweichen: eine echte Abweichung blockiert weiterhin exakt."""
    moved = BASTELGARAGE_CART_HTML.replace(
        '<td class="text-right">CHF6.90</td>\n    <td class="text-right">CHF6.90</td>',
        '<td class="text-right">CHF7.40</td>\n    <td class="text-right">CHF7.40</td>',
    )
    cart, _ = adapter(
        pages={BG_DUPONT: PRODUCT_HTML, BG_BREADBOARD: BREADBOARD_HTML},
        cart_page=moved,
    )

    with pytest.raises(CartVerificationError) as error:
        cart.fill(
            SHOP_URL,
            [
                item(BG_DUPONT, offer_id=31, menge=2, preis="5.90",
                     name="Dupont Jumper Cable Set 10cm 60 pieces."),
                item(BG_BREADBOARD, offer_id=32, menge=1, preis="6.90",
                     name="Half-Size Transparent Breadboard ZY-60", line_id=11),
            ],
        )

    message = str(error.value)
    # Nennt die betroffene Position, nicht nur eine Summe.
    assert "Half-Size Transparent Breadboard ZY-60: erfasst CHF 6.90, Korb CHF 7.40." in message
    assert "Dupont" not in message


# ---------------------------------------------------------------------------
# Füllen und Rückverifikation
# ---------------------------------------------------------------------------

def test_fill_adds_every_position_and_hands_over_the_verified_session():
    cart, session = adapter(
        pages={DUPONT_URL: PRODUCT_HTML, BREADBOARD_URL: BREADBOARD_HTML},
        cart_page=cart_html(DUPONT_ROW + BREADBOARD_ROW, "CHF 18.70"),
    )

    result = cart.fill(
        SHOP_URL,
        [item(), item(BREADBOARD_URL, offer_id=32, menge=1, preis="6.90",
                     name="Breadboard Half Size", line_id=11)],
    )

    assert result.verifiziert is True
    assert result.plattform == PLATFORM_OPENCART
    assert result.artikel_anzahl == 3
    assert result.total_chf == Decimal("18.70")
    assert result.cookie_name == "OCSESSID"
    assert result.cookie_wert == "sess-abc-123"
    assert result.produkt_ids == {31: "96", 32: "412"}
    assert [data["product_id"] for _, data in session.posted] == ["96", "412"]
    assert [data["quantity"] for _, data in session.posted] == [2, 1]


def test_cached_product_id_skips_the_lookup_but_still_pins_the_language():
    """Vorfall 2026-08-11: bei warmem Cache wurde gar keine Produktseite mehr
    besucht, der Shop rendert den Korb dann in seiner Default-Sprache und jede
    Position gilt als fehlend. Ein Abruf einer erfassten URL stellt den
    Sprachkontext her - die ID kommt weiterhin aus dem Cache."""
    cart, session = adapter(
        pages={DUPONT_URL: PRODUCT_HTML},
        cart_page=cart_html(DUPONT_ROW, "CHF 11.80"),
    )

    result = cart.fill(SHOP_URL, [item(shop_produkt_id="96")])

    assert result.verifiziert is True
    assert result.produkt_ids == {}, "ID kam aus dem Cache, nichts nachzutragen"
    assert DUPONT_URL in session.fetched, "Sprachkontext wurde nicht gesetzt"
    # Genau einmal - kein Abruf pro Position.
    assert session.fetched.count(DUPONT_URL) == 1


def test_a_cold_cache_does_not_fetch_the_first_page_twice():
    cart, session = adapter(
        pages={DUPONT_URL: PRODUCT_HTML},
        cart_page=cart_html(DUPONT_ROW, "CHF 11.80"),
    )

    cart.fill(SHOP_URL, [item()])

    # Die Auflösung hat den Kontext schon gesetzt, ein zweiter Abruf wäre unnötig.
    assert session.fetched.count(DUPONT_URL) == 1


def test_a_cart_rendered_in_another_language_is_blocked_not_guessed():
    """Die echte Fehlantwort des Vorfalls: gleiche Produkte, fremde Slugs.

    Der strikte URL-Vergleich bleibt - lieber blockieren als eine Position
    über Namensähnlichkeit erraten.
    """
    entries, _ = parse_opencart_cart(GERMAN_CART_HTML)
    hrefs = [entry.href for entry in entries]

    assert all("lochraster" in href or "jumperkabel" in href for href in hrefs)
    assert not any("breadboard-hole-grid" in href or "dupont-jumper-cable" in href for href in hrefs)

    cart, _ = adapter(
        pages={BG_DUPONT: PRODUCT_HTML, BG_BREADBOARD: BREADBOARD_HTML},
        cart_page=GERMAN_CART_HTML,
    )

    with pytest.raises(CartVerificationError) as error:
        cart.fill(
            SHOP_URL,
            [
                item(BG_DUPONT, offer_id=31, menge=1, preis="5.90", name="Dupont Jumper Cable Set"),
                item(BG_BREADBOARD, offer_id=32, menge=1, preis="4.90",
                     name="Breadboard Half-Size", line_id=11),
            ],
        )

    message = str(error.value)
    assert "Position fehlt im Korb: Dupont Jumper Cable Set." in message
    assert "Position fehlt im Korb: Breadboard Half-Size." in message
    # Artikelzahl stimmt - genau deshalb sah der Vorfall nach "leerer Korb" aus,
    # war aber ein voller Korb mit fremdsprachigen Links.
    assert "Artikelzahl weicht ab" not in message


def test_changed_shop_price_blocks_handover_with_an_exact_diff():
    cart, _ = adapter(
        pages={DUPONT_URL: PRODUCT_HTML},
        cart_page=cart_html(
            cart_row(DUPONT_URL, "Dupont Jumper Cable Set 10cm", 2, "CHF 6.10", "CHF 12.20", 42),
            "CHF 12.20",
        ),
    )

    with pytest.raises(CartVerificationError) as error:
        cart.fill(SHOP_URL, [item()])

    message = str(error.value)
    assert "erfasst CHF 11.80" in message
    assert "Korb CHF 12.20" in message


def test_missing_position_blocks_handover_and_names_it():
    cart, _ = adapter(
        pages={DUPONT_URL: PRODUCT_HTML, BREADBOARD_URL: BREADBOARD_HTML},
        cart_page=cart_html(DUPONT_ROW, "CHF 11.80"),
    )

    with pytest.raises(CartVerificationError) as error:
        cart.fill(
            SHOP_URL,
            [item(), item(BREADBOARD_URL, offer_id=32, menge=1, preis="6.90",
                          name="Breadboard Half Size", line_id=11)],
        )

    message = str(error.value)
    assert "Position fehlt im Korb: Breadboard Half Size" in message
    assert "Artikelzahl weicht ab: erfasst 3, Korb 2" in message


def test_quantity_mismatch_is_reported_per_position():
    cart, _ = adapter(
        pages={DUPONT_URL: PRODUCT_HTML},
        cart_page=cart_html(
            cart_row(DUPONT_URL, "Dupont Jumper Cable Set 10cm", 1, "CHF 5.90", "CHF 5.90", 42),
            "CHF 5.90",
        ),
    )

    with pytest.raises(CartVerificationError) as error:
        cart.fill(SHOP_URL, [item()])

    assert "erfasst 2 Stück, Korb 1 Stück" in str(error.value)


def test_product_with_required_options_is_reported_in_plain_text():
    cart, _ = adapter(
        pages={DUPONT_URL: PRODUCT_HTML},
        add_responses=[
            FakeResponse(payload={"error": {"option": "Bitte Farbe auswählen!"}})
        ],
        cart_page=cart_html("", "CHF 0.00"),
    )

    with pytest.raises(CartError) as error:
        cart.fill(SHOP_URL, [item()])

    message = str(error.value)
    assert "verlangt eine Auswahl im Shop" in message
    assert "Bitte Farbe auswählen!" in message


def test_shop_refusing_the_add_is_reported_without_a_cart():
    cart, _ = adapter(
        pages={DUPONT_URL: PRODUCT_HTML},
        add_responses=[FakeResponse(payload={"error": {"warning": "Nicht verfügbar"}})],
        cart_page=cart_html("", "CHF 0.00"),
    )

    with pytest.raises(CartError) as error:
        cart.fill(SHOP_URL, [item()])

    assert "Nicht verfügbar" in str(error.value)


# ---------------------------------------------------------------------------
# Gast-Session eröffnen und dabei erkennen
# ---------------------------------------------------------------------------

def test_starting_a_session_reports_the_completed_detection():
    session = FakeSession(pages={SHOP_URL: HOME_HTML})

    evidence = start_guest_session(session, SHOP_URL)

    assert evidence is not None
    assert evidence.plattform == PLATFORM_OPENCART


def test_a_shop_without_known_markers_is_a_completed_negative_result():
    session = FakeSession(
        pages={SHOP_URL: "<html><body>Ein Shop ohne Merkmale</body></html>"},
        cookies={"PHPSESSID": "x"},
    )

    assert start_guest_session(session, SHOP_URL) is None


def test_a_timeout_is_not_a_detection_result():
    """Ein Netzwerkhänger darf nie als 'unbekannte Plattform' durchgehen.

    Sonst würde ein einziger Timeout einen unterstützten Shop dauerhaft
    stummschalten. Der Aufrufer muss diesen Fall vom echten Negativergebnis
    unterscheiden können, deshalb eine eigene Fehlerklasse.
    """

    class TimingOutSession(FakeSession):
        def get(self, url):
            raise TimeoutError("read timeout")

    with pytest.raises(CartTemporaryError) as error:
        start_guest_session(TimingOutSession(), SHOP_URL)

    assert "wiederholen" in str(error.value)
    assert not isinstance(error.value, CartUnsupported)


def test_an_error_response_is_also_no_detection_result():
    session = FakeSession(pages={SHOP_URL: FakeResponse("Wartung", status_code=503)})

    with pytest.raises(CartTemporaryError) as error:
        start_guest_session(session, SHOP_URL)

    assert "503" in str(error.value)


# ---------------------------------------------------------------------------
# Vertraulichkeit
# ---------------------------------------------------------------------------

def test_no_session_cookie_leaks_into_any_failure_message():
    secret = "sess-abc-123"
    cart, _ = adapter(
        pages={DUPONT_URL: PRODUCT_HTML},
        cart_page=cart_html(
            cart_row(DUPONT_URL, "Dupont Jumper Cable Set 10cm", 2, "CHF 6.10", "CHF 12.20", 42),
            "CHF 12.20",
        ),
    )

    with pytest.raises(CartVerificationError) as error:
        cart.fill(SHOP_URL, [item()])

    assert secret not in str(error.value)
    assert all(secret not in line for line in error.value.abweichungen)


def test_empty_selection_for_a_shop_is_rejected_before_any_request():
    cart, session = adapter()

    with pytest.raises(CartError):
        cart.fill(SHOP_URL, [])

    assert session.fetched == []
    assert session.posted == []

"""Finding a product from the way a supplier's invoice writes its name.

A distributor writes `PARACETAMOL TAB`, `VOLINI GEL 30GM`, `AMOXY 500 CAP
10'S`. None of that is what the master holds, and the matcher's job is to
offer the right rows *without deciding between them* — a supplier's
abbreviation is not evidence enough to bind money to a product.
"""

from decimal import Decimal

import pytest

from app.services import matching
from tests.conftest import API, make_medicine


def match(client, headers, q, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{API}/medicines/match?q={q}" + (f"&{query}" if query else "")
    response = client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def names(body):
    return [m["medicine"]["brand_name"] for m in body["matches"]]


# --- reading the supplier's text -----------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("PARACETAMOL TAB", ["PARACETAMOL"]),
        ("PARACETAMOL TABS", ["PARACETAMOL"]),
        ("COUGH SYP 100ML", ["COUGH", "100ML"]),
        ("AMOXY 500 CAP", ["AMOXY", "500"]),
        ("VOLINI-GEL 30GM", ["VOLINI", "GEL", "30GM"]),
        # A line that is only a form word still has to search for something.
        ("TABLET", ["TABLET"]),
    ],
)
def test_form_words_come_off_the_search_terms(text, expected):
    assert matching.tokenise(text) == expected


@pytest.mark.parametrize(
    ("text", "size", "unit"),
    [
        ("VOLINI GEL 30GM", Decimal("30"), "GM"),
        ("VOLINI GEL 10 GM", Decimal("10"), "GM"),
        ("COUGH SYP 100ML", Decimal("100"), "ML"),
        ("PARACETAMOL 15'S", Decimal("15"), "COUNT"),
        ("PARACETAMOL 15 NOS", Decimal("15"), "COUNT"),
    ],
)
def test_a_pack_size_is_read_off_the_line(text, size, unit):
    hint = matching.read_pack(text)
    assert hint is not None
    assert hint.size == size
    assert hint.unit == unit


def test_the_last_size_wins_because_it_is_the_pack():
    """`AMOXY 500 CAP 10'S` — 500 is the strength, 10 is the pack."""
    hint = matching.read_pack("AMOXY 500MG CAP 10'S")
    assert hint.size == Decimal("10")
    assert hint.unit == "COUNT"


def test_a_line_with_no_size_reports_none():
    assert matching.read_pack("PARACETAMOL TAB") is None


@pytest.mark.parametrize("text", ["PARACETAMOL 500 TAB", "AMOXY 250 CAP", "PARA 650MG TAB"])
def test_a_strength_before_a_form_word_is_not_a_pack_count(text):
    """"PARACETAMOL 500 TAB" is a 500 mg tablet, not a pack of five hundred.

    Reading it as a count would report the product missing from the master
    and offer to add a 500-count pack. A real strip count is written `15'S`.
    """
    assert matching.read_pack(text) is None


# --- matching against the master -----------------------------------------


def test_a_trailing_form_word_still_finds_the_product(client, admin_headers):
    make_medicine(client, admin_headers, brand_name="Paracetamol 500", pack_size="10")

    assert names(match(client, admin_headers, "PARACETAMOL%20TAB")) == ["Paracetamol 500"]
    assert names(match(client, admin_headers, "PARACETAMOL%20TABS")) == ["Paracetamol 500"]


def test_word_order_now_matters_unlike_the_old_token_matcher(client, admin_headers):
    """A deliberate trade-off, not a regression: matching is now a shrinking
    *prefix* of the name, read the same direction the name is — so reordered
    words no longer find a product the way they used to under the old
    any-order token matcher. "VOLINI GEL" (below) still works because that
    is how the brand is actually written; "GEL VOLINI" is not."""
    make_medicine(client, admin_headers, brand_name="Volini Gel", dosage_form="OINTMENT",
                  pack_size="30", pack_unit="GM")

    assert names(match(client, admin_headers, "GEL%20VOLINI")) == []
    assert names(match(client, admin_headers, "VOLINI%20GEL")) == ["Volini Gel"]


def test_a_typoed_trailing_word_backs_off_to_the_prefix_that_matches(client, admin_headers):
    """"P 120 Syruop" (typo) -> nothing starts with that -> back off one
    word to "P 120 Syrup" -> still nothing exact -> back off to "P 120",
    which is where every "P 120 Syrup..." on the master starts."""
    make_medicine(client, admin_headers, brand_name="P 120 Syrup", dosage_form="SYRUP",
                  pack_size="100", pack_unit="ML")
    make_medicine(client, admin_headers, brand_name="P 120 Syrup Sugar Free",
                  dosage_form="SYRUP", pack_size="200", pack_unit="ML",
                  composition_text="Paracetamol 120mg/5ml")

    assert set(names(match(client, admin_headers, "P%20120%20Syruop"))) == {
        "P 120 Syrup", "P 120 Syrup Sugar Free",
    }


def test_backing_off_stops_at_the_first_prefix_that_matches(client, admin_headers):
    """"T Minic Drops Af" -> nothing -> "T Minic Drops" -> matches, so it
    stops there rather than backing off further to "T Minic" or "T"."""
    make_medicine(client, admin_headers, brand_name="T Minic Drops", dosage_form="DROPS",
                  pack_size="15", pack_unit="ML")
    # Would also match a looser "T" or "T Minic" prefix -- must not appear,
    # proving the search stopped at "T Minic Drops" rather than backing off
    # all the way and returning everything that shares a first word.
    make_medicine(client, admin_headers, brand_name="T Minic Plus", dosage_form="SYRUP",
                  pack_size="60", pack_unit="ML", composition_text="Chlorpheniramine 2mg")
    make_medicine(client, admin_headers, brand_name="Tussivon Cough Syrup", dosage_form="SYRUP",
                  pack_size="100", pack_unit="ML", composition_text="Dextromethorphan 10mg")

    assert names(match(client, admin_headers, "T%20Minic%20Drops%20Af")) == ["T Minic Drops"]


def test_a_bare_single_letter_still_searches(client, admin_headers):
    """"P" alone is noise for scoring purposes (tokenise() drops it), but
    it is still a real, if blunt, invoice line -- every "P ..." brand is
    exactly what backing off all the way to one letter means."""
    make_medicine(client, admin_headers, brand_name="P 120 Suspension",
                  composition_text="Paracetamol 120mg/5ml")
    make_medicine(client, admin_headers, brand_name="Volini Gel", dosage_form="OINTMENT",
                  pack_size="30", pack_unit="GM")

    assert names(match(client, admin_headers, "P")) == ["P 120 Suspension"]


def test_the_search_is_case_insensitive(client, admin_headers):
    make_medicine(client, admin_headers, brand_name="P 120 Syrup", dosage_form="SYRUP",
                  pack_size="100", pack_unit="ML")

    assert names(match(client, admin_headers, "p%20120%20syrup")) == ["P 120 Syrup"]
    assert names(match(client, admin_headers, "P%20120%20SYRUP")) == ["P 120 Syrup"]


def test_punctuation_does_not_hide_a_product(client, admin_headers):
    make_medicine(client, admin_headers, brand_name="Volini Gel", dosage_form="OINTMENT",
                  pack_size="30", pack_unit="GM")

    assert names(match(client, admin_headers, "VOLINI-GEL")) == ["Volini Gel"]


def test_nothing_is_chosen_even_when_one_row_matches(client, admin_headers):
    """The whole contract: it returns candidates, it does not decide."""
    make_medicine(client, admin_headers, brand_name="Paracetamol 500")

    body = match(client, admin_headers, "PARACETAMOL%20TAB")
    assert len(body["matches"]) == 1
    # No "selected", no "auto", no id lifted out of the list — the client has
    # to show the row and have somebody click it.
    assert set(body) == {
        "query", "terms", "pack_hint", "form_hint", "matches", "has_pack_match", "has_form_match",
    }


# --- pack size is part of the identity ------------------------------------


def _two_volinis(client, headers):
    small = make_medicine(
        client, headers, brand_name="Volini Gel", dosage_form="OINTMENT",
        pack_size="10", pack_unit="GM",
    )
    big = make_medicine(
        client, headers, brand_name="Volini Gel", dosage_form="OINTMENT",
        pack_size="30", pack_unit="GM",
    )
    return small, big


def test_two_sizes_of_one_brand_are_two_products(client, admin_headers):
    small, big = _two_volinis(client, admin_headers)
    assert small["id"] != big["id"]
    assert small["pack_label"] != big["pack_label"]


def test_the_asked_for_size_is_flagged_as_the_match(client, admin_headers):
    _two_volinis(client, admin_headers)

    body = match(client, admin_headers, "VOLINI%20GEL%2030GM")

    assert body["has_pack_match"] is True
    by_pack = {m["medicine"]["pack_label"]: m for m in body["matches"]}
    thirty = next(m for label, m in by_pack.items() if "30" in label)
    ten = next(m for label, m in by_pack.items() if "10" in label)

    assert thirty["pack_matches"] is True
    # The other size is still offered — it is a real product and the invoice
    # may have been written loosely — but it is marked as a different pack.
    assert ten["pack_matches"] is False
    assert "different pack" in ten["note"]
    # And the one actually asked for sorts first.
    assert body["matches"][0]["pack_matches"] is True


def test_a_size_not_on_the_master_is_reported_as_missing(client, admin_headers):
    """The Volini 20GM case: the brand exists, this pack does not."""
    _two_volinis(client, admin_headers)

    body = match(client, admin_headers, "VOLINI%20GEL%2020GM")

    assert body["pack_hint"]["text"] == "20GM"
    # Candidates are offered, but none of them is that pack — which is the
    # signal to ask the operator to add it rather than accepting a near miss.
    assert body["matches"]
    assert body["has_pack_match"] is False
    assert all(m["pack_matches"] is False for m in body["matches"])


def test_a_strip_count_matches_units_per_pack(client, admin_headers):
    """"15'S" on an invoice means fifteen tablets in the pack."""
    make_medicine(client, admin_headers, brand_name="Paracetamol 500", pack_size="15")
    make_medicine(client, admin_headers, brand_name="Paracetamol 500", pack_size="10")

    body = match(client, admin_headers, "PARACETAMOL%20500%20TAB%2015'S")

    assert body["has_pack_match"] is True
    assert body["matches"][0]["medicine"]["units_per_pack"] == 15


# --- ranking: a shrinking prefix of the name, then the named form --------


def test_a_brand_only_matching_later_is_not_offered_at_all(client, admin_headers):
    """"PARACETAMOL" is a name search, not a substring search: a brand
    where the word only appears later ("Acefirst Paracetamol") is not a
    prefix match and is not offered, even though a plain substring search
    would have found it."""
    make_medicine(client, admin_headers, brand_name="Acefirst Paracetamol",
                  composition_text="Paracetamol 500mg")
    make_medicine(client, admin_headers, brand_name="Paracetamol Plus",
                  composition_text="Paracetamol 250mg")

    assert names(match(client, admin_headers, "PARACETAMOL")) == ["Paracetamol Plus"]


def test_two_brands_starting_the_same_way_sort_alphabetically(client, admin_headers):
    make_medicine(client, admin_headers, brand_name="Paracetamol Forte",
                  composition_text="Paracetamol 650mg")
    make_medicine(client, admin_headers, brand_name="Paracetamol Extra",
                  composition_text="Paracetamol 750mg")

    assert names(match(client, admin_headers, "PARACETAMOL")) == [
        "Paracetamol Extra", "Paracetamol Forte",
    ]


def test_a_form_word_prefers_the_matching_dosage_form(client, admin_headers):
    """"PARACETAMOL SYP" should rank the syrup ahead of the tablet, even
    though both share a brand name and otherwise match equally well."""
    make_medicine(client, admin_headers, brand_name="Paracetamol", dosage_form="TABLET",
                  pack_size="10", composition_text="Paracetamol 500mg")
    make_medicine(client, admin_headers, brand_name="Paracetamol", dosage_form="SYRUP",
                  pack_size="100", pack_unit="ML", composition_text="Paracetamol 500mg")

    body = match(client, admin_headers, "PARACETAMOL%20SYP")

    assert body["form_hint"]["dosage_form"] == "SYRUP"
    assert body["has_form_match"] is True
    assert body["matches"][0]["medicine"]["dosage_form"] == "SYRUP"
    assert body["matches"][0]["form_matches"] is True
    assert body["matches"][1]["form_matches"] is False
    assert "different form" in body["matches"][1]["note"]


def test_a_form_not_on_the_master_is_reported_as_missing(client, admin_headers):
    make_medicine(client, admin_headers, brand_name="Paracetamol", dosage_form="TABLET",
                  pack_size="10", composition_text="Paracetamol 500mg")

    body = match(client, admin_headers, "PARACETAMOL%20SYP")

    assert body["form_hint"]["dosage_form"] == "SYRUP"
    assert body["has_form_match"] is False
    assert body["matches"][0]["form_matches"] is False


def test_a_line_with_no_form_word_reports_no_form_hint(client, admin_headers):
    assert match(client, admin_headers, "PARACETAMOL")["form_hint"] is None


def test_a_wanted_row_survives_a_catalogue_with_hundreds_of_near_matches(
    client, admin_headers
):
    """A catalogue built from a full distributor price list can have
    hundreds of rows sharing one generic's name — every manufacturer's own
    Paracetamol, say — comfortably past the pre-ranking database limit.
    Which of them Postgres hands back for that cap to keep must not be
    arbitrary: the row created *last* (so it cannot survive by coincidence
    of insertion order) still has to come back for an unambiguous query.
    """
    for i in range(60):
        make_medicine(
            client, admin_headers,
            brand_name=f"Company{i} Paracetamol Combo",
            composition_text="Paracetamol 500mg",
            manufacturer_name=f"Manufacturer {i}",
        )
    make_medicine(client, admin_headers, brand_name="Paracetamol 500",
                  composition_text="Paracetamol 250mg")

    assert "Paracetamol 500" in names(match(client, admin_headers, "PARACETAMOL"))


def test_a_line_matching_nothing_comes_back_empty(client, admin_headers):
    make_medicine(client, admin_headers, brand_name="Paracetamol 500")

    body = match(client, admin_headers, "ZZZQQQ%20TAB")
    assert body["matches"] == []
    assert body["has_pack_match"] is False


def test_the_search_can_be_scoped_to_one_kind(client, admin_headers):
    from tests.test_general_items import make_general

    make_medicine(client, admin_headers, brand_name="Soapolol")
    make_general(client, admin_headers, brand_name="Soap Bar")

    both = match(client, admin_headers, "SOAP")
    general = match(client, admin_headers, "SOAP", item_type="GENERAL")

    assert len(both["matches"]) == 2
    assert names(general) == ["Soap Bar"]

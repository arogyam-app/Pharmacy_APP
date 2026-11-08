"""Catalogue: canonicalisation, brand-or-generic search, alternatives, import.

The two behaviours the shop actually asked for are the search box that
accepts either name, and the same-salt alternatives panel. Both hang off
composition canonicalisation, so that is tested hardest.
"""

import io
from decimal import Decimal

import pytest

from app.models.catalog import Composition, Medicine
from app.services import catalog as svc
from tests.conftest import API, MISSING_ID, make_batch, make_medicine


# --- canonicalisation ----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Paracetamol 500mg", "PARACETAMOL (500MG)"),
        ("paracetamol 500 mg", "PARACETAMOL (500MG)"),
        ("  Paracetamol   500MG  ", "PARACETAMOL (500MG)"),
        # Already bracketed: canonical text must re-parse to itself, or a
        # screen that reads a formulation and saves it again forks the row.
        ("Paracetamol (500mg)", "PARACETAMOL (500MG)"),
        ("PARACETAMOL ( 500 MG )", "PARACETAMOL (500MG)"),
        ("Amoxicillin 500mg + Clavulanic Acid 125mg",
         "AMOXICILLIN (500MG) + CLAVULANIC ACID (125MG)"),
        # Ingredient order must not matter.
        ("Clavulanic Acid 125mg + Amoxicillin 500mg",
         "AMOXICILLIN (500MG) + CLAVULANIC ACID (125MG)"),
        ("Povidone Iodine 5%", "POVIDONE IODINE (5%)"),
        ("Ceftriaxone 1g", "CEFTRIAXONE (1G)"),
        # "1GM" is how a distributor writes a gram, and `g\b` cannot match it.
        ("Ceftriaxone 1gm + Sulbactam 0.5gm", "CEFTRIAXONE (1GM) + SULBACTAM (0.5GM)"),
        # A gram outranks 500 milligrams, which a bare number comparison
        # would get backwards.
        ("Sulbactam 500mg + Ceftriaxone 1gm", "CEFTRIAXONE (1GM) + SULBACTAM (500MG)"),
        # Milligrams and per-cent do not compare, so this one is alphabetical.
        ("Zeta 500mg + Alpha 1% w/v", "ALPHA (1% W/V) + ZETA (500MG)"),
        ("Vitamin B12 1000mcg", "VITAMIN B12 (1000MCG)"),
        # A bare number is not a strength: inventing the unit would be worse
        # than leaving it where the operator put it.
        ("Cabergolin 0.5", "CABERGOLIN 0.5"),
        ("Human Insulin 40IU", "HUMAN INSULIN (40IU)"),
        ("Vitamin B12", "VITAMIN B12"),
        # The two shapes from the brief. The strongest ingredient leads, so
        # guaifenesin comes off the front of the first one.
        (
            "Ambroxol 7.5mg/ml + Guaifenesin 12.5mg/ml + Levosalbutamol 0.25mg/ml",
            "GUAIFENESIN (12.5MG/ML) + AMBROXOL (7.5MG/ML) + LEVOSALBUTAMOL (0.25MG/ML)",
        ),
        (
            "Atropine 1% w/v + Chloramphenicol 0.5% w/v + Dexamethasone 0.1% w/v",
            "ATROPINE (1% W/V) + CHLORAMPHENICOL (0.5% W/V) + DEXAMETHASONE (0.1% W/V)",
        ),
        # A qualifier keeps the space that makes it readable; the gaps inside
        # the dose itself close up.
        ("Atropine 1 % w/v", "ATROPINE (1% W/V)"),
        ("Atropine ( 1 % w / v )", "ATROPINE (1% W/V)"),
        ("Ambroxol ( 7.5 mg / ml )", "AMBROXOL (7.5MG/ML)"),
        # A salt name is often more than one word, and its spaces are real.
        ("Chlorpheniramine Maleate (4mg)", "CHLORPHENIRAMINE MALEATE (4MG)"),
        ("CHLORPHENIRAMINE MALEATE ( 4 M G )", "CHLORPHENIRAMINE MALEATE (4MG)"),
    ],
)
def test_composition_text_is_canonicalised(text, expected):
    assert svc.canonical_text(svc.parse_composition_text(text)) == expected


@pytest.mark.parametrize(
    "typed",
    [
        "ACECLOFENAC   ( 100  M G  )  + PARACETAMOL (  325  MG ) ",
        "ACECLOFENAC (100MG    ) +    PARACETAMOL (325 MG)",
        # No spaces around the brackets or the plus at all.
        "Aceclofenac(100mg)+Paracetamol(325mg)",
        # Closing bracket never typed — a truncated paste, or a half-finished
        # line. It is not ambiguous about what was meant.
        "Aceclofenac  ( 100  M G  )  + Paracetamol (  325  MG ",
        "Aceclofenac 100mg + Paracetamol 325mg",
        "aceclofenac    100   M G+paracetamol 325MG",
        # Order is not part of the identity either.
        "Paracetamol (325mg) + Aceclofenac (100mg)",
    ],
)
def test_every_way_of_typing_one_formulation_lands_on_the_same_text(typed):
    """Spacing, case, brackets and order are all noise around the same fact.

    Each of these has to canonicalise identically, because two spellings that
    do not would become two compositions — and then the brands using them
    would never be offered as alternatives to each other.
    """
    assert (
        svc.canonical_text(svc.parse_composition_text(typed))
        == "PARACETAMOL (325MG) + ACECLOFENAC (100MG)"
    )


def test_the_messy_spellings_resolve_to_a_single_composition(client, admin_headers, db):
    """The same thing again, end to end: one row, not six."""
    for index, typed in enumerate(
        [
            "ACECLOFENAC   ( 100  M G  )  + PARACETAMOL (  325  MG ) ",
            "ACECLOFENAC (100MG    ) +    PARACETAMOL (325 MG)",
            "Aceclofenac  ( 100  M G  )  + Paracetamol (  325  MG ",
        ]
    ):
        make_medicine(
            client, admin_headers, brand_name=f"Painkillol {index}", composition_text=typed,
        )

    assert db.query(Composition).count() == 1
    assert db.query(Composition).one().composition_text == (
        "PARACETAMOL (325MG) + ACECLOFENAC (100MG)"
    )


def test_reordered_ingredients_resolve_to_one_composition(client, admin_headers, db):
    a = make_medicine(
        client, admin_headers,
        brand_name="Combi A", composition_text="Ibuprofen 400mg + Paracetamol 325mg",
    )
    b = make_medicine(
        client, admin_headers,
        brand_name="Combi B", composition_text="paracetamol 325 mg + ibuprofen 400 mg",
    )

    assert a["composition_id"] == b["composition_id"]
    assert db.query(Composition).count() == 1
    assert a["composition_text"] == "IBUPROFEN (400MG) + PARACETAMOL (325MG)"


def test_a_percentage_strength_is_split_from_the_salt_name(client, admin_headers, db):
    """`%` has no word boundary after it, so the old parser never matched one.

    Every ophthalmic drop and topical cream is dosed that way, and the
    strength used to end up glued onto the salt name — which meant two
    strengths of the same drug looked like two different generics and were
    never offered as alternatives to each other.
    """
    medicine = make_medicine(
        client, admin_headers, brand_name="Dropsol", composition_text="Atropine 1% w/v",
    )
    assert medicine["composition_text"] == "ATROPINE (1% W/V)"

    composition = db.query(Composition).filter(
        Composition.composition_text == "ATROPINE (1% W/V)"
    ).one()
    component = composition.components[0]
    assert component.salt.name == "ATROPINE"
    assert component.strength == "1% W/V"


def test_a_formulation_stored_before_brackets_still_matches_one_typed_today(
    client, admin_headers, db
):
    """The search key ignores brackets, so the two formats meet in the middle."""
    make_medicine(
        client, admin_headers, brand_name="Oldstyle", composition_text="Paracetamol 500mg",
    )
    # Simulate a row written by the previous release.
    stored = db.query(Composition).one()
    stored.composition_text = "PARACETAMOL 500MG"
    stored.search_key = svc.composition_search_key("PARACETAMOL 500MG")
    db.commit()

    body = client.get(
        f"{API}/compositions/similar", params={"text": "Paracetamol (500mg)"},
        headers=admin_headers,
    ).json()
    found = [c["composition_text"] for c in body["similar"]]
    if body["exact"]:
        found.append(body["exact"]["composition_text"])
    assert "PARACETAMOL 500MG" in found


@pytest.mark.parametrize(
    ("stored", "typed"),
    [
        ("Paracetamol 500mg", "paracetamol 500 mg"),   # extra space before the unit
        ("Paracetamol 500mg", "PARACETAMOL500MG"),      # no spaces at all
        ("Paracetamol 500mg", "  Paracetamol  500MG "), # stray padding
        ("Paracetamol 500mg", "paracetamol"),           # partial, salt only
        ("Amoxicillin 500mg + Clavulanic Acid 125mg", "clavulanic acid"),
        ("Amoxicillin 500mg + Clavulanic Acid 125mg", "amoxicillin 500 mg"),
    ],
)
def test_similar_compositions_ignore_spacing_and_case(client, admin_headers, stored, typed):
    """Typing a near-miss must surface the existing row, or the operator
    creates a duplicate and silently splits a set of alternatives in two."""
    make_medicine(client, admin_headers, composition_text=stored)

    body = client.get(f"{API}/compositions/similar?text={typed}", headers=admin_headers).json()

    found = [c["composition_text"] for c in body["similar"]]
    if body["exact"]:
        found.append(body["exact"]["composition_text"])
    assert svc.canonical_text(svc.parse_composition_text(stored)) in found


def test_an_exact_match_is_reported_separately(client, admin_headers):
    make_medicine(client, admin_headers, composition_text="Paracetamol 500mg")

    body = client.get(
        f"{API}/compositions/similar?text=paracetamol   500 MG", headers=admin_headers
    ).json()

    assert body["exact"] is not None
    assert body["exact"]["composition_text"] == "PARACETAMOL (500MG)"
    assert body["exact"]["medicine_count"] == 1


def test_similar_compositions_returns_nothing_for_an_unknown_salt(client, admin_headers):
    make_medicine(client, admin_headers, composition_text="Paracetamol 500mg")

    body = client.get(
        f"{API}/compositions/similar?text=Rifampicin 450mg", headers=admin_headers
    ).json()

    assert body["exact"] is None
    assert body["similar"] == []


def test_composition_search_ignores_spacing(client, admin_headers):
    make_medicine(client, admin_headers, composition_text="Paracetamol 500mg")

    spaced = client.get(f"{API}/compositions?q=paracetamol 500 mg", headers=admin_headers).json()
    packed = client.get(f"{API}/compositions?q=PARACETAMOL500MG", headers=admin_headers).json()

    assert spaced["total"] == 1
    assert packed["total"] == 1


def test_blank_composition_is_rejected(client, admin_headers):
    response = client.post(
        f"{API}/medicines",
        json={"brand_name": "Nameless", "manufacturer_name": "X Labs", "composition_text": "   "},
        headers=admin_headers,
    )
    assert response.status_code in (400, 422)


# --- search --------------------------------------------------------------


@pytest.fixture
def catalogue(client, admin_headers):
    """Three brands of one salt at three prices, plus an unrelated medicine.

    Prices live on batches now. Per-unit prices are deliberately not in brand
    order, and pack sizes differ, so per-pack comparison would mislead:
        Crocin  30.00 / 15 = 2.0000
        Calpol  27.00 / 15 = 1.8000
        Pacimol 14.00 / 10 = 1.4000  <- cheapest per tablet
    """
    catalogue = {
        "crocin": make_medicine(
            client, admin_headers, brand_name="Crocin 500",
            manufacturer_name="GlaxoSmithKline", composition_text="Paracetamol 500mg",
            therapeutic_class="Analgesic", pack_size="15",
        ),
        "calpol": make_medicine(
            client, admin_headers, brand_name="Calpol 500",
            manufacturer_name="GlaxoSmithKline", composition_text="Paracetamol 500mg",
            pack_size="15",
        ),
        "pacimol": make_medicine(
            client, admin_headers, brand_name="Pacimol 500",
            manufacturer_name="Ipca", composition_text="Paracetamol 500mg", pack_size="10",
        ),
        "augmentin": make_medicine(
            client, admin_headers, brand_name="Augmentin 625",
            manufacturer_name="GlaxoSmithKline",
            composition_text="Amoxicillin 500mg + Clavulanic Acid 125mg",
            schedule_type="SCHEDULE_H", pack_size="10",
        ),
    }
    for key, mrp in (
        ("crocin", "30.00"), ("calpol", "27.00"), ("pacimol", "14.00"), ("augmentin", "223.50")
    ):
        make_batch(client, admin_headers, catalogue[key]["id"], mrp_per_pack=mrp)
    return catalogue


def test_search_finds_a_medicine_by_brand_name(client, sales_headers, catalogue):
    results = client.get(f"{API}/medicines/search?q=crocin", headers=sales_headers).json()

    # The brand asked for leads. What follows are its same-salt substitutes,
    # offered because they are on the shelf — see test_search_ranking.py.
    assert results[0]["brand_name"] == "Crocin 500"
    assert results[0]["is_alternative"] is False
    assert all(r["is_alternative"] for r in results[1:])
    assert "Augmentin 625" not in [r["brand_name"] for r in results]


def test_search_finds_medicines_by_generic_name(client, sales_headers, catalogue):
    """The counter's core requirement: one box, either kind of name."""
    results = client.get(f"{API}/medicines/search?q=paracetamol", headers=sales_headers).json()

    assert {r["brand_name"] for r in results} == {"Crocin 500", "Calpol 500", "Pacimol 500"}


def test_search_matches_one_salt_inside_a_combination(client, sales_headers, catalogue):
    results = client.get(f"{API}/medicines/search?q=clavulanic", headers=sales_headers).json()

    assert [r["brand_name"] for r in results] == ["Augmentin 625"]


def test_search_matches_manufacturer(client, sales_headers, catalogue):
    results = client.get(f"{API}/medicines/search?q=ipca", headers=sales_headers).json()

    assert results[0]["brand_name"] == "Pacimol 500"
    assert results[0]["is_alternative"] is False


def test_search_is_case_and_space_insensitive(client, sales_headers, catalogue):
    upper = client.get(f"{API}/medicines/search?q=PARACETAMOL", headers=sales_headers).json()
    spaced = client.get(f"{API}/medicines/search?q=  paracetamol  ", headers=sales_headers).json()

    assert len(upper) == len(spaced) == 3


def test_search_ranks_brand_matches_first(client, sales_headers, admin_headers, catalogue):
    # A brand whose name is a salt, competing with salt-only matches. It
    # needs stock of its own: since search became stock-aware, an empty shelf
    # outranks any name, and this test is about the name ordering *within*
    # what can actually be handed over.
    brand = make_medicine(
        client, admin_headers, brand_name="Paracetamol IP 500",
        manufacturer_name="Generic Co", composition_text="Paracetamol 500mg",
    )
    make_batch(client, admin_headers, brand["id"], batch_no="PIP1")

    results = client.get(f"{API}/medicines/search?q=paracetamol", headers=sales_headers).json()

    assert results[0]["brand_name"] == "Paracetamol IP 500"


def test_search_excludes_inactive_medicines(client, sales_headers, admin_headers, catalogue):
    client.patch(
        f"{API}/medicines/{catalogue['calpol']['id']}",
        json={"is_active": False},
        headers=admin_headers,
    )
    results = client.get(f"{API}/medicines/search?q=paracetamol", headers=sales_headers).json()

    assert "Calpol 500" not in {r["brand_name"] for r in results}


def test_search_requires_at_least_two_characters(client, sales_headers, catalogue):
    assert client.get(f"{API}/medicines/search?q=p", headers=sales_headers).status_code == 422


def test_search_result_carries_the_pricing_policy(client, sales_headers, catalogue):
    result = client.get(f"{API}/medicines/search?q=crocin", headers=sales_headers).json()[0]

    assert Decimal(result["effective_default_discount_pct"]) == Decimal("10.00")
    assert Decimal(result["effective_max_discount_pct"]) == Decimal("20.00")
    assert result["units_per_pack"] == 15


# --- alternatives --------------------------------------------------------


def test_alternatives_are_the_other_brands_of_the_same_salt(client, sales_headers, catalogue):
    results = client.get(
        f"{API}/medicines/{catalogue['crocin']['id']}/alternatives", headers=sales_headers
    ).json()

    assert {r["brand_name"] for r in results} == {"Calpol 500", "Pacimol 500"}
    # The medicine itself is never offered as its own substitute.
    assert catalogue["crocin"]["id"] not in {r["id"] for r in results}


def test_alternatives_carry_a_price(client, sales_headers, catalogue):
    """The panel exists to offer a cheaper equivalent, so it must show one."""
    results = client.get(
        f"{API}/medicines/{catalogue['crocin']['id']}/alternatives", headers=sales_headers
    ).json()

    pacimol = next(r for r in results if r["brand_name"] == "Pacimol 500")
    assert Decimal(pacimol["mrp_per_pack"]) == Decimal("14.00")
    assert Decimal(pacimol["unit_mrp"]) == Decimal("1.4000")
    assert pacimol["pack_label"] == "10 tab strip"


def test_alternatives_are_ordered_cheapest_per_unit_first(client, sales_headers, catalogue):
    """Pack price alone would mislead: Pacimol's pack is cheapest *and* so is
    its per-tablet price, but a 10-pack vs a 15-pack must be compared per unit."""
    results = client.get(
        f"{API}/medicines/{catalogue['crocin']['id']}/alternatives", headers=sales_headers
    ).json()

    assert [r["brand_name"] for r in results] == ["Pacimol 500", "Calpol 500"]
    assert [Decimal(r["unit_mrp"]) for r in results] == [Decimal("1.4000"), Decimal("1.8000")]


def test_out_of_stock_alternatives_sort_last_rather_than_looking_free(
    client, sales_headers, admin_headers, catalogue
):
    """A substitute with no stock has no price and cannot be handed over, so
    it must never head the list."""
    make_medicine(
        client, admin_headers, brand_name="Nopricil 500", manufacturer_name="Unknown Labs",
        composition_text="Paracetamol 500mg", pack_size="10",  # no batch, no stock
    )
    results = client.get(
        f"{API}/medicines/{catalogue['crocin']['id']}/alternatives", headers=sales_headers
    ).json()

    assert results[-1]["brand_name"] == "Nopricil 500"
    assert results[-1]["unit_mrp"] is None
    assert results[-1]["in_stock"] is False


def test_alternatives_never_cross_compositions(client, sales_headers, catalogue):
    results = client.get(
        f"{API}/medicines/{catalogue['augmentin']['id']}/alternatives", headers=sales_headers
    ).json()

    assert results == []


def test_alternatives_exclude_inactive_brands(client, sales_headers, admin_headers, catalogue):
    client.patch(
        f"{API}/medicines/{catalogue['pacimol']['id']}",
        json={"is_active": False},
        headers=admin_headers,
    )
    results = client.get(
        f"{API}/medicines/{catalogue['crocin']['id']}/alternatives", headers=sales_headers
    ).json()

    assert [r["brand_name"] for r in results] == ["Calpol 500"]


def test_alternatives_for_unknown_medicine_is_404(client, sales_headers, store):
    response = client.get(
        f"{API}/medicines/{MISSING_ID}/alternatives", headers=sales_headers
    )
    assert response.status_code == 404


# --- discount policy resolution -----------------------------------------


def test_the_medicine_master_carries_no_money(client, admin_headers):
    """MRP, GST and discounts moved to the batch: prices change per
    consignment, so the master must not pretend to own them."""
    medicine = make_medicine(client, admin_headers)

    # Accepted on the way in? No — unknown fields are simply not applied.
    assert medicine["mrp_per_pack"] is None
    assert medicine["gst_rate"] is None
    assert medicine["available_units"] == "0.000"
    assert "default_discount_pct" not in medicine
    assert "max_discount_pct" not in medicine


def test_discounts_fall_back_to_the_store_policy_without_a_batch(client, admin_headers):
    medicine = make_medicine(client, admin_headers)

    assert Decimal(medicine["effective_default_discount_pct"]) == Decimal("10.00")
    assert Decimal(medicine["effective_max_discount_pct"]) == Decimal("20.00")


def test_batch_discount_overrides_the_store_policy(client, admin_headers):
    medicine = make_medicine(client, admin_headers)
    make_batch(
        client, admin_headers, medicine["id"],
        default_discount_pct="2.50", max_discount_pct="5.00",
    )

    fetched = client.get(f"{API}/medicines/{medicine['id']}", headers=admin_headers).json()
    assert Decimal(fetched["effective_default_discount_pct"]) == Decimal("2.50")
    assert Decimal(fetched["effective_max_discount_pct"]) == Decimal("5.00")


def test_store_default_is_clamped_to_a_lower_batch_ceiling(client, admin_headers):
    """Store suggests 10 % but this lot is capped at 5 % — offer 5 %, not 10 %."""
    medicine = make_medicine(client, admin_headers)
    make_batch(client, admin_headers, medicine["id"], max_discount_pct="5.00")

    fetched = client.get(f"{API}/medicines/{medicine['id']}", headers=admin_headers).json()
    assert Decimal(fetched["effective_default_discount_pct"]) == Decimal("5.00")
    assert Decimal(fetched["effective_max_discount_pct"]) == Decimal("5.00")


# --- integrity & scoping -------------------------------------------------


def test_duplicate_brand_manufacturer_and_pack_is_rejected(client, admin_headers):
    make_medicine(client, admin_headers)
    response = client.post(
        f"{API}/medicines",
        json={
            "brand_name": "Testolol 50", "manufacturer_name": "Test Labs",
            "composition_text": "Atenolol 50mg", "pack_label": "10 tab strip",
        },
        headers=admin_headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "MEDICINE_EXISTS"


def test_same_brand_in_a_different_pack_size_is_allowed(client, admin_headers):
    make_medicine(client, admin_headers, pack_size="10")
    second = make_medicine(client, admin_headers, pack_size="15")

    assert second["units_per_pack"] == 15
    assert second["pack_label"] == "15 tab strip"


# --- pack measure by dosage form ----------------------------------------


@pytest.mark.parametrize(
    ("dosage_form", "pack_size", "expected_unit", "expected_units", "expected_label"),
    [
        ("TABLET", "10", "TABLET", 10, "10 tab strip"),
        ("TABLET", "15", "TABLET", 15, "15 tab strip"),
        ("CAPSULE", "4", "CAPSULE", 4, "4 cap strip"),
        # Everything below is one sellable container, whatever its size.
        ("SYRUP", "100", "ML", 1, "100 ml bottle"),
        ("DROPS", "10", "ML", 1, "10 ml bottle"),
        ("INJECTION", "10", "ML", 1, "10 ml bottle"),
        ("OINTMENT", "20", "GM", 1, "20 g pack"),
        ("POWDER", "21.8", "GM", 1, "21.8 g pack"),
        ("OTHER", "1", "UNIT", 1, "1 unit"),
    ],
)
def test_pack_measure_follows_the_dosage_form(
    client, admin_headers, dosage_form, pack_size, expected_unit, expected_units, expected_label
):
    medicine = make_medicine(
        client, admin_headers,
        brand_name=f"Packtest {dosage_form} {pack_size}",
        dosage_form=dosage_form,
        pack_size=pack_size,
        pack_unit=None,
    )

    assert medicine["pack_unit"] == expected_unit
    assert medicine["units_per_pack"] == expected_units
    assert medicine["pack_label"] == expected_label
    assert medicine["is_countable"] is (dosage_form in ("TABLET", "CAPSULE"))


def test_only_tablets_and_capsules_are_sold_piece_by_piece(client, admin_headers):
    """A 100 ml syrup is one unit, not 100 — otherwise the POS would happily
    sell a single millilitre out of the bottle."""
    syrup = make_medicine(
        client, admin_headers, brand_name="Syrupy 100", dosage_form="SYRUP", pack_size="100"
    )

    assert syrup["pack_size"] == "100.000"
    assert syrup["units_per_pack"] == 1
    assert syrup["is_countable"] is False


def test_injection_may_be_measured_in_vials(client, admin_headers):
    vial = make_medicine(
        client, admin_headers, brand_name="Monocef 1G", dosage_form="INJECTION",
        pack_size="1", pack_unit="VIAL",
    )

    assert vial["pack_unit"] == "VIAL"
    assert vial["pack_label"] == "1 vial"


def test_a_pack_unit_that_does_not_suit_the_form_is_rejected(client, admin_headers):
    response = client.post(
        f"{API}/medicines",
        json={
            "brand_name": "Wrong Unit", "manufacturer_name": "X Labs",
            "composition_text": "Atenolol 50mg", "dosage_form": "SYRUP",
            "pack_size": "100", "pack_unit": "TABLET",
        },
        headers=admin_headers,
    )

    assert response.status_code == 422
    assert "measured in ML" in response.text


def test_a_strip_cannot_hold_a_fractional_number_of_tablets(client, admin_headers):
    response = client.post(
        f"{API}/medicines",
        json={
            "brand_name": "Half Tab", "manufacturer_name": "X Labs",
            "composition_text": "Atenolol 50mg", "dosage_form": "TABLET", "pack_size": "10.5",
        },
        headers=admin_headers,
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "PACK_INVALID"


def test_switching_form_from_tablet_to_syrup_re_derives_the_pack(client, admin_headers):
    """Leaving units_per_pack at 10 after the switch would let the POS sell a
    tenth of a bottle."""
    medicine = make_medicine(client, admin_headers, pack_size="10")
    assert medicine["units_per_pack"] == 10

    updated = client.patch(
        f"{API}/medicines/{medicine['id']}",
        json={"dosage_form": "SYRUP", "pack_size": "100"},
        headers=admin_headers,
    ).json()

    assert updated["pack_unit"] == "ML"
    assert updated["units_per_pack"] == 1
    assert updated["pack_label"] == "100 ml bottle"


def test_changing_only_the_pack_size_keeps_a_deliberate_unit_choice(client, admin_headers):
    """Editing the size of a vial pack must not silently reset it to ml."""
    medicine = make_medicine(
        client, admin_headers, brand_name="Vialed", dosage_form="INJECTION",
        pack_size="1", pack_unit="VIAL",
    )

    updated = client.patch(
        f"{API}/medicines/{medicine['id']}", json={"pack_size": "2"}, headers=admin_headers
    ).json()

    assert updated["pack_unit"] == "VIAL"
    assert updated["pack_label"] == "2 vials"


def test_unit_price_is_derived_from_pack_price_and_pack_count(client, admin_headers):
    strip = make_medicine(client, admin_headers, pack_size="15")
    strip_batch = make_batch(client, admin_headers, strip["id"], mrp_per_pack="30.24")

    bottle = make_medicine(
        client, admin_headers, brand_name="Bottly", dosage_form="SYRUP", pack_size="100"
    )
    bottle_batch = make_batch(client, admin_headers, bottle["id"], mrp_per_pack="128.00")

    assert Decimal(strip_batch["unit_mrp"]) == Decimal("2.0160")  # per tablet
    # Per bottle, not per ml — a syrup is one sellable unit.
    assert Decimal(bottle_batch["unit_mrp"]) == Decimal("128.0000")


def test_schedule_h_medicines_require_a_prescription_automatically(client, admin_headers):
    medicine = make_medicine(client, admin_headers, schedule_type="SCHEDULE_H1")

    assert medicine["is_prescription_required"] is True


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/medicines", {"brand_name": "X", "manufacturer_name": "Y",
                        "composition_text": "Atenolol 50mg"}),
        ("/salts", {"name": "New Salt"}),
        ("/manufacturers", {"name": "New Labs"}),
        ("/compositions", {"composition_text": "Atenolol 50mg"}),
    ],
)
def test_a_sales_executive_can_add_to_the_catalogue(client, sales_headers, path, payload):
    """Adding what a customer has just asked for is the counter's job.

    A brand nobody has stocked before turns up mid-sale, and waiting for an
    admin to key it in means waiting with a customer at the counter.
    """
    response = client.post(f"{API}{path}", json=payload, headers=sales_headers)
    assert response.status_code == 201, response.text


def test_but_cannot_change_or_remove_what_is_already_there(
    client, admin_headers, sales_headers
):
    """Editing is a different act from adding.

    Pack size, HSN code and schedule drive the unit maths, the tax on the
    bill and the prescription check, so they stay with an admin.
    """
    medicine = make_medicine(client, admin_headers, brand_name="Establishol")

    edited = client.patch(
        f"{API}/medicines/{medicine['id']}", json={"brand_name": "Renamed"},
        headers=sales_headers,
    )
    assert edited.status_code == 403
    assert edited.json()["detail"]["code"] == "FORBIDDEN_ROLE"

    assert client.delete(
        f"{API}/medicines/{medicine['id']}", headers=sales_headers
    ).status_code == 403


def test_sales_executive_can_read_the_catalogue(client, sales_headers, catalogue):
    assert client.get(f"{API}/medicines", headers=sales_headers).status_code == 200
    assert client.get(f"{API}/compositions", headers=sales_headers).status_code == 200
    assert client.get(f"{API}/manufacturers", headers=sales_headers).status_code == 200


def test_medicines_are_scoped_to_the_callers_store(client, admin_headers, catalogue, db, store):
    from app.models.store import Store, StoreSettings

    other = Store(name="Other Branch", state_code="19")
    db.add(other)
    db.flush()
    db.add(StoreSettings(store_id=other.id))
    db.add(
        Medicine(
            store_id=other.id,
            brand_name="Foreign Brand",
            manufacturer_id=db.query(Medicine).first().manufacturer_id,
            composition_id=db.query(Medicine).first().composition_id,
            search_text="foreign brand",
        )
    )
    db.commit()

    listed = client.get(f"{API}/medicines", headers=admin_headers).json()
    assert "Foreign Brand" not in {m["brand_name"] for m in listed["items"]}


# --- search-text upkeep --------------------------------------------------


def test_renaming_a_manufacturer_keeps_search_working(client, admin_headers, sales_headers):
    medicine = make_medicine(client, admin_headers, manufacturer_name="Oldname Labs")
    manufacturer_id = medicine["manufacturer_id"]

    client.patch(
        f"{API}/manufacturers/{manufacturer_id}",
        json={"name": "Newname Labs"},
        headers=admin_headers,
    )

    stale = client.get(f"{API}/medicines/search?q=oldname", headers=sales_headers).json()
    fresh = client.get(f"{API}/medicines/search?q=newname", headers=sales_headers).json()
    assert stale == []
    assert [r["brand_name"] for r in fresh] == ["Testolol 50"]


def test_changing_a_composition_updates_the_search_text(client, admin_headers, sales_headers):
    medicine = make_medicine(client, admin_headers)

    client.patch(
        f"{API}/medicines/{medicine['id']}",
        json={"composition_text": "Metoprolol 25mg"},
        headers=admin_headers,
    )

    assert client.get(f"{API}/medicines/search?q=atenolol", headers=sales_headers).json() == []
    assert len(client.get(f"{API}/medicines/search?q=metoprolol", headers=sales_headers).json()) == 1


# --- csv import ----------------------------------------------------------


CSV_HEADER = (
    "brand_name,manufacturer,composition,therapeutic_class,dosage_form,pack_size,"
    "pack_unit,schedule_type,hsn_code,rack_location\n"
)


def upload(client, headers, body: str, **params):
    return client.post(
        f"{API}/medicines/import",
        files={"file": ("medicines.csv", io.BytesIO(body.encode("utf-8")), "text/csv")},
        headers=headers,
        params=params,
    )


def test_csv_import_creates_medicines(client, admin_headers, sales_headers):
    body = CSV_HEADER + (
        "Dolo 650,Micro Labs,Paracetamol 650mg,Analgesic,TABLET,15,TABLET,OTC,30049099,A1\n"
        "Pan 40,Alkem,Pantoprazole 40mg,PPI,TABLET,15,TABLET,SCHEDULE_H,30049069,A2\n"
    )
    result = upload(client, admin_headers, body).json()

    assert result["total_rows"] == 2
    assert result["created"] == 2
    assert result["errors"] == []
    assert len(client.get(f"{API}/medicines/search?q=pantoprazole", headers=sales_headers).json()) == 1


def test_csv_import_applies_the_pack_rules(client, admin_headers):
    body = CSV_HEADER + (
        "Syrupy,Micro Labs,Paracetamol 125mg,Analgesic,SYRUP,60,ML,OTC,30049099,A1\n"
        "Tubey,Micro Labs,Povidone Iodine 5%,Antiseptic,OINTMENT,20,GM,OTC,30049099,A2\n"
    )
    assert upload(client, admin_headers, body).json()["created"] == 2

    listed = client.get(f"{API}/medicines?q=syrupy", headers=admin_headers).json()["items"][0]
    assert listed["pack_unit"] == "ML"
    assert listed["units_per_pack"] == 1
    assert listed["pack_label"] == "60 ml bottle"


def test_csv_import_reports_bad_rows_without_losing_the_good_ones(client, admin_headers):
    body = CSV_HEADER + (
        "Good One,Micro Labs,Paracetamol 650mg,Analgesic,TABLET,15,TABLET,OTC,30049099,A1\n"
        ",Micro Labs,Paracetamol 650mg,Analgesic,TABLET,15,TABLET,OTC,30049099,A2\n"
        "Bad Form,Micro Labs,Paracetamol 650mg,Analgesic,LOZENGE,15,TABLET,OTC,30049099,A3\n"
        "Bad Unit,Micro Labs,Paracetamol 650mg,Analgesic,SYRUP,60,TABLET,OTC,30049099,A4\n"
        "Good Two,Alkem,Pantoprazole 40mg,PPI,TABLET,15,TABLET,SCHEDULE_H,30049069,A5\n"
    )
    result = upload(client, admin_headers, body).json()

    assert result["created"] == 2
    assert [e["row"] for e in result["errors"]] == [3, 4, 5]
    assert result["errors"][0]["field"] == "brand_name"
    assert result["errors"][1]["field"] == "dosage_form"
    assert result["errors"][2]["field"] == "pack_unit"


def test_csv_import_skips_existing_rows_by_default(client, admin_headers):
    body = CSV_HEADER + (
        "Dolo 650,Micro Labs,Paracetamol 650mg,Analgesic,TABLET,15,TABLET,OTC,30049099,A1\n"
    )
    upload(client, admin_headers, body)
    second = upload(client, admin_headers, body).json()

    assert second["created"] == 0
    assert second["skipped"] == 1


def test_csv_import_can_update_existing_rows(client, admin_headers):
    body = CSV_HEADER + (
        "Dolo 650,Micro Labs,Paracetamol 650mg,Analgesic,TABLET,15,TABLET,OTC,30049099,A1\n"
    )
    upload(client, admin_headers, body)

    changed = CSV_HEADER + (
        "Dolo 650,Micro Labs,Paracetamol 650mg,Analgesic,TABLET,15,TABLET,SCHEDULE_H,30049069,B7\n"
    )
    result = upload(client, admin_headers, changed, update_existing=True).json()

    assert result["updated"] == 1
    listed = client.get(f"{API}/medicines?q=dolo", headers=admin_headers).json()["items"]
    assert listed[0]["schedule_type"] == "SCHEDULE_H"
    assert listed[0]["rack_location"] == "B7"


def test_csv_import_deduplicates_compositions_across_rows(client, admin_headers, db):
    body = CSV_HEADER + (
        "Brand A,Labs One,Amoxicillin 500mg + Clavulanic Acid 125mg,Antibiotic,TABLET,10,TABLET,SCHEDULE_H,30041020,A1\n"
        "Brand B,Labs Two,Clavulanic Acid 125mg + Amoxicillin 500mg,Antibiotic,TABLET,10,TABLET,SCHEDULE_H,30041020,A2\n"
    )
    result = upload(client, admin_headers, body).json()

    assert result["created"] == 2
    assert db.query(Composition).count() == 1


def test_a_bad_row_does_not_take_the_good_rows_before_it_with_it(client, admin_headers):
    """The report and the database have to agree.

    Rolling the whole session back on a bad row used to discard every
    medicine imported ahead of it while still counting them as created —
    a file could report "2 created" and leave one row on file.
    """
    body = CSV_HEADER + (
        "First Good,Micro Labs,Paracetamol 650mg,Analgesic,TABLET,15,TABLET,OTC,30049099,A1\n"
        "Bad Form,Micro Labs,Paracetamol 650mg,Analgesic,LOZENGE,15,TABLET,OTC,30049099,A2\n"
        "Second Good,Alkem,Pantoprazole 40mg,PPI,TABLET,15,TABLET,SCHEDULE_H,30049069,A3\n"
    )
    result = upload(client, admin_headers, body).json()
    assert result["created"] == 2
    assert [e["row"] for e in result["errors"]] == [3]

    on_file = client.get(f"{API}/medicines", params={"q": "Good"}, headers=admin_headers).json()
    assert sorted(m["brand_name"] for m in on_file["items"]) == ["First Good", "Second Good"]


def test_csv_import_rejects_a_file_without_the_required_columns(client, admin_headers):
    response = upload(client, admin_headers, "brand_name,notes\nX,hello\n")

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "MISSING_COLUMNS"


def test_csv_import_is_admin_only(client, sales_headers):
    response = upload(client, sales_headers, CSV_HEADER)

    assert response.status_code == 403


# --- choosing a maker rather than retyping one ---------------------------
#
# The maker box is a search now: four spellings of Cipla used to be four
# rows, with the catalogue's maker filter quietly showing a quarter of the
# brands. These pin the contract the picker binds to.


def test_a_medicine_can_be_created_against_an_existing_maker(client, admin_headers):
    first = make_medicine(client, admin_headers, brand_name="Alpha",
                          manufacturer_name="Cipla")

    response = client.post(
        f"{API}/medicines",
        json={
            "brand_name": "Beta",
            "manufacturer_id": first["manufacturer_id"],
            "composition_text": "IBUPROFEN (400MG)",
        },
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["manufacturer_id"] == first["manufacturer_id"]


def test_an_unknown_maker_id_is_refused(client, admin_headers):
    """The picker only ever sends an id it was given, so this is a bad client."""
    response = client.post(
        f"{API}/medicines",
        json={
            "brand_name": "Ghost",
            "manufacturer_id": MISSING_ID,
            "composition_text": "IBUPROFEN (400MG)",
        },
        headers=admin_headers,
    )
    assert response.status_code == 404
    assert response.json()["detail"]["field"] == "manufacturer_id"


def test_a_maker_typed_in_a_different_case_is_not_filed_twice(client, admin_headers):
    """The safety net behind the picker: even a name typed fresh reuses the
    row that already exists, whatever its capitalisation."""
    first = make_medicine(client, admin_headers, brand_name="Alpha",
                          manufacturer_name="Cipla")
    second = make_medicine(client, admin_headers, brand_name="Beta",
                           manufacturer_name="CIPLA")

    assert second["manufacturer_id"] == first["manufacturer_id"]


def test_makers_can_be_searched_by_part_of_the_name(client, admin_headers):
    """What the picker types into."""
    make_medicine(client, admin_headers, brand_name="Alpha", manufacturer_name="Cipla")
    make_medicine(client, admin_headers, brand_name="Beta", manufacturer_name="Sun Pharma")

    rows = client.get(f"{API}/manufacturers?q=cip", headers=admin_headers).json()
    assert [r["name"] for r in rows["items"]] == ["Cipla"]

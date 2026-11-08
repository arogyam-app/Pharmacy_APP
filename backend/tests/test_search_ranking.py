"""What the counter sees first when it searches.

Three tiers, because that is the order the question gets answered in: what
was asked for and can be handed over, then an equivalent that can be handed
over, then everything that cannot. A brand that is out of stock is not an
answer to "do you have it?" — it is on the list only so nobody searches for
it twice.
"""

from decimal import Decimal

from tests.conftest import API, make_batch, make_medicine


def search(client, headers, term):
    response = client.get(f"{API}/medicines/search?q={term}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def names(rows):
    return [r["brand_name"] for r in rows]


def test_in_stock_matches_come_before_out_of_stock_ones(client, admin_headers):
    empty = make_medicine(client, admin_headers, brand_name="Calpol 650",
                          composition_text="PARACETAMOL (650MG)")
    stocked = make_medicine(client, admin_headers, brand_name="Calpol 500",
                            composition_text="PARACETAMOL (500MG)")
    make_batch(client, admin_headers, stocked["id"], qty_packs="5")

    rows = search(client, admin_headers, "calpol")

    assert names(rows)[0] == "Calpol 500"
    assert set(names(rows)) == {"Calpol 500", "Calpol 650"}
    assert rows[0]["in_stock"] is True
    assert rows[-1]["in_stock"] is False
    assert empty["id"] == rows[-1]["id"]


def test_a_stocked_substitute_beats_the_out_of_stock_brand_asked_for(
    client, admin_headers
):
    """The case the tiering exists for: they asked for Calpol, it has run
    out, and the shop has another paracetamol on the shelf."""
    asked = make_medicine(client, admin_headers, brand_name="Calpol 500",
                          composition_text="PARACETAMOL (500MG)")
    substitute = make_medicine(client, admin_headers, brand_name="Dolo 500",
                               composition_text="PARACETAMOL (500MG)")
    make_batch(client, admin_headers, substitute["id"], qty_packs="5")

    rows = search(client, admin_headers, "calpol")

    assert names(rows) == ["Dolo 500", "Calpol 500"]
    # And it says why it is on the list, since the name does not match.
    assert rows[0]["is_alternative"] is True
    assert rows[1]["is_alternative"] is False


def test_a_named_match_in_stock_still_leads_its_substitutes(client, admin_headers):
    asked = make_medicine(client, admin_headers, brand_name="Calpol 500",
                          composition_text="PARACETAMOL (500MG)")
    other = make_medicine(client, admin_headers, brand_name="Dolo 500",
                          composition_text="PARACETAMOL (500MG)")
    make_batch(client, admin_headers, asked["id"], qty_packs="5")
    make_batch(client, admin_headers, other["id"], batch_no="D1", qty_packs="5")

    rows = search(client, admin_headers, "calpol")

    assert names(rows) == ["Calpol 500", "Dolo 500"]


def test_the_cheapest_substitute_is_offered_first(client, admin_headers):
    asked = make_medicine(client, admin_headers, brand_name="Calpol 500",
                          composition_text="PARACETAMOL (500MG)")
    dear = make_medicine(client, admin_headers, brand_name="Zed 500",
                         composition_text="PARACETAMOL (500MG)")
    cheap = make_medicine(client, admin_headers, brand_name="Abc 500",
                          composition_text="PARACETAMOL (500MG)")
    make_batch(client, admin_headers, dear["id"], batch_no="Z1", mrp_per_pack="200")
    make_batch(client, admin_headers, cheap["id"], batch_no="A1", mrp_per_pack="50")

    rows = search(client, admin_headers, "calpol")

    # Not alphabetical — the point of a substitute is that it is cheaper.
    assert names(rows)[:2] == ["Abc 500", "Zed 500"]


def test_a_substitute_out_of_stock_is_not_promoted(client, admin_headers):
    make_medicine(client, admin_headers, brand_name="Calpol 500",
                  composition_text="PARACETAMOL (500MG)")
    make_medicine(client, admin_headers, brand_name="Dolo 500",
                  composition_text="PARACETAMOL (500MG)")

    rows = search(client, admin_headers, "calpol")

    # Nothing is in stock, so the named match leads on its own merit.
    assert names(rows)[0] == "Calpol 500"
    assert all(r["in_stock"] is False for r in rows)


def test_an_unrelated_composition_is_never_dragged_in(client, admin_headers):
    make_medicine(client, admin_headers, brand_name="Calpol 500",
                  composition_text="PARACETAMOL (500MG)")
    other = make_medicine(client, admin_headers, brand_name="Brufen 400",
                          composition_text="IBUPROFEN (400MG)")
    make_batch(client, admin_headers, other["id"], qty_packs="9")

    rows = search(client, admin_headers, "calpol")
    assert "Brufen 400" not in names(rows)


def test_a_general_item_has_no_substitutes_dragged_in(client, admin_headers):
    """Two general items are not equivalents just because neither has a salt."""
    from tests.test_general_items import make_general

    soap = make_general(client, admin_headers, brand_name="Dettol Soap")
    make_general(client, admin_headers, brand_name="Lifebuoy Soap")
    make_batch(client, admin_headers, soap["id"], qty_packs="3")

    rows = search(client, admin_headers, "dettol")
    assert names(rows) == ["Dettol Soap"]


def test_the_limit_still_holds_once_substitutes_are_added(client, admin_headers):
    for n in range(6):
        item = make_medicine(client, admin_headers, brand_name=f"Para {n}",
                             composition_text="PARACETAMOL (500MG)")
        make_batch(client, admin_headers, item["id"], batch_no=f"B{n}")

    response = client.get(f"{API}/medicines/search?q=para&limit=3", headers=admin_headers)
    assert len(response.json()) == 3

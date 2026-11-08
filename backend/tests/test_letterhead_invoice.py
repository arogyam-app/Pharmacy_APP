"""The A4 letterhead bill and the branding it prints.

The layout is rendered by the SPA, so what the backend has to get right is
the payload behind it: the shop's default format, the logos and QR reaching
the print endpoint, and — since these values end up in an `<img src>` on a
document customers are handed — that only an image can be stored there.
"""

from tests.conftest import API, sell, stocked_medicine

PNG = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAA"
    "C0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def a_sale(client, headers, brand="Letterhead Test"):
    medicine, _ = stocked_medicine(client, headers, mrp="100.00", brand_name=brand)
    return sell(
        client, headers,
        [{"medicine_id": medicine["id"], "qty_units": "2", "discount_pct": "0"}],
        customer={"mobile": "9876500123", "name": "Ramesh Kumar"},
    )


def printed(client, headers, sale_id):
    response = client.get(f"{API}/sales/{sale_id}/print", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


# --- the default ----------------------------------------------------------


def test_a_new_store_bills_on_the_letterhead(client, admin_headers):
    settings = client.get(f"{API}/settings", headers=admin_headers).json()
    assert settings["print_format"] == "A4"


def test_the_invoice_payload_names_the_format_to_render(client, admin_headers):
    sale = a_sale(client, admin_headers)
    assert printed(client, admin_headers, sale["id"])["print_format"] == "A4"


def test_the_other_layouts_are_still_available(client, admin_headers):
    for fmt in ("THERMAL_80", "A5", "A4"):
        saved = client.patch(
            f"{API}/settings", json={"print_format": fmt}, headers=admin_headers
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["print_format"] == fmt


# --- branding -------------------------------------------------------------


def test_the_letterhead_images_reach_the_printed_bill(client, admin_headers):
    client.patch(
        f"{API}/settings",
        json={
            "logo_image": PNG,
            "partner_logo_image": PNG,
            "order_qr_image": PNG,
            "order_qr_caption": "Scan the QR to Order",
        },
        headers=admin_headers,
    )
    sale = a_sale(client, admin_headers)
    store = printed(client, admin_headers, sale["id"])["store"]

    assert store["logo_image"] == PNG
    assert store["partner_logo_image"] == PNG
    assert store["order_qr_image"] == PNG
    assert store["order_qr_caption"] == "Scan the QR to Order"


def test_a_bill_prints_without_any_branding_set(client, admin_headers):
    """A shop that has not uploaded a logo still gets a bill."""
    sale = a_sale(client, admin_headers, brand="Bare Letterhead")
    store = printed(client, admin_headers, sale["id"])["store"]

    assert store["logo_image"] is None
    assert store["order_qr_image"] is None


def test_the_bill_carries_what_the_letterhead_prints(client, admin_headers):
    """Licence and GSTIN are printed in the header, so they must be sent."""
    client.patch(
        f"{API}/store",
        json={
            "gstin": "36BUHPB9766L1Z0",
            "drug_licence_no_20b": "TG/RR/2026-152148",
            "address_line1": "G2, 4/44-150, Road no 1, Tirumala Hills",
            "city": "Puppalaguda, Rangareddy",
            "state": "Telangana",
            "pincode": "500089",
        },
        headers=admin_headers,
    )
    sale = a_sale(client, admin_headers, brand="Header Test")
    body = printed(client, admin_headers, sale["id"])

    assert body["store"]["gstin"] == "36BUHPB9766L1Z0"
    assert body["store"]["drug_licence_no_20b"] == "TG/RR/2026-152148"
    assert body["store"]["pincode"] == "500089"


def test_every_column_the_letterhead_prints_is_in_the_payload(client, admin_headers):
    sale = a_sale(client, admin_headers, brand="Columns Test")
    item = printed(client, admin_headers, sale["id"])["sale"]["items"][0]

    # #, Product, HSN no., Batch, Qty, MRP, GST, Amount
    for field in ("line_no", "medicine_name", "hsn_code", "batch_no",
                  "qty_units", "unit_mrp", "gst_rate", "net_amount"):
        assert field in item, field


def test_an_image_can_be_cleared(client, admin_headers):
    client.patch(f"{API}/settings", json={"logo_image": PNG}, headers=admin_headers)
    cleared = client.patch(f"{API}/settings", json={"logo_image": ""}, headers=admin_headers)

    assert cleared.status_code == 200
    assert cleared.json()["logo_image"] is None


def test_a_link_is_accepted_as_well_as_an_uploaded_image(client, admin_headers):
    saved = client.patch(
        f"{API}/settings",
        json={"logo_image": "https://cdn.example.com/logo.png"},
        headers=admin_headers,
    )
    assert saved.status_code == 200
    assert saved.json()["logo_image"] == "https://cdn.example.com/logo.png"


def test_only_an_image_can_be_stored_there(client, admin_headers):
    """These values are rendered into an <img src> on a customer's bill."""
    for bad in (
        "javascript:alert(1)",
        "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
        "<script>alert(1)</script>",
    ):
        response = client.patch(
            f"{API}/settings", json={"logo_image": bad}, headers=admin_headers
        )
        assert response.status_code == 422, (bad, response.text)

    # And nothing was stored.
    assert client.get(f"{API}/settings", headers=admin_headers).json()["logo_image"] is None


def test_an_oversized_image_is_refused(client, admin_headers):
    huge = "data:image/png;base64," + ("A" * (512 * 1024))
    response = client.patch(
        f"{API}/settings", json={"logo_image": huge}, headers=admin_headers
    )
    assert response.status_code == 422


def test_the_counter_cannot_change_the_letterhead(client, sales_headers):
    response = client.patch(
        f"{API}/settings", json={"logo_image": PNG}, headers=sales_headers
    )
    assert response.status_code == 403

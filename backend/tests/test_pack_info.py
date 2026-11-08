"""Reading a price list's pack column.

The strings here are the ones that actually turn up in distributor and
portal exports. What matters most is the countability rule: a pack read as
TABLET is sellable a piece at a time, so anything mis-read as a tablet would
let the counter sell one millilitre of syrup.
"""

from decimal import Decimal

import pytest

from app.models.enums import DosageForm, PackUnit
from app.services.catalog import resolve_pack
from app.services.packinfo import PackInfoError, parse_pack_info


def parsed(text):
    pack = parse_pack_info(text)
    return pack.dosage_form, pack.pack_size, pack.pack_unit


# --- the shapes in the user's file ---------------------------------------


@pytest.mark.parametrize(
    "text,form,size,unit",
    [
        ("strip of 10 tablets", DosageForm.TABLET, 10, PackUnit.TABLET),
        ("strip of 1 Tablet DT", DosageForm.TABLET, 1, PackUnit.TABLET),
        ("strip of 15 tablet er", DosageForm.TABLET, 15, PackUnit.TABLET),
        ("strip of 10 capsules", DosageForm.CAPSULE, 10, PackUnit.CAPSULE),
        ("vial of 1 Injection", DosageForm.INJECTION, 1, PackUnit.VIAL),
        ("vial of 1 Powder for Injection", DosageForm.INJECTION, 1, PackUnit.VIAL),
        ("bottle of 30 ml Oral Suspension", DosageForm.SYRUP, 30, PackUnit.ML),
        ("bottle of 60 ml Syrup", DosageForm.SYRUP, 60, PackUnit.ML),
        ("bottle of 15 ml Oral Drops", DosageForm.DROPS, 15, PackUnit.ML),
        ("tube of 30 gm Cream", DosageForm.OINTMENT, 30, PackUnit.GM),
        ("tube of 15 gm Ointment", DosageForm.OINTMENT, 15, PackUnit.GM),
        ("bottle of 10 ml Eye Drops", DosageForm.DROPS, 10, PackUnit.ML),
        ("ampoule of 2 ml Injection", DosageForm.INJECTION, 2, PackUnit.ML),
        ("sachet of 5 gm Granules", DosageForm.POWDER, 5, PackUnit.GM),
        ("jar of 200 gm Powder", DosageForm.POWDER, 200, PackUnit.GM),
    ],
)
def test_the_pack_phrases_a_price_list_carries(text, form, size, unit):
    assert parsed(text) == (form, Decimal(size), unit)


def test_case_and_spacing_do_not_matter():
    assert parsed("STRIP  OF  10   TABLETS") == parsed("strip of 10 tablets")


def test_a_leading_count_is_ignored():
    """"1 tube of 20 gm" — the leading 1 is the number of tubes, not the size."""
    assert parsed("1 tube of 20 gm Cream") == (DosageForm.OINTMENT, Decimal(20), PackUnit.GM)


def test_a_phrase_with_no_container_still_reads():
    assert parsed("10 tablets") == (DosageForm.TABLET, Decimal(10), PackUnit.TABLET)
    assert parsed("100 ml syrup") == (DosageForm.SYRUP, Decimal(100), PackUnit.ML)


def test_a_decimal_volume_survives():
    assert parsed("prefilled syringe of 0.5 ml Injection") == (
        DosageForm.INJECTION,
        Decimal("0.5"),
        PackUnit.ML,
    )


def test_litres_and_kilos_are_converted_not_taken_literally():
    """A "bottle of 1 litre" is 1000 ml, not a one-millilitre bottle."""
    assert parsed("bottle of 1 litre Syrup") == (DosageForm.SYRUP, Decimal(1000), PackUnit.ML)
    assert parsed("jar of 1 kg Powder") == (DosageForm.POWDER, Decimal(1000), PackUnit.GM)


def test_a_bare_solution_is_not_assumed_to_be_oral():
    """"Solution" alone could be an IV fluid or an antiseptic wash.

    It is measured correctly in ml and left as OTHER rather than filed as a
    syrup someone might hand a child.
    """
    assert parsed("bottle of 500 ml Solution") == (DosageForm.OTHER, Decimal(500), PackUnit.ML)


# --- the judgement calls -------------------------------------------------


def test_powder_for_injection_is_an_injection_not_a_powder():
    """It is reconstituted and injected; the shop stocks it as a vial."""
    assert parsed("vial of 1 Powder for Injection")[0] is DosageForm.INJECTION


def test_a_vial_is_counted_as_a_vial():
    """One vial is one sellable thing, whatever it holds."""
    form, size, unit = parsed("vial of 1 Injection")
    assert unit is PackUnit.VIAL
    assert resolve_pack(form, size, unit)[2] == 1


def test_a_measured_ampoule_keeps_its_volume():
    """"2 ml" is the strength of the ampoule, and still one sellable unit."""
    form, size, unit = parsed("ampoule of 2 ml Injection")
    assert (size, unit) == (Decimal(2), PackUnit.ML)
    assert resolve_pack(form, size, unit)[2] == 1


def test_only_tablets_and_capsules_come_out_countable():
    """The rule the counter depends on (CLAUDE.md §2)."""
    countable = {
        "strip of 10 tablets": 10,
        "strip of 10 capsules": 10,
        "bottle of 100 ml Syrup": 1,
        "vial of 1 Injection": 1,
        "tube of 30 gm Cream": 1,
        "bottle of 15 ml Oral Drops": 1,
        "jar of 200 gm Powder": 1,
    }
    for text, units in countable.items():
        form, size, unit = parsed(text)
        assert resolve_pack(form, size, unit)[2] == units, text


def test_an_unrecognised_form_is_not_guessed_at():
    """OTHER, not TABLET — a wrong guess would make it sellable piecemeal."""
    form, size, unit = parsed("box of 1 Kit")
    assert form is DosageForm.OTHER
    assert resolve_pack(form, size, unit)[2] == 1


def test_the_label_keeps_the_container_the_source_named():
    assert parse_pack_info("tube of 30 gm Cream").pack_label == "30 g tube"
    assert parse_pack_info("bottle of 30 tablets").pack_label == "30 tab bottle"
    # A vial needs no help — the catalogue already writes "1 vial".
    assert parse_pack_info("vial of 1 Injection").pack_label is None


def test_every_parse_survives_the_catalogue_rules():
    """Whatever comes out has to be a pack the catalogue will accept."""
    phrases = [
        "strip of 10 tablets", "strip of 1 Tablet DT", "vial of 1 Injection",
        "vial of 1 Powder for Injection", "bottle of 30 ml Oral Suspension",
        "bottle of 15 ml Oral Drops", "bottle of 60 ml Syrup", "tube of 20 gm Gel",
        "sachet of 5 gm Granules", "ampoule of 2 ml Injection", "box of 1 Kit",
        "bottle of 200 ml Lotion", "strip of 4 tablets", "packet of 10 sachets",
    ]
    for phrase in phrases:
        pack = parse_pack_info(phrase)
        size, unit, units_per_pack, label = resolve_pack(
            pack.dosage_form, pack.pack_size, pack.pack_unit, pack.pack_label
        )
        assert units_per_pack >= 1, phrase
        assert label, phrase


# --- refusals ------------------------------------------------------------


def test_a_pack_with_no_quantity_is_refused():
    with pytest.raises(PackInfoError):
        parse_pack_info("strip of tablets")


def test_an_empty_pack_column_is_refused():
    with pytest.raises(PackInfoError):
        parse_pack_info("   ")


def test_a_fractional_strip_is_refused():
    """2.5 tablets in a strip is a typo, not a pack."""
    with pytest.raises(PackInfoError):
        parse_pack_info("strip of 2.5 tablets")


def test_a_zero_pack_is_refused():
    with pytest.raises(PackInfoError):
        parse_pack_info("strip of 0 tablets")

"""Amount in words — Indian numbering, on the foot of every invoice."""

import pytest

from app.utils.money_words import amount_in_words


@pytest.mark.parametrize(
    "amount,expected",
    [
        ("0", "Rupees zero only"),
        ("1", "Rupees one only"),
        ("15", "Rupees fifteen only"),
        ("42", "Rupees forty two only"),
        ("100", "Rupees one hundred only"),
        ("120.50", "Rupees one hundred twenty and fifty paise only"),
        ("999", "Rupees nine hundred ninety nine only"),
        ("1000", "Rupees one thousand only"),
        # Lakh and crore, not "hundred thousand" and "million".
        ("150000", "Rupees one lakh fifty thousand only"),
        ("10000000", "Rupees one crore only"),
        ("12345678", "Rupees one crore twenty three lakh forty five thousand six hundred seventy eight only"),
        ("0.05", "Rupees zero and five paise only"),
    ],
)
def test_amount_in_words(amount, expected):
    assert amount_in_words(amount) == expected.capitalize()


def test_paise_are_spelled_out_not_left_as_a_decimal():
    """"₹120.50" must not read as "one hundred twenty point five"."""
    words = amount_in_words("120.50")
    assert "point" not in words
    assert "fifty paise" in words

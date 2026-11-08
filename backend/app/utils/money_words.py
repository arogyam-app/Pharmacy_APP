"""Rupee amounts in words, for the foot of an invoice.

Indian numbering, not international: 1,50,000 reads "one lakh fifty
thousand", never "one hundred fifty thousand". Paise are spelled out
separately because a bill of ₹120.50 must not read as "one hundred twenty
point five".
"""

from decimal import Decimal

ONES = [
    "", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
]
TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def _under_thousand(value: int) -> str:
    if value == 0:
        return ""
    if value < 20:
        return ONES[value]
    if value < 100:
        head = TENS[value // 10]
        rest = ONES[value % 10]
        return f"{head} {rest}".strip()
    head = f"{ONES[value // 100]} hundred"
    rest = _under_thousand(value % 100)
    return f"{head} {rest}".strip()


def _whole_in_words(value: int) -> str:
    if value == 0:
        return "zero"

    # Indian grouping: crore, lakh, thousand, then the last three digits.
    parts: list[str] = []
    for divisor, label in ((10_000_000, "crore"), (100_000, "lakh"), (1_000, "thousand")):
        chunk, value = divmod(value, divisor)
        if chunk:
            parts.append(f"{_whole_in_words(chunk) if divisor == 10_000_000 else _under_thousand(chunk)} {label}")
    if value:
        parts.append(_under_thousand(value))
    return " ".join(parts).strip()


def amount_in_words(amount: Decimal | float | str, currency: str = "Rupees") -> str:
    """`120.50` → "Rupees one hundred twenty and fifty paise only"."""
    value = Decimal(str(amount)).quantize(Decimal("0.01"))
    negative = value < 0
    value = abs(value)

    whole = int(value)
    paise = int((value - whole) * 100)

    words = f"{currency} {_whole_in_words(whole)}"
    if paise:
        words += f" and {_under_thousand(paise)} paise"
    words += " only"
    if negative:
        words = f"minus {words}"
    return " ".join(words.split()).capitalize()

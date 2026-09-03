"""Money parsing and arithmetic.

Everything is Decimal. A float never touches a P/L figure.

The legacy sheet writes numbers several ways in the same column, so parsing has
to cope with accounting negatives, currency symbols, thousands separators and
two different spellings of "nothing".
"""

from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")
ZERO = Decimal("0")

# The sheet uses a bare hyphen for "not applicable" (e.g. Put Risk on a call).
_NULLS = {"", "-", "--", "n/a", "N/A", "#N/A"}


def q2(value) -> Decimal:
    """Quantize to cents, rounding half up (how a broker rounds, and a sheet)."""
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def parse_money(raw) -> "Decimal | None":
    """Parse a sheet money cell.

    Handles ``(1,462.00)`` -> -1462.00, ``"$16,500.00"`` -> 16500.00,
    ``-$2,000.00`` -> -2000.00, ``.50`` -> 0.50, and ``-``/``""`` -> None.

    Returns None only for genuinely absent values; ``0`` and ``0.00`` parse to
    zero, because "no premium" and "no row" mean different things (see the
    disguised share rows described in docs/legacy-format.md).
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if text in _NULLS:
        return None

    negative = False
    # Accounting parentheses. Can co-exist with a currency symbol: ($1,234.00).
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()

    if text.startswith("-"):
        negative = not negative
        text = text[1:].strip()

    text = text.replace("$", "").replace(",", "").replace(" ", "")
    if text.startswith("-"):  # "-$1,000" leaves a second sign after stripping
        negative = not negative
        text = text[1:]

    if text in _NULLS:
        return None

    try:
        value = Decimal(text)
    except Exception as exc:  # noqa: BLE001 - re-raised with the original cell
        raise ValueError(f"cannot parse money value {raw!r}") from exc

    return -value if negative else value


def parse_int(raw) -> "int | None":
    """Parse an integer cell (quantities). Signed; the sheet uses -N for long."""
    value = parse_money(raw)
    if value is None:
        return None
    if value != value.to_integral_value():
        raise ValueError(f"expected an integer quantity, got {raw!r}")
    return int(value)


def fmt(value) -> str:
    """Render for reports: parenthesised negatives, thousands separators."""
    if value is None:
        return "-"
    value = q2(value)
    if value < 0:
        return f"({-value:,.2f})"
    return f"{value:,.2f}"

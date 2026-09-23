from accrueboard.domain.capitalization import apply_capitalization
from accrueboard.domain.documents import ExtractedDocument
from accrueboard.domain.money import money

from .factories import EQUIPMENT, FIXED_ASSETS, OFFICE, chart, invoice, line


def single_line(description: str, quantity: str, unit_price: str) -> ExtractedDocument:
    amount = str(money(unit_price) * int(quantity))
    return invoice(
        lines=(line(description, quantity, unit_price, amount, taxable=False),),
        subtotal=amount,
        shipping="0.00",
        tax_rate=None,
        tax="0.00",
        total=amount,
    )


def test_item_above_threshold_on_capitalizable_account_is_capitalized() -> None:
    accounts, events = apply_capitalization(
        single_line("Forklift", "1", "6000.00"), [EQUIPMENT], chart()
    )
    assert accounts == (FIXED_ASSETS,)
    assert len(events) == 1
    event = events[0]
    assert (event.line_index, event.from_account, event.to_account) == (0, EQUIPMENT, FIXED_ASSETS)
    assert event.unit_price == money("6000.00")
    assert "2500.00" in event.reason


def test_threshold_is_exclusive() -> None:
    doc = single_line("Scanner", "1", "2500.00")
    assert apply_capitalization(doc, [EQUIPMENT], chart()) == ((EQUIPMENT,), ())


def test_threshold_is_per_item_not_per_line() -> None:
    # Ten 400.00 monitors are 4,000.00 in total, but each item is below the threshold.
    doc = single_line("Monitors", "10", "400.00")
    assert apply_capitalization(doc, [EQUIPMENT], chart()) == ((EQUIPMENT,), ())


def test_non_capitalizable_accounts_are_untouched() -> None:
    doc = single_line("Consulting", "1", "9000.00")
    assert apply_capitalization(doc, [OFFICE], chart()) == ((OFFICE,), ())


def test_custom_threshold() -> None:
    doc = single_line("Chair", "1", "1200.00")
    accounts, _ = apply_capitalization(doc, [EQUIPMENT], chart(), threshold=money("1000.00"))
    assert accounts == (FIXED_ASSETS,)

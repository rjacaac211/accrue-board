"""Test builders for domain objects. Defaults describe a valid, internally consistent invoice."""

from datetime import date
from typing import Any

from accrueboard.domain.accounts import Account, AccountRole, AccountType, ChartOfAccounts
from accrueboard.domain.documents import DocumentType, ExtractedDocument, LineItem

TODAY = date(2026, 6, 15)

AP = "2000"
CARD = "2100"
BANK = "1000"
INVENTORY = "1300"
FIXED_ASSETS = "1500"
OFFICE = "6100"
EQUIPMENT = "6150"
SHIPPING = "5200"


def chart() -> ChartOfAccounts:
    accounts = (
        Account(code=BANK, name="Operating Bank", type=AccountType.ASSET),
        Account(code=INVENTORY, name="Inventory", type=AccountType.ASSET),
        Account(code=FIXED_ASSETS, name="Equipment (Fixed Assets)", type=AccountType.ASSET),
        Account(code=AP, name="Accounts Payable", type=AccountType.LIABILITY),
        Account(code=CARD, name="Credit Card Clearing", type=AccountType.LIABILITY),
        Account(code=SHIPPING, name="Freight & Shipping", type=AccountType.EXPENSE),
        Account(code=OFFICE, name="Office Supplies", type=AccountType.EXPENSE),
        Account(
            code=EQUIPMENT, name="Small Equipment", type=AccountType.EXPENSE, capitalizable=True
        ),
    )
    roles = {
        AccountRole.ACCOUNTS_PAYABLE: AP,
        AccountRole.CARD_CLEARING: CARD,
        AccountRole.BANK: BANK,
        AccountRole.INVENTORY: INVENTORY,
        AccountRole.FIXED_ASSETS: FIXED_ASSETS,
    }
    return ChartOfAccounts(accounts=accounts, roles=roles)


def line(
    description: str = "Printer toner",
    quantity: str = "2",
    unit_price: str = "45.00",
    amount: str = "90.00",
    *,
    taxable: bool = True,
) -> LineItem:
    return LineItem.model_validate(
        {
            "description": description,
            "quantity": quantity,
            "unit_price": unit_price,
            "amount": amount,
            "taxable": taxable,
        }
    )


def invoice(**overrides: Any) -> ExtractedDocument:
    """A valid invoice: 90.00 taxable + 125.00 exempt, 10.00 shipping, 8.25% tax on 90.00."""
    data: dict[str, Any] = {
        "doc_type": DocumentType.INVOICE,
        "vendor_name": "Acme Office Supply Inc.",
        "vendor_state": "TX",
        "document_number": "INV-00123",
        "issue_date": date(2026, 6, 1),
        "due_date": date(2026, 7, 1),
        "lines": (
            line(),
            line("Widget stock", "10", "12.50", "125.00", taxable=False),
        ),
        "subtotal": "215.00",
        "shipping": "10.00",
        "tax_rate": "0.0825",
        "tax": "7.43",
        "total": "232.43",
    }
    data.update(overrides)
    return ExtractedDocument.model_validate(data)

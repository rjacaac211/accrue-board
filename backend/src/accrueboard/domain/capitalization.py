"""Fixed-asset capitalization rule.

Applied after account coding, deterministically: a line item whose *unit* price is above the
threshold and that was coded to a capitalizable expense account (e.g. small equipment) is
re-coded to Fixed Assets. The default threshold of $2,500 per item mirrors the common US
de minimis safe-harbor amount; items at or below it are expensed.
"""

from collections.abc import Sequence
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from accrueboard.domain.accounts import AccountRole, ChartOfAccounts
from accrueboard.domain.documents import ExtractedDocument
from accrueboard.domain.money import Money, money

DEFAULT_CAPITALIZATION_THRESHOLD = money("2500.00")


class CapitalizationEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    line_index: int
    from_account: str
    to_account: str
    unit_price: Decimal
    threshold: Money
    reason: str


def apply_capitalization(
    doc: ExtractedDocument,
    line_accounts: Sequence[str],
    coa: ChartOfAccounts,
    *,
    threshold: Decimal = DEFAULT_CAPITALIZATION_THRESHOLD,
) -> tuple[tuple[str, ...], tuple[CapitalizationEvent, ...]]:
    """Return the (possibly re-coded) line accounts and one event per re-coded line."""
    fixed_assets = coa.role(AccountRole.FIXED_ASSETS)
    accounts: list[str] = []
    events: list[CapitalizationEvent] = []
    for i, (item, code) in enumerate(zip(doc.lines, line_accounts, strict=True)):
        if coa.has(code) and coa.get(code).capitalizable and item.unit_price > threshold:
            accounts.append(fixed_assets)
            events.append(
                CapitalizationEvent(
                    line_index=i,
                    from_account=code,
                    to_account=fixed_assets,
                    unit_price=item.unit_price,
                    threshold=threshold,
                    reason=(
                        f"unit price {item.unit_price} exceeds the {threshold} per-item "
                        f"capitalization threshold"
                    ),
                )
            )
        else:
            accounts.append(code)
    return tuple(accounts), tuple(events)

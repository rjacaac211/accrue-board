"""Client configuration loaded from the database: chart of accounts and routing settings."""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from accrueboard.db.models import Account, Client
from accrueboard.domain.accounts import Account as DomainAccount
from accrueboard.domain.accounts import AccountRole, AccountType, ChartOfAccounts
from accrueboard.domain.capitalization import DEFAULT_CAPITALIZATION_THRESHOLD
from accrueboard.domain.money import money
from accrueboard.domain.routing import RoutingConfig


def load_chart(session: Session, client_id: str) -> ChartOfAccounts:
    rows = session.execute(
        select(Account).where(Account.client_id == client_id).order_by(Account.code)
    ).scalars()
    accounts: list[DomainAccount] = []
    roles: dict[AccountRole, str] = {}
    for row in rows:
        accounts.append(
            DomainAccount(
                code=row.code,
                name=row.name,
                type=AccountType(row.type),
                description=row.description,
                capitalizable=row.capitalizable,
            )
        )
        if row.role:
            roles[AccountRole(row.role)] = row.code
    return ChartOfAccounts(accounts=tuple(accounts), roles=roles)


def routing_config(client: Client) -> RoutingConfig:
    config = client.config or {}
    return RoutingConfig(
        auto_post_threshold=float(config.get("auto_post_threshold", 0.9)),
        materiality_cap=money(config.get("materiality_cap", "10000.00")),
    )


def capitalization_threshold(client: Client) -> Decimal:
    return money(
        (client.config or {}).get("capitalization_threshold", str(DEFAULT_CAPITALIZATION_THRESHOLD))
    )

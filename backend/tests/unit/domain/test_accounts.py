import pytest
from pydantic import ValidationError

from accrueboard.domain.accounts import Account, AccountRole, AccountType, ChartOfAccounts

from .factories import AP, OFFICE, chart


def test_lookup_by_code_and_role() -> None:
    coa = chart()
    assert coa.get(OFFICE).name == "Office Supplies"
    assert coa.role(AccountRole.ACCOUNTS_PAYABLE) == AP
    with pytest.raises(KeyError):
        coa.get("0000")


def test_duplicate_codes_rejected() -> None:
    base = chart()
    with pytest.raises(ValidationError, match="duplicate"):
        ChartOfAccounts(
            accounts=(*base.accounts, Account(code=OFFICE, name="Dup", type=AccountType.EXPENSE)),
            roles=base.roles,
        )


def test_every_role_must_be_mapped() -> None:
    base = chart()
    roles = {r: c for r, c in base.roles.items() if r is not AccountRole.INVENTORY}
    with pytest.raises(ValidationError, match="inventory"):
        ChartOfAccounts(accounts=base.accounts, roles=roles)


def test_roles_must_point_to_existing_accounts() -> None:
    base = chart()
    roles = {**base.roles, AccountRole.BANK: "0001"}
    with pytest.raises(ValidationError, match="unknown"):
        ChartOfAccounts(accounts=base.accounts, roles=roles)

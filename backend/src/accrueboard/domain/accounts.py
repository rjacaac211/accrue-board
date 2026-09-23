"""Chart of accounts."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator


class AccountType(StrEnum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    REVENUE = "revenue"
    EXPENSE = "expense"


class AccountRole(StrEnum):
    """Accounts the posting logic needs to find by purpose rather than by code."""

    ACCOUNTS_PAYABLE = "accounts_payable"
    CARD_CLEARING = "card_clearing"
    BANK = "bank"
    INVENTORY = "inventory"
    FIXED_ASSETS = "fixed_assets"


class Account(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    name: str
    type: AccountType
    description: str = ""
    capitalizable: bool = False
    """True for expense accounts whose high-value items must be capitalized (e.g. equipment)."""


class ChartOfAccounts(BaseModel):
    model_config = ConfigDict(frozen=True)

    accounts: tuple[Account, ...]
    roles: dict[AccountRole, str]

    @model_validator(mode="after")
    def _check(self) -> "ChartOfAccounts":
        codes = [a.code for a in self.accounts]
        duplicates = sorted({c for c in codes if codes.count(c) > 1})
        if duplicates:
            raise ValueError(f"duplicate account codes: {duplicates}")
        missing_roles = sorted(set(AccountRole) - set(self.roles))
        if missing_roles:
            raise ValueError(f"chart of accounts has no account for roles: {missing_roles}")
        unknown = sorted(code for code in self.roles.values() if code not in codes)
        if unknown:
            raise ValueError(f"roles map to unknown account codes: {unknown}")
        return self

    def has(self, code: str) -> bool:
        return any(a.code == code for a in self.accounts)

    def get(self, code: str) -> Account:
        for account in self.accounts:
            if account.code == code:
                return account
        raise KeyError(f"unknown account code: {code}")

    def role(self, role: AccountRole) -> str:
        return self.roles[role]

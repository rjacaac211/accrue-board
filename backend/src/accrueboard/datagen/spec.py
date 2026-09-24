"""Typed models for the synthetic-data specifications (client and anomaly YAML files)."""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from importlib import resources
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from accrueboard.domain.accounts import Account, AccountRole, ChartOfAccounts
from accrueboard.domain.documents import PaymentMethod
from accrueboard.domain.money import Money
from accrueboard.domain.routing import Rule


class Split(StrEnum):
    HISTORY = "history"
    VALIDATION = "validation"
    TEST = "test"


EVAL_SPLITS = (Split.VALIDATION, Split.TEST)
RECEIPT_WIDTH_CHARS = 34
"""Longest item wording that fits on one line of a till receipt."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Period(_Frozen):
    start: date
    end: date


class Schedule(_Frozen):
    every_days: int | None = None
    jitter_days: int = 0
    monthly_day: int | None = None

    @model_validator(mode="after")
    def _one_kind(self) -> "Schedule":
        if (self.every_days is None) == (self.monthly_day is None):
            raise ValueError("schedule needs exactly one of every_days or monthly_day")
        return self


class CatalogItem(_Frozen):
    description: str
    account: str
    price: tuple[Money, Money]
    qty: tuple[int, int]
    taxable: bool = False
    fixed: bool = False
    """Always the same price (a subscription or rent)."""
    always: bool = False
    """Always included on the vendor's documents."""
    rare: bool = False
    """Picked only occasionally (e.g. an expensive one-off item)."""
    aliases: tuple[str, ...] = ()
    """Other ways the vendor words the same item on its documents."""


class Shipping(_Frozen):
    prob: float
    min: Money
    max: Money


class Discount(_Frozen):
    prob: float
    pct_min: int
    pct_max: int


class VendorSpec(_Frozen):
    id: str
    name: str
    address: tuple[str, ...]
    state: str
    layout: str
    number_format: str
    doc_type: Literal["invoice", "receipt"] = "invoice"
    payment_method: PaymentMethod | None = None
    schedule: Schedule | None = None
    terms_days: int = 30
    lines: tuple[int, int] = (1, 1)
    shipping: Shipping | None = None
    discount: Discount | None = None
    credit_note_prob: float = 0.0
    seasonal: bool = False
    catalog: tuple[CatalogItem, ...]

    @property
    def is_inventory_supplier(self) -> bool:
        return any(item.account == "1300" for item in self.catalog)


class ClientSpec(_Frozen):
    id: str
    name: str
    business: str
    address: tuple[str, ...]
    materiality_cap: Money
    capitalization_threshold: Money
    periods: dict[Split, Period]
    tax_rates: dict[str, Decimal]
    accounts: tuple[Account, ...]
    roles: dict[AccountRole, str]
    vendors: tuple[VendorSpec, ...]
    new_vendors: tuple[VendorSpec, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> "ClientSpec":
        codes = {a.code for a in self.accounts}
        for vendor in (*self.vendors, *self.new_vendors):
            if vendor.state not in self.tax_rates:
                raise ValueError(f"vendor {vendor.id}: no tax rate for state {vendor.state}")
            for item in vendor.catalog:
                if item.account not in codes:
                    raise ValueError(f"vendor {vendor.id}: unknown account {item.account}")
                if vendor.layout == "receipt":
                    too_long = [
                        w for w in (item.description, *item.aliases) if len(w) > RECEIPT_WIDTH_CHARS
                    ]
                    if too_long:
                        raise ValueError(
                            f"vendor {vendor.id}: receipt wording longer than "
                            f"{RECEIPT_WIDTH_CHARS} characters: {too_long}"
                        )
            sample = vendor.number_format.format(n=1234)
            if not sample or any(ch.isspace() for ch in sample):
                raise ValueError(f"vendor {vendor.id}: number_format must not produce whitespace")
            if vendor in self.vendors and vendor.schedule is None:
                raise ValueError(f"vendor {vendor.id}: recurring vendors need a schedule")
        ids = [v.id for v in (*self.vendors, *self.new_vendors)]
        if len(ids) != len(set(ids)):
            raise ValueError("vendor ids must be unique")
        return self

    @property
    def chart(self) -> ChartOfAccounts:
        return ChartOfAccounts(accounts=self.accounts, roles=self.roles)

    def vendor(self, vendor_id: str) -> VendorSpec:
        for vendor in (*self.vendors, *self.new_vendors):
            if vendor.id == vendor_id:
                return vendor
        raise KeyError(vendor_id)


class AnomalySpec(_Frozen):
    id: str
    expected_rule: Rule
    definition: str
    counts: dict[Split, int]


class HardNegativeSpec(_Frozen):
    id: str
    must_not_fire: tuple[Rule, ...]
    definition: str
    counts: dict[Split, int] | Literal["natural"]


class AnomalyCatalog(_Frozen):
    anomalies: tuple[AnomalySpec, ...]
    hard_negatives: tuple[HardNegativeSpec, ...] = Field(default=())

    def anomaly(self, anomaly_id: str) -> AnomalySpec:
        return next(a for a in self.anomalies if a.id == anomaly_id)

    def hard_negative(self, negative_id: str) -> HardNegativeSpec:
        return next(h for h in self.hard_negatives if h.id == negative_id)


def _read_spec(*parts: str) -> object:
    text = resources.files("accrueboard.datagen").joinpath("specs", *parts).read_text("utf-8")
    return yaml.safe_load(text)


def load_client(client_id: str) -> ClientSpec:
    return ClientSpec.model_validate(_read_spec("clients", f"{client_id}.yaml"))


def load_anomaly_catalog() -> AnomalyCatalog:
    return AnomalyCatalog.model_validate(_read_spec("anomalies.yaml"))

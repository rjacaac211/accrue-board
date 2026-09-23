from typing import Any

import pytest
from pydantic import ValidationError

from accrueboard.datagen.spec import ClientSpec, Split, load_anomaly_catalog, load_client
from accrueboard.domain.routing import Rule


def raw_client() -> dict[str, Any]:
    return load_client("fernhill").model_dump(mode="json")


def test_shipped_specs_load() -> None:
    client = load_client("fernhill")
    catalog = load_anomaly_catalog()
    assert client.chart.has("1300")
    assert {a.expected_rule for a in catalog.anomalies} <= set(Rule)
    assert set(client.periods) == set(Split)


def test_periods_are_ordered_and_disjoint() -> None:
    periods = load_client("fernhill").periods
    assert periods[Split.HISTORY].end < periods[Split.VALIDATION].start
    assert periods[Split.VALIDATION].end < periods[Split.TEST].start


def test_unknown_account_is_rejected() -> None:
    data = raw_client()
    data["vendors"][0]["catalog"][0]["account"] = "9999"
    with pytest.raises(ValidationError, match="unknown account"):
        ClientSpec.model_validate(data)


def test_vendor_state_needs_a_tax_rate() -> None:
    data = raw_client()
    data["vendors"][0]["state"] = "ZZ"
    with pytest.raises(ValidationError, match="tax rate"):
        ClientSpec.model_validate(data)


def test_number_format_cannot_produce_whitespace() -> None:
    data = raw_client()
    data["vendors"][0]["number_format"] = "0{n:12d}"
    with pytest.raises(ValidationError, match="whitespace"):
        ClientSpec.model_validate(data)


def test_recurring_vendor_needs_a_schedule() -> None:
    data = raw_client()
    data["vendors"][0]["schedule"] = None
    with pytest.raises(ValidationError, match="schedule"):
        ClientSpec.model_validate(data)


def test_vendor_ids_are_unique() -> None:
    data = raw_client()
    data["new_vendors"][0]["id"] = data["vendors"][0]["id"]
    with pytest.raises(ValidationError, match="unique"):
        ClientSpec.model_validate(data)

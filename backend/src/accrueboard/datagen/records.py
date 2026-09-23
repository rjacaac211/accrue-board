"""Ground-truth records written by the generator and read by the pipeline evaluation."""

from datetime import datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from accrueboard.datagen.spec import Split
from accrueboard.domain.documents import ExtractedDocument
from accrueboard.domain.routing import Rule

FileFormat = Literal["pdf", "png"]


class AnomalyLabel(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: str
    expected_rule: Rule
    source_doc_id: str | None = None
    """The earlier document this one duplicates or was derived from, if any."""
    injected: bool = True
    """False when the property occurred naturally and was labelled after generation."""


class HardNegativeLabel(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: str
    must_not_fire: tuple[Rule, ...]
    related_doc_id: str | None = None


class GroundTruth(BaseModel):
    """Everything known to be true about one generated document."""

    model_config = ConfigDict(frozen=True)

    doc_id: str
    client_id: str
    split: Split
    vendor_id: str
    received_at: AwareDatetime
    layout: str
    file_format: FileFormat
    file: str | None
    """Path of the rendered file relative to the dataset directory (None for history)."""
    copy_of: str | None = None
    """For exact-file duplicates: the document whose bytes this file repeats."""
    document: ExtractedDocument
    """What is printed on the document, i.e. the correct extraction."""
    line_accounts: tuple[str, ...]
    """Correct account per line, before the capitalization rule is applied."""
    anomalies: tuple[AnomalyLabel, ...] = ()
    hard_negatives: tuple[HardNegativeLabel, ...] = ()
    extra: dict[str, Any] = {}
    """Rendering-only content for unsupported documents (statement rows, quote items)."""

    @property
    def is_anomalous(self) -> bool:
        return bool(self.anomalies)


class Manifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    client_id: str
    seed: int
    generator_version: str
    counts: dict[str, int]
    anomaly_counts: dict[str, dict[str, int]]
    hard_negative_counts: dict[str, dict[str, int]]
    periods: dict[str, dict[str, str]]


def received_sort_key(record: GroundTruth) -> tuple[datetime, str]:
    return (record.received_at, record.doc_id)

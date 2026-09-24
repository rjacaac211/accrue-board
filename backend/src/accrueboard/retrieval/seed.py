"""Seed a knowledge store from the history split of a generated dataset."""

from collections.abc import Iterable

from accrueboard.datagen.records import GroundTruth
from accrueboard.datagen.spec import ClientSpec, Split
from accrueboard.domain.duplicates import normalize_vendor
from accrueboard.pipeline.coding import ClientContext
from accrueboard.retrieval.knowledge import EntrySource, KnowledgeEntry


def history_entries(records: Iterable[GroundTruth]) -> list[KnowledgeEntry]:
    """One knowledge entry per line item of every history document."""
    entries: list[KnowledgeEntry] = []
    for record in records:
        if record.split is not Split.HISTORY:
            continue
        doc = record.document
        vendor_key = normalize_vendor(doc.vendor_name)
        if vendor_key is None or doc.vendor_name is None:
            continue
        for i, (item, account) in enumerate(zip(doc.lines, record.line_accounts, strict=True)):
            entries.append(
                KnowledgeEntry(
                    entry_id=f"{record.doc_id}:{i}",
                    client_id=record.client_id,
                    vendor_key=vendor_key,
                    vendor_name=doc.vendor_name,
                    description=item.description,
                    amount=item.amount,
                    account=account,
                    source=EntrySource.HISTORY,
                    document_ref=record.doc_id,
                )
            )
    return entries


def client_context(spec: ClientSpec) -> ClientContext:
    return ClientContext(
        client_id=spec.id, name=spec.name, business=spec.business, chart=spec.chart
    )

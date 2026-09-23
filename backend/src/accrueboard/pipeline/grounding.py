"""Grounding: is each extracted value actually printed in the document's text layer?

A model can return a well-formed, internally consistent value that is simply not on the page.
Grounding catches that by searching the PDF's own text for every extracted value, after
normalizing the different ways a value can be printed:

- amounts and quantities: "$1,234.50", "1234.50", "USD 1,234.50" and "-21.50" all match 1234.5
- percentages: "8.25%" and "8.25 %" match a rate of 0.0825
- dates: 12/01/2025, 12/01/25, 2025-12-01, December 01, 2025, Dec 01, 2025 and 01 Dec 2025
- names and numbers: compared case-insensitively, ignoring spaces and punctuation

Grounding needs a text layer, so scanned images are reported as unverifiable rather than
ungrounded; the second extraction pass covers them instead.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from accrueboard.domain.documents import ExtractedDocument

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_MONTHS = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
_DATE_PATTERNS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), ("%Y-%m-%d",)),
    (re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"), ("%m/%d/%Y", "%m/%d/%y")),
    (
        re.compile(rf"\b(?:{_MONTHS})[a-z]*\.? \d{{1,2}}, \d{{4}}\b", re.IGNORECASE),
        ("%B %d, %Y", "%b %d, %Y", "%b. %d, %Y"),
    ),
    (
        re.compile(rf"\b\d{{1,2}} (?:{_MONTHS})[a-z]* \d{{4}}\b", re.IGNORECASE),
        ("%d %b %Y", "%d %B %Y"),
    ),
)


def _alnum(value: str) -> str:
    return re.sub(r"[^0-9a-z]", "", value.casefold())


def _words(value: str) -> str:
    return " ".join(re.sub(r"[^0-9a-z]+", " ", value.casefold()).split())


@dataclass(frozen=True)
class TextIndex:
    """Pre-parsed values found in a document's text layer."""

    numbers: frozenset[Decimal]
    percents: frozenset[Decimal]
    dates: frozenset[date]
    alnum: str
    words: str

    @classmethod
    def build(cls, text: str) -> "TextIndex":
        numbers: set[Decimal] = set()
        for token in _NUMBER.findall(text):
            try:
                numbers.add(Decimal(token.replace(",", "")))
            except InvalidOperation:
                continue
        percents = {Decimal(m) / 100 for m in _PERCENT.findall(text)}
        dates: set[date] = set()
        for pattern, formats in _DATE_PATTERNS:
            for match in pattern.findall(text):
                for fmt in formats:
                    try:
                        dates.add(datetime.strptime(match, fmt).date())  # noqa: DTZ007
                        break
                    except ValueError:
                        continue
        return cls(
            numbers=frozenset(numbers),
            percents=frozenset(percents),
            dates=frozenset(dates),
            alnum=_alnum(text),
            words=_words(text),
        )

    def has_number(self, value: Decimal) -> bool:
        return abs(value) in self.numbers

    def has_text(self, value: str) -> bool:
        words = _words(value)
        return bool(words) and words in self.words

    def has_code(self, value: str) -> bool:
        key = _alnum(value)
        return bool(key) and key in self.alnum


def ground(doc: ExtractedDocument, text: str) -> dict[str, bool]:
    """Map each extracted (non-empty) field to whether it was found in ``text``."""
    index = TextIndex.build(text)
    checks: dict[str, bool] = {}
    if doc.vendor_name:
        checks["vendor_name"] = index.has_text(doc.vendor_name)
    for name in ("document_number", "po_number", "referenced_document_number"):
        value = getattr(doc, name)
        if value:
            checks[name] = index.has_code(value)
    for name in ("issue_date", "due_date"):
        value = getattr(doc, name)
        if value is not None:
            checks[name] = value in index.dates
    for name in ("subtotal", "total"):
        value = getattr(doc, name)
        if value is not None:
            checks[name] = index.has_number(value)
    for name in ("discount", "shipping", "tax"):
        value = getattr(doc, name)
        if value:
            checks[name] = index.has_number(value)
    if doc.tax_rate is not None:
        checks["tax_rate"] = doc.tax_rate in index.percents
    for i, item in enumerate(doc.lines):
        checks[f"lines[{i}].description"] = index.has_text(item.description[:30])
        checks[f"lines[{i}].quantity"] = index.has_number(item.quantity)
        checks[f"lines[{i}].unit_price"] = index.has_number(item.unit_price)
        checks[f"lines[{i}].amount"] = index.has_number(item.amount)
    return checks

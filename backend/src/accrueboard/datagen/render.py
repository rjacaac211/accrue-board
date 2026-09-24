"""Render ground-truth records to PDF (and receipts to PNG).

Six invoice styles differ in fonts, header placement, labels, date and money formats and how
taxable lines are marked, so extraction cannot rely on one fixed position or spelling. Every
value in the ground truth is printed on the page; nothing extraction is scored on is hidden.

Output is deterministic across runs and operating systems: ReportLab runs in invariant mode (no
timestamps or random ids in the file), receipt scans are drawn with Pillow's bundled FreeType,
and raster noise comes from a seeded generator.
"""

import io
import random
import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import reportlab
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.colors import Color, HexColor, black
from reportlab.lib.pagesizes import letter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

from accrueboard.datagen.records import GroundTruth
from accrueboard.datagen.spec import ClientSpec, VendorSpec
from accrueboard.domain.documents import DocumentType, ExtractedDocument

# ---------------------------------------------------------------------------- formatting


def money_plain(value: Decimal) -> str:
    return f"{value:,.2f}"


def money_dollar(value: Decimal) -> str:
    return f"${value:,.2f}"


def money_usd(value: Decimal) -> str:
    return f"USD {value:,.2f}"


def percent(rate: Decimal) -> str:
    text = f"{rate * 100:f}".rstrip("0").rstrip(".")
    return f"{text}%"


def quantity(value: Decimal) -> str:
    return f"{value.normalize():f}"


@dataclass(frozen=True)
class Style:
    font: str
    bold: str
    accent: Color
    title: str
    number_label: str
    date_label: str
    due_label: str
    date_format: str
    money: Callable[[Decimal], str]
    header: str  # "left" | "right" | "banner"
    grid: bool
    taxable_marker: str  # "column" | "star"
    tax_label: str  # may contain {rate}
    total_label: str
    terms_note: str


STYLES: dict[str, Style] = {
    "classic": Style(
        font="Times-Roman",
        bold="Times-Bold",
        accent=HexColor("#1f3a5f"),
        title="INVOICE",
        number_label="Invoice #",
        date_label="Invoice Date",
        due_label="Due Date",
        date_format="%m/%d/%Y",
        money=money_dollar,
        header="left",
        grid=True,
        taxable_marker="column",
        tax_label="Sales Tax ({rate})",
        total_label="Total Due",
        terms_note="Payment terms: Net {terms} days",
    ),
    "modern": Style(
        font="Helvetica",
        bold="Helvetica-Bold",
        accent=HexColor("#0f766e"),
        title="Invoice",
        number_label="Invoice No.",
        date_label="Issued",
        due_label="Due",
        date_format="%B %d, %Y",
        money=money_dollar,
        header="banner",
        grid=False,
        taxable_marker="star",
        tax_label="Tax {rate}",
        total_label="Amount Due",
        terms_note="Please pay within {terms} days of the invoice date.",
    ),
    "compact": Style(
        font="Courier",
        bold="Courier-Bold",
        accent=black,
        title="BILL",
        number_label="Bill Number",
        date_label="Bill Date",
        due_label="Pay By",
        date_format="%Y-%m-%d",
        money=money_plain,
        header="right",
        grid=False,
        taxable_marker="column",
        tax_label="Sales tax @ {rate}",
        total_label="TOTAL",
        terms_note="Terms: net {terms}. Amounts in US dollars.",
    ),
    "boxed": Style(
        font="Helvetica",
        bold="Helvetica-Bold",
        accent=HexColor("#7c3aed"),
        title="INVOICE",
        number_label="Reference",
        date_label="Date",
        due_label="Payment Due",
        date_format="%d %b %Y",
        money=money_usd,
        header="left",
        grid=True,
        taxable_marker="star",
        tax_label="Sales Tax ({rate})",
        total_label="Balance Due",
        terms_note="Net {terms}. Thank you for your business.",
    ),
    "ledger": Style(
        font="Times-Roman",
        bold="Times-Bold",
        accent=HexColor("#9a3412"),
        title="Statement of Charges - Invoice",
        number_label="Invoice Number",
        date_label="Invoice Date",
        due_label="Due Date",
        date_format="%b %d, %Y",
        money=money_dollar,
        header="right",
        grid=True,
        taxable_marker="column",
        tax_label="Sales Tax {rate}",
        total_label="Invoice Total",
        terms_note="Remit within {terms} days.",
    ),
    "minimal": Style(
        font="Helvetica",
        bold="Helvetica-Bold",
        accent=HexColor("#334155"),
        title="Invoice",
        number_label="#",
        date_label="Date",
        due_label="Due",
        date_format="%m/%d/%y",
        money=money_plain,
        header="left",
        grid=False,
        taxable_marker="star",
        tax_label="Tax ({rate})",
        total_label="Total (USD)",
        terms_note="Due in {terms} days.",
    ),
}


# ---------------------------------------------------------------------------- drawing helpers


class _Page:
    def __init__(self, canvas: Canvas, style: Style, width: float, height: float) -> None:
        self.c = canvas
        self.s = style
        self.w = width
        self.h = height

    def text(
        self,
        x: float,
        y: float,
        value: str,
        *,
        size: float = 9,
        bold: bool = False,
        align: str = "left",
        color: Color = black,
    ) -> None:
        self.c.setFillColor(color)
        self.c.setFont(self.s.bold if bold else self.s.font, size)
        if align == "right":
            self.c.drawRightString(x, y, value)
        elif align == "center":
            self.c.drawCentredString(x, y, value)
        else:
            self.c.drawString(x, y, value)
        self.c.setFillColor(black)

    def rule(
        self, x1: float, y: float, x2: float, *, color: Color = black, width: float = 0.6
    ) -> None:
        self.c.setStrokeColor(color)
        self.c.setLineWidth(width)
        self.c.line(x1, y, x2, y)
        self.c.setStrokeColor(black)

    def fmt_date(self, value: date | None) -> str:
        return value.strftime(self.s.date_format) if value else ""


def _canvas(
    buffer: io.BytesIO, size: tuple[float, float], vendor: VendorSpec, title: str
) -> Canvas:
    canvas = Canvas(buffer, pagesize=size, invariant=1, pageCompression=1)
    canvas.setAuthor(vendor.name)
    canvas.setTitle(title)
    canvas.setCreator("AccrueBoard synthetic data")
    return canvas


# ---------------------------------------------------------------------------- bills


def _render_bill(record: GroundTruth, vendor: VendorSpec, client: ClientSpec) -> bytes:
    doc = record.document
    style = STYLES[record.layout if record.layout in STYLES else vendor.layout]
    credit = doc.doc_type is DocumentType.CREDIT_NOTE
    buffer = io.BytesIO()
    width, height = letter
    title = "CREDIT MEMO" if credit else style.title
    canvas = _canvas(buffer, letter, vendor, f"{title} {doc.document_number}")
    page = _Page(canvas, style, width, height)
    left, right = 54.0, width - 54.0
    y = height - 60

    # Header: vendor identity and document facts.
    facts: list[tuple[str, str]] = [
        ("Credit Memo #" if credit else style.number_label, doc.document_number or ""),
        (style.date_label, page.fmt_date(doc.issue_date)),
    ]
    if doc.due_date and not credit:
        facts.append((style.due_label, page.fmt_date(doc.due_date)))
    if doc.po_number:
        facts.append(("PO Number", doc.po_number))
    if credit and doc.referenced_document_number:
        facts.append(("Applies to Invoice", doc.referenced_document_number))

    if style.header == "banner":
        canvas.setFillColor(style.accent)
        canvas.rect(0, height - 96, width, 96, stroke=0, fill=1)
        page.text(left, height - 50, vendor.name, size=18, bold=True, color=HexColor("#ffffff"))
        page.text(left, height - 68, ", ".join(vendor.address), size=9, color=HexColor("#ffffff"))
        page.text(
            right, height - 50, title, size=20, bold=True, align="right", color=HexColor("#ffffff")
        )
        y = height - 124
        for label, value in facts:
            page.text(right - 150, y, f"{label}:", size=9)
            page.text(right, y, value, size=9, bold=True, align="right")
            y -= 13
        top_block = y
        y = height - 124
    else:
        vendor_x = left if style.header == "left" else right
        vendor_align = "left" if style.header == "left" else "right"
        facts_x = right if style.header == "left" else left
        page.text(
            vendor_x, y, vendor.name, size=16, bold=True, align=vendor_align, color=style.accent
        )
        for i, line in enumerate(vendor.address):
            page.text(vendor_x, y - 16 - 11 * i, line, size=9, align=vendor_align)
        page.text(
            facts_x,
            y,
            title,
            size=18,
            bold=True,
            align="right" if style.header == "left" else "left",
        )
        fy = y - 22
        for label, value in facts:
            if style.header == "left":
                page.text(facts_x - 110, fy, f"{label}:", size=9)
                page.text(facts_x, fy, value, size=9, bold=True, align="right")
            else:
                page.text(facts_x, fy, f"{label}:", size=9)
                page.text(facts_x + 110, fy, value, size=9, bold=True)
            fy -= 13
        top_block = min(fy, y - 16 - 11 * len(vendor.address))
        y = top_block - 10

    # Bill to.
    y = min(y, top_block) - 14
    page.text(left, y, "Bill To:" if not credit else "Credit To:", size=9, bold=True)
    page.text(left, y - 12, client.name, size=9)
    for i, line in enumerate(client.address):
        page.text(left, y - 24 - 11 * i, line, size=9)
    y -= 36 + 11 * len(client.address)

    # Line items.
    cols = {
        "desc": left,
        "qty": left + 300,
        "price": left + 390,
        "amount": right,
        "tax": right - 118,
    }
    if style.taxable_marker == "column":
        cols["amount"] = right
    page.rule(left, y + 12, right, color=style.accent, width=1.2)
    page.text(cols["desc"], y, "Description", size=9, bold=True)
    page.text(cols["qty"], y, "Qty", size=9, bold=True, align="right")
    page.text(cols["price"], y, "Unit Price", size=9, bold=True, align="right")
    if style.taxable_marker == "column":
        page.text(cols["price"] + 26, y, "Tax", size=9, bold=True, align="center")
    page.text(cols["amount"], y, "Amount", size=9, bold=True, align="right")
    y -= 6
    page.rule(left, y, right, color=style.accent)
    y -= 14
    any_star = False
    for item in doc.lines:
        description = item.description
        if style.taxable_marker == "star" and item.taxable:
            description += " *"
            any_star = True
        page.text(cols["desc"], y, description, size=9)
        page.text(cols["qty"], y, quantity(item.quantity), size=9, align="right")
        page.text(cols["price"], y, style.money(item.unit_price), size=9, align="right")
        if style.taxable_marker == "column":
            page.text(cols["price"] + 26, y, "T" if item.taxable else "", size=9, align="center")
        page.text(cols["amount"], y, style.money(item.amount), size=9, align="right")
        if style.grid:
            page.rule(left, y - 5, right, color=HexColor("#d4d4d4"), width=0.4)
        y -= 17
    if any_star:
        page.text(left, y - 2, "* taxable item", size=7)
    y -= 12

    # Totals.
    assert doc.subtotal is not None and doc.total is not None
    totals: list[tuple[str, str]] = [("Subtotal", style.money(doc.subtotal))]
    if doc.discount > 0:
        totals.append(("Discount", f"-{style.money(doc.discount)}"))
    if doc.shipping > 0:
        totals.append(("Shipping & Handling", style.money(doc.shipping)))
    if doc.tax > 0 or any(i.taxable for i in doc.lines):
        rate = percent(doc.tax_rate) if doc.tax_rate is not None else ""
        label = style.tax_label.format(rate=rate).replace("()", "").replace("@ ", "").strip()
        totals.append((label, style.money(doc.tax)))
    total_label = "Total Credit" if credit else style.total_label
    label_x = right - 190
    for label, value in totals:
        page.text(label_x, y, label, size=9)
        page.text(right, y, value, size=9, align="right")
        y -= 14
    page.rule(label_x, y + 8, right, color=style.accent, width=1)
    y -= 4
    page.text(label_x, y, total_label, size=11, bold=True)
    page.text(right, y, style.money(doc.total), size=11, bold=True, align="right")

    # Footer.
    if credit:
        note = "This credit will be applied to your account balance."
    elif vendor.terms_days > 0:
        note = style.terms_note.format(terms=vendor.terms_days)
    else:
        note = "Charged to the payment method on file."
    page.text(left, 60, note, size=8)
    page.text(left, 48, f"Questions? Contact billing at {vendor.name}.", size=8)
    canvas.showPage()
    canvas.save()
    return buffer.getvalue()


# ---------------------------------------------------------------------------- receipts

# Receipts come as PDFs or as phone-style PNG scans. Both are painted from the same rows, in
# Bitstream Vera (shipped with ReportLab). The PNG is drawn directly with Pillow, whose bundled
# FreeType produces identical pixels on every OS; rasterizing the PDF instead would not, because
# PDF renderers differ between platforms. Identical bytes keep recorded model responses
# replayable on any machine.
RECEIPT_FONT, RECEIPT_BOLD = "AccrueVera", "AccrueVeraBold"
_FONT_DIR = Path(reportlab.__file__).parent / "fonts"
RECEIPT_WIDTH = 226.0  # points (80 mm till roll)
_MARGIN = 14.0


@dataclass(frozen=True)
class ReceiptRow:
    advance: float
    """Vertical distance from the previous row, in points."""
    left: str = ""
    right: str = ""
    center: str = ""
    size: float = 7.5
    bold: bool = False
    indent: float = 0.0
    rule: bool = False


def receipt_rows(record: GroundTruth, vendor: VendorSpec) -> list[ReceiptRow]:
    doc = record.document
    assert doc.subtotal is not None
    assert doc.total is not None
    issued = doc.issue_date.strftime("%m/%d/%Y") if doc.issue_date else ""
    rows = [ReceiptRow(advance=30, center=vendor.name.upper(), size=10, bold=True)]
    rows += [ReceiptRow(advance=11, center=line) for line in vendor.address]
    rows += [
        ReceiptRow(advance=20, left=f"Date: {issued}  {record.received_at:%H:%M}"),
        ReceiptRow(advance=11, left=f"Receipt #: {doc.document_number}"),
        ReceiptRow(advance=5, rule=True),
    ]
    for item in doc.lines:
        tag = " T" if item.taxable else "  "
        rows += [
            ReceiptRow(advance=12, left=item.description),
            ReceiptRow(
                advance=10,
                left=f"{quantity(item.quantity)} @ {money_plain(item.unit_price)}",
                right=f"{money_plain(item.amount)}{tag}",
                indent=10,
            ),
        ]
    rows.append(ReceiptRow(advance=5, rule=True))
    totals = [("SUBTOTAL", doc.subtotal)]
    if doc.discount > 0:
        totals.append(("DISCOUNT", -doc.discount))
    if doc.shipping > 0:
        totals.append(("SHIPPING", doc.shipping))
    if doc.tax > 0:
        label = f"TAX {percent(doc.tax_rate)}" if doc.tax_rate is not None else "SALES TAX"
        totals.append((label, doc.tax))
    rows += [ReceiptRow(advance=12, left=label, right=money_plain(v)) for label, v in totals]
    last4 = random.Random(f"{vendor.id}:card").randint(1000, 9999)
    rows += [
        ReceiptRow(advance=15, left="TOTAL", right=money_dollar(doc.total), size=9, bold=True),
        ReceiptRow(advance=18, left=f"VISA ************{last4}"),
        ReceiptRow(advance=10, left="PAID - CARD SALE APPROVED"),
        ReceiptRow(advance=22, center="T = taxable item", size=6.5),
        ReceiptRow(advance=10, center="THANK YOU FOR SHOPPING WITH US", size=7),
    ]
    return rows


def _receipt_height(rows: list[ReceiptRow]) -> float:
    return sum(row.advance for row in rows) + 40


def _receipt_fonts() -> None:
    registered = pdfmetrics.getRegisteredFontNames()
    if RECEIPT_FONT not in registered:
        pdfmetrics.registerFont(TTFont(RECEIPT_FONT, str(_FONT_DIR / "Vera.ttf")))
    if RECEIPT_BOLD not in registered:
        pdfmetrics.registerFont(TTFont(RECEIPT_BOLD, str(_FONT_DIR / "VeraBd.ttf")))


def _render_receipt(record: GroundTruth, vendor: VendorSpec) -> bytes:
    _receipt_fonts()
    rows = receipt_rows(record, vendor)
    width, height = RECEIPT_WIDTH, _receipt_height(rows)
    buffer = io.BytesIO()
    canvas = _canvas(buffer, (width, height), vendor, f"Receipt {record.document.document_number}")
    left, right, mid = _MARGIN, width - _MARGIN, width / 2
    y = height
    for row in rows:
        y -= row.advance
        if row.rule:
            canvas.setLineWidth(0.5)
            canvas.line(left, y, right, y)
            continue
        canvas.setFont(RECEIPT_BOLD if row.bold else RECEIPT_FONT, row.size)
        if row.center:
            canvas.drawCentredString(mid, y, row.center)
        if row.left:
            canvas.drawString(left + row.indent, y, row.left)
        if row.right:
            canvas.drawRightString(right, y, row.right)
    canvas.showPage()
    canvas.save()
    return buffer.getvalue()


def _receipt_png(record: GroundTruth, vendor: VendorSpec, *, seed: str, dpi: int = 200) -> bytes:
    """A slightly rotated, noisy grayscale scan of the receipt, drawn directly with Pillow."""
    rows = receipt_rows(record, vendor)
    scale = dpi / 72
    width_pt, height_pt = RECEIPT_WIDTH, _receipt_height(rows)
    image = Image.new("L", (round(width_pt * scale), round(height_pt * scale)), 255)
    draw = ImageDraw.Draw(image)
    fonts: dict[tuple[bool, float], ImageFont.FreeTypeFont] = {}

    def font(bold: bool, size: float) -> ImageFont.FreeTypeFont:
        key = (bold, size)
        if key not in fonts:
            name = "VeraBd.ttf" if bold else "Vera.ttf"
            fonts[key] = ImageFont.truetype(str(_FONT_DIR / name), round(size * scale))
        return fonts[key]

    left, right, mid = _MARGIN * scale, (width_pt - _MARGIN) * scale, width_pt * scale / 2
    y = 0.0
    for row in rows:
        y += row.advance * scale
        if row.rule:
            draw.line([(left, y), (right, y)], fill=0, width=max(1, round(0.5 * scale)))
            continue
        face = font(row.bold, row.size)
        if row.center:
            draw.text((mid, y), row.center, font=face, fill=0, anchor="ms")
        if row.left:
            draw.text((left + row.indent * scale, y), row.left, font=face, fill=0, anchor="ls")
        if row.right:
            draw.text((right, y), row.right, font=face, fill=0, anchor="rs")

    rng = random.Random(seed)
    image = image.rotate(
        rng.uniform(-1.5, 1.5), resample=Image.Resampling.BICUBIC, expand=True, fillcolor=255
    )
    noise = Image.frombytes("L", image.size, rng.randbytes(image.size[0] * image.size[1]))
    image = Image.blend(image, noise, 0.07)
    return encode_gray_png(image)


def encode_gray_png(image: Image.Image) -> bytes:
    """Encode an 8-bit grayscale image as PNG using the standard library's zlib.

    Pillow's encoder links its own compression library, which differs between platforms, so the
    same pixels can compress to different bytes. Encoding here keeps files byte-identical.
    """
    if image.mode != "L":
        raise ValueError("expected an 8-bit grayscale ('L') image")
    width, height = image.size
    raw = image.tobytes()
    scanlines = b"".join(b"\x00" + raw[row * width : (row + 1) * width] for row in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(scanlines, 6))
        + chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------------------- unsupported docs


def _render_other(record: GroundTruth, vendor: VendorSpec, client: ClientSpec) -> bytes:
    doc: ExtractedDocument = record.document
    kind = record.extra["kind"]
    style = STYLES[vendor.layout]
    buffer = io.BytesIO()
    width, height = letter
    title = "STATEMENT OF ACCOUNT" if kind == "statement" else "QUOTATION"
    canvas = _canvas(buffer, letter, vendor, title)
    page = _Page(canvas, style, width, height)
    left, right = 54.0, width - 54.0
    y = height - 60
    page.text(left, y, vendor.name, size=16, bold=True, color=style.accent)
    for i, line in enumerate(vendor.address):
        page.text(left, y - 16 - 11 * i, line, size=9)
    page.text(right, y, title, size=16, bold=True, align="right")
    page.text(right, y - 20, f"Date: {page.fmt_date(doc.issue_date)}", size=9, align="right")
    if doc.document_number:
        page.text(right, y - 33, f"Quote #: {doc.document_number}", size=9, align="right")
    y -= 80
    page.text(left, y, "Prepared for:", size=9, bold=True)
    page.text(left, y - 12, client.name, size=9)
    y -= 44
    if kind == "statement":
        page.text(left, y, "Date", size=9, bold=True)
        page.text(left + 120, y, "Reference", size=9, bold=True)
        page.text(right, y, "Open Amount", size=9, bold=True, align="right")
        y -= 16
        for row in record.extra["rows"]:
            page.text(left, y, row["date"], size=9)
            page.text(left + 120, y, row["number"], size=9)
            page.text(right, y, style.money(Decimal(row["amount"])), size=9, align="right")
            y -= 14
        y -= 10
        page.text(right - 260, y, "Balance Outstanding", size=10, bold=True)
        page.text(
            right,
            y,
            style.money(Decimal(record.extra["balance"])),
            size=10,
            bold=True,
            align="right",
        )
        page.text(
            left,
            60,
            "This statement is for your records. Please do not pay from this statement;",
            size=8,
        )
        page.text(left, 48, "refer to the individual invoices listed above.", size=8)
    else:
        page.text(left, y, "Item", size=9, bold=True)
        page.text(left + 300, y, "Qty", size=9, bold=True, align="right")
        page.text(left + 390, y, "Unit Price", size=9, bold=True, align="right")
        page.text(right, y, "Line Total", size=9, bold=True, align="right")
        y -= 16
        estimate = Decimal(0)
        for row in record.extra["rows"]:
            page.text(left, y, row["description"], size=9)
            page.text(left + 300, y, row["quantity"], size=9, align="right")
            page.text(left + 390, y, style.money(Decimal(row["unit_price"])), size=9, align="right")
            page.text(right, y, style.money(Decimal(row["amount"])), size=9, align="right")
            estimate += Decimal(row["amount"])
            y -= 14
        y -= 10
        page.text(right - 260, y, "Estimated Total", size=10, bold=True)
        page.text(right, y, style.money(estimate), size=10, bold=True, align="right")
        page.text(
            left,
            60,
            f"This quotation is valid for {record.extra['valid_days']} days. "
            "It is not a request for payment.",
            size=8,
        )
    canvas.showPage()
    canvas.save()
    return buffer.getvalue()


# ---------------------------------------------------------------------------- entry points


def render_pdf(record: GroundTruth, client: ClientSpec) -> bytes:
    vendor = client.vendor(record.vendor_id)
    if record.document.doc_type is DocumentType.OTHER:
        return _render_other(record, vendor, client)
    if record.layout == "receipt":
        return _render_receipt(record, vendor)
    return _render_bill(record, vendor, client)


def render(record: GroundTruth, client: ClientSpec) -> bytes:
    """Bytes of the file for ``record`` in its declared format."""
    if record.file_format == "png":
        vendor = client.vendor(record.vendor_id)
        return _receipt_png(record, vendor, seed=f"{record.client_id}:{record.doc_id}")
    return render_pdf(record, client)

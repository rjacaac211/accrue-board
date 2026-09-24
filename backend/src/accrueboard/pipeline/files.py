"""Incoming document files: media type and PDF text layer."""

import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pypdfium2 as pdfium

MediaType = Literal["application/pdf", "image/png", "image/jpeg"]
_SIGNATURES: tuple[tuple[bytes, MediaType], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)


class UnsupportedFileError(ValueError):
    pass


@dataclass(frozen=True)
class SourceFile:
    name: str
    data: bytes
    media_type: MediaType

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def is_pdf(self) -> bool:
        return self.media_type == "application/pdf"

    @classmethod
    def from_bytes(cls, name: str, data: bytes) -> "SourceFile":
        for signature, media_type in _SIGNATURES:
            if data.startswith(signature):
                return cls(name=name, data=data, media_type=media_type)
        raise UnsupportedFileError(f"{name}: not a PDF, PNG or JPEG file")

    @classmethod
    def from_path(cls, path: Path) -> "SourceFile":
        return cls.from_bytes(path.name, path.read_bytes())


_PDFIUM = threading.Lock()
"""PDFium is not thread-safe: every call into it, from any thread, must hold this lock."""


def pdf_text(data: bytes) -> str:
    """The text layer of every page of a PDF (empty for scanned PDFs without one)."""
    with _PDFIUM:
        document = pdfium.PdfDocument(data)
        try:
            pages: list[str] = []
            for index in range(len(document)):
                textpage = document[index].get_textpage()
                try:
                    pages.append(textpage.get_text_range())
                finally:
                    textpage.close()
            return "\n".join(pages)
        finally:
            document.close()

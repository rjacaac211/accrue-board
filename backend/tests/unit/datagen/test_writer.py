"""Full-dataset round trip: files on disk, reproducibility, and text-layer completeness."""

import hashlib
import json
from pathlib import Path

import pypdfium2 as pdfium
import pytest

from accrueboard.cli import main
from accrueboard.datagen.records import GroundTruth
from accrueboard.datagen.spec import EVAL_SPLITS, Split
from accrueboard.datagen.writer import DatasetDirError, read_records
from accrueboard.domain.documents import DocumentType


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("data")
    assert main(["datagen", "--out", str(out), "--seed", "7"]) == 0
    return out / "fernhill"


def digest_tree(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def all_records(dataset: Path) -> list[GroundTruth]:
    return [r for split in Split for r in read_records(dataset, split)]


def test_manifest_matches_records(dataset: Path) -> None:
    manifest = json.loads((dataset / "manifest.json").read_text())
    records = all_records(dataset)
    for split in Split:
        assert manifest["counts"][split.value] == sum(r.split is split for r in records)
    assert manifest["seed"] == 7


def test_every_eval_record_has_its_file(dataset: Path) -> None:
    for record in all_records(dataset):
        if record.split in EVAL_SPLITS:
            assert record.file is not None
            assert (dataset / record.file).is_file(), record.doc_id
    assert not (dataset / "documents" / "history").exists()


def test_exact_duplicates_share_bytes_with_their_source(dataset: Path) -> None:
    records = {r.doc_id: r for r in all_records(dataset)}
    copies = [r for r in records.values() if r.copy_of]
    assert copies
    for record in copies:
        source = records[record.copy_of or ""]
        assert record.file is not None
        assert source.file is not None
        assert (dataset / record.file).read_bytes() == (dataset / source.file).read_bytes()


def test_non_duplicate_files_are_unique(dataset: Path) -> None:
    hashes: dict[str, str] = {}
    for record in all_records(dataset):
        if record.file is None or record.copy_of:
            continue
        digest = hashlib.sha256((dataset / record.file).read_bytes()).hexdigest()
        assert digest not in hashes, (record.doc_id, hashes.get(digest))
        hashes[digest] = record.doc_id


def test_every_pdf_prints_its_key_values(dataset: Path) -> None:
    checked = 0
    for record in all_records(dataset):
        if record.file is None or record.file_format != "pdf":
            continue
        document = pdfium.PdfDocument((dataset / record.file).read_bytes())
        try:
            text = document[0].get_textpage().get_text_range().replace(",", "")
        finally:
            document.close()
        doc = record.document
        if doc.doc_type is DocumentType.OTHER:
            continue
        assert doc.document_number, record.doc_id
        assert doc.document_number in text, record.doc_id
        assert doc.total is not None
        assert f"{doc.total:.2f}" in text, record.doc_id
        for item in doc.lines:
            assert f"{item.amount:.2f}" in text, record.doc_id
        checked += 1
    assert checked > 350


def test_regenerating_is_byte_identical(dataset: Path, tmp_path: Path) -> None:
    assert main(["datagen", "--out", str(tmp_path), "--seed", "7"]) == 0
    assert digest_tree(tmp_path / "fernhill") == digest_tree(dataset)


def test_refuses_to_overwrite_a_foreign_directory(tmp_path: Path) -> None:
    foreign = tmp_path / "fernhill"
    foreign.mkdir()
    (foreign / "notes.txt").write_text("keep me")
    with pytest.raises(DatasetDirError):
        main(["datagen", "--out", str(tmp_path), "--no-render"])
    assert (foreign / "notes.txt").read_text() == "keep me"


def test_sample_command(tmp_path: Path) -> None:
    assert main(["sample", "--out", str(tmp_path)]) == 0
    truth = json.loads((tmp_path / "ground_truth.json").read_text())
    assert {entry["file"] for entry in truth} == {
        p.name for p in tmp_path.iterdir() if p.suffix in {".pdf", ".png"}
    }
    assert len(truth) >= 10


def test_text_files_use_lf_line_endings(dataset: Path) -> None:
    for name in ("manifest.json", "history.jsonl", "validation.jsonl", "test.jsonl"):
        assert b"\r\n" not in (dataset / name).read_bytes(), name

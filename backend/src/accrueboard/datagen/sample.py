"""A small, committed sample of rendered documents: one per layout and document type."""

import json
from pathlib import Path

from accrueboard.datagen.generate import generate
from accrueboard.datagen.records import GroundTruth
from accrueboard.datagen.render import render
from accrueboard.datagen.spec import EVAL_SPLITS, load_anomaly_catalog, load_client


def pick_samples(records: list[GroundTruth]) -> list[GroundTruth]:
    picked: dict[tuple[str, str, str], GroundTruth] = {}
    for record in records:
        if record.split in EVAL_SPLITS and record.copy_of is None:
            key = (record.layout, record.document.doc_type.value, record.file_format)
            picked.setdefault(key, record)
    return sorted(
        picked.values(), key=lambda r: (r.layout, r.document.doc_type.value, r.file_format)
    )


def write_samples(out: Path, *, client_id: str = "fernhill", seed: int = 7) -> list[Path]:
    spec = load_client(client_id)
    samples = pick_samples(generate(spec, load_anomaly_catalog(), seed))
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("*"):
        if stale.suffix in {".pdf", ".png", ".json"}:
            stale.unlink()
    paths: list[Path] = []
    truth: list[dict[str, object]] = []
    for record in samples:
        name = f"{record.layout}-{record.document.doc_type.value}.{record.file_format}"
        path = out / name
        path.write_bytes(render(record, spec))
        paths.append(path)
        truth.append({"file": name, **record.model_dump(mode="json", exclude={"file"})})
    (out / "ground_truth.json").write_text(json.dumps(truth, indent=2) + "\n", encoding="utf-8")
    return paths

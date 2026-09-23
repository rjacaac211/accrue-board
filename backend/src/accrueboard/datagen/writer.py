"""Write a generated dataset to disk.

Layout of ``<out>/<client_id>/``::

    manifest.json               seed, generator version, counts (no timestamps: reproducible)
    history.jsonl               knowledge-store seed (structured only, never rendered)
    validation.jsonl            ground truth, one GroundTruth per line, in arrival order
    test.jsonl
    documents/<split>/<doc_id>.pdf|png
"""

import json
import shutil
from collections import Counter
from pathlib import Path

from accrueboard.datagen.generate import GENERATOR_VERSION
from accrueboard.datagen.records import GroundTruth, Manifest
from accrueboard.datagen.render import render
from accrueboard.datagen.spec import EVAL_SPLITS, ClientSpec, Split


class DatasetDirError(RuntimeError):
    pass


def _prepare(target: Path) -> None:
    if target.exists():
        if any(target.iterdir()) and not (target / "manifest.json").is_file():
            raise DatasetDirError(
                f"{target} exists but is not a generated dataset; refusing to overwrite it"
            )
        # Clear the contents rather than the directory itself, which may be in use
        # (e.g. a shell's working directory on Windows).
        for child in target.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    target.mkdir(parents=True, exist_ok=True)


def build_manifest(records: list[GroundTruth], spec: ClientSpec, seed: int) -> Manifest:
    counts = Counter(r.split.value for r in records)
    anomalies: dict[str, Counter[str]] = {s.value: Counter() for s in EVAL_SPLITS}
    negatives: dict[str, Counter[str]] = {s.value: Counter() for s in EVAL_SPLITS}
    for r in records:
        if r.split in EVAL_SPLITS:
            anomalies[r.split.value].update(a.type for a in r.anomalies)
            negatives[r.split.value].update(h.type for h in r.hard_negatives)
    return Manifest(
        client_id=spec.id,
        seed=seed,
        generator_version=GENERATOR_VERSION,
        counts={s.value: counts.get(s.value, 0) for s in Split},
        anomaly_counts={k: dict(sorted(v.items())) for k, v in anomalies.items()},
        hard_negative_counts={k: dict(sorted(v.items())) for k, v in negatives.items()},
        periods={
            s.value: {"start": p.start.isoformat(), "end": p.end.isoformat()}
            for s, p in spec.periods.items()
        },
    )


def write_dataset(
    records: list[GroundTruth],
    spec: ClientSpec,
    seed: int,
    out_dir: Path,
    *,
    render_files: bool = True,
) -> Path:
    """Write records (and rendered files) under ``out_dir/<client_id>``; returns that path."""
    target = out_dir / spec.id
    _prepare(target)

    for split in Split:
        lines = [r.model_dump_json() for r in records if r.split is split]
        (target / f"{split.value}.jsonl").write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8", newline="\n"
        )

    if render_files:
        written: dict[str, bytes] = {}
        for record in records:
            if record.file is None or record.copy_of is not None:
                continue
            data = render(record, spec)
            path = target / record.file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            written[record.doc_id] = data
        for record in records:
            if record.file is not None and record.copy_of is not None:
                (target / record.file).write_bytes(written[record.copy_of])

    manifest = build_manifest(records, spec, seed)
    (target / "manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return target


def read_records(dataset_dir: Path, split: Split) -> list[GroundTruth]:
    path = dataset_dir / f"{split.value}.jsonl"
    return [
        GroundTruth.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

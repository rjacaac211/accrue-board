"""Command-line entry point: `accrueboard <command>`."""

import argparse
import time
from pathlib import Path
from typing import Any

from accrueboard import __version__
from accrueboard.config import get_settings


def _datagen(args: argparse.Namespace) -> int:
    from accrueboard.datagen.generate import generate
    from accrueboard.datagen.spec import load_anomaly_catalog, load_client
    from accrueboard.datagen.writer import write_dataset

    started = time.perf_counter()
    spec = load_client(args.client)
    records = generate(spec, load_anomaly_catalog(), args.seed)
    out = Path(args.out) if args.out else get_settings().data_dir / "generated"
    target = write_dataset(records, spec, args.seed, out, render_files=not args.no_render)
    elapsed = time.perf_counter() - started
    print(f"wrote {len(records)} records to {target} in {elapsed:.1f}s")
    return 0


def _sample(args: argparse.Namespace) -> int:
    from accrueboard.datagen.sample import write_samples

    out = Path(args.out) if args.out else get_settings().data_dir / "sample"
    paths = write_samples(out, client_id=args.client, seed=args.seed)
    print(f"wrote {len(paths)} sample documents to {out}")
    return 0


def _seed(args: argparse.Namespace) -> int:
    from accrueboard.clock import SystemClock
    from accrueboard.datagen.generate import generate
    from accrueboard.datagen.spec import load_anomaly_catalog, load_client
    from accrueboard.db.session import get_sessionmaker
    from accrueboard.retrieval.embeddings import FastEmbedder, HashingEmbedder
    from accrueboard.services.seed import seed_client

    settings = get_settings()
    spec = load_client(args.client)
    records = generate(spec, load_anomaly_catalog(), args.seed)
    embedder = (
        HashingEmbedder(dimensions=384)
        if args.embedder == "hashing"
        else FastEmbedder(settings.embedding_model, cache_dir=str(settings.embedding_cache_dir))
    )
    with get_sessionmaker()() as session, session.begin():
        summary = seed_client(session, spec, records, embedder, now=SystemClock().now())
    if not summary.created:
        print(f"client {summary.client_id} already exists; nothing to do")
    else:
        print(
            f"seeded {summary.client_id}: {summary.documents} posted history documents, "
            f"{summary.knowledge_entries} knowledge entries, {summary.posted_total} posted"
        )
    return 0


def _processor() -> "Any":
    from accrueboard.clock import SystemClock
    from accrueboard.db.session import get_sessionmaker
    from accrueboard.llm.factory import build_llm
    from accrueboard.pipeline.process import PipelineModels, Processor
    from accrueboard.retrieval.embeddings import FastEmbedder

    settings = get_settings()
    models = PipelineModels(
        classify=settings.model_classify,
        extract=settings.model_extract,
        verify=settings.model_verify,
        code=settings.model_code,
    )
    embedder = FastEmbedder(settings.embedding_model, cache_dir=str(settings.embedding_cache_dir))
    return Processor(get_sessionmaker(), build_llm(settings), models, embedder, SystemClock())


def _ingest(args: argparse.Namespace) -> int:
    from accrueboard.clock import SystemClock
    from accrueboard.db.session import get_sessionmaker
    from accrueboard.pipeline.files import SourceFile
    from accrueboard.pipeline.process import ingest

    now = SystemClock().now()
    with get_sessionmaker()() as session, session.begin():
        for name in args.files:
            task = ingest(session, args.client, SourceFile.from_path(Path(name)), received_at=now)
            print(f"queued {name} as {task.id}")
    return 0


def _ingest_dataset(args: argparse.Namespace) -> int:
    from accrueboard.datagen.spec import Split
    from accrueboard.datagen.writer import read_records
    from accrueboard.db.session import get_sessionmaker
    from accrueboard.pipeline.files import SourceFile
    from accrueboard.pipeline.process import ingest

    dataset = get_settings().data_dir / "generated" / args.client
    records = read_records(dataset, Split(args.split))[: args.limit]
    with get_sessionmaker()() as session, session.begin():
        for record in records:
            if record.file is None:
                continue
            source = SourceFile.from_path(dataset / record.file)
            ingest(session, args.client, source, received_at=record.received_at)
    print(f"queued {len(records)} {args.split} documents for {args.client}")
    return 0


def _worker(args: argparse.Namespace) -> int:
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    processor = _processor()
    while True:
        processor.requeue_expired()
        outcome = processor.run_once(args.client)
        if outcome is not None:
            print(f"{outcome.task_id}: {outcome.state.value} - {outcome.summary}")
            continue
        if args.drain:
            return 0
        time.sleep(args.poll)


def _learning_curve(args: argparse.Namespace) -> int:
    from accrueboard.datagen.generate import generate
    from accrueboard.datagen.spec import load_anomaly_catalog, load_client
    from accrueboard.eval.learning_curve import ALL, learning_curve, to_markdown, write_report
    from accrueboard.llm.factory import build_llm
    from accrueboard.retrieval.embeddings import FastEmbedder, HashingEmbedder

    settings = get_settings()
    spec = load_client(args.client)
    records = generate(spec, load_anomaly_catalog(), args.seed)
    embedder = (
        HashingEmbedder(dimensions=384)
        if args.embedder == "hashing"
        else FastEmbedder(settings.embedding_model, cache_dir=str(settings.embedding_cache_dir))
    )
    steps = [ALL if s.strip() == "all" else int(s) for s in args.steps.split(",")]
    llm = build_llm(settings) if args.with_model else None
    points = learning_curve(
        records,
        spec,
        embedder,
        steps=steps,
        llm=llm,
        model=settings.model_code,
        test_limit=args.test_limit,
        new_vendors_only=args.new_vendors_only,
    )
    out = Path(args.out) if args.out else settings.data_dir / "eval"
    meta = {
        "client": spec.id,
        "seed": args.seed,
        "steps": steps,
        "embedder": args.embedder,
        "with_model": bool(args.with_model),
        "model": settings.model_code if args.with_model else None,
        "test_limit": args.test_limit,
        "new_vendors_only": args.new_vendors_only,
    }
    path = write_report(points, out, meta=meta)
    print(to_markdown(points))
    print(f"wrote {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="accrueboard", description=__doc__)
    parser.add_argument("--version", action="version", version=f"accrueboard {__version__}")
    commands = parser.add_subparsers(dest="command")

    datagen = commands.add_parser("datagen", help="generate a synthetic dataset")
    datagen.add_argument("--client", default="fernhill")
    datagen.add_argument("--seed", type=int, default=7)
    datagen.add_argument("--out", help="output directory (default: <data_dir>/generated)")
    datagen.add_argument("--no-render", action="store_true", help="write ground truth only")
    datagen.set_defaults(handler=_datagen)

    sample = commands.add_parser("sample", help="render one example per layout")
    sample.add_argument("--client", default="fernhill")
    sample.add_argument("--seed", type=int, default=7)
    sample.add_argument("--out", help="output directory (default: <data_dir>/sample)")
    sample.set_defaults(handler=_sample)

    seed = commands.add_parser("seed", help="load a client and its coded history into the database")
    seed.add_argument("--client", default="fernhill")
    seed.add_argument("--seed", type=int, default=7)
    seed.add_argument(
        "--embedder",
        choices=["fast", "hashing"],
        default="fast",
        help="fast = local ONNX model (default); hashing = dependency-free, for tests",
    )
    seed.set_defaults(handler=_seed)

    ingest_cmd = commands.add_parser("ingest", help="queue document files for processing")
    ingest_cmd.add_argument("files", nargs="+")
    ingest_cmd.add_argument("--client", default="fernhill")
    ingest_cmd.set_defaults(handler=_ingest)

    dataset_cmd = commands.add_parser(
        "ingest-dataset", help="queue generated documents in arrival order (demo and evaluation)"
    )
    dataset_cmd.add_argument("--client", default="fernhill")
    dataset_cmd.add_argument("--split", choices=["validation", "test"], default="validation")
    dataset_cmd.add_argument("--limit", type=int)
    dataset_cmd.set_defaults(handler=_ingest_dataset)

    worker = commands.add_parser("worker", help="process queued tasks")
    worker.add_argument("--client", help="only this client's tasks")
    worker.add_argument("--drain", action="store_true", help="exit when the queue is empty")
    worker.add_argument("--poll", type=float, default=2.0, help="seconds between queue checks")
    worker.set_defaults(handler=_worker)

    evaluate = commands.add_parser("eval", help="evaluation experiments")
    experiments = evaluate.add_subparsers(dest="experiment", required=True)
    curve = experiments.add_parser(
        "learning-curve", help="coding accuracy as reviewed documents are fed back"
    )
    curve.add_argument("--client", default="fernhill")
    curve.add_argument("--seed", type=int, default=7)
    curve.add_argument(
        "--steps",
        default="0,50,100,all",
        help="numbers of reviewed documents to feed back, comma separated ('all' = every one)",
    )
    curve.add_argument(
        "--with-model",
        action="store_true",
        help="also evaluate the full cascade (calls or replays the model)",
    )
    curve.add_argument("--test-limit", type=int, help="evaluate only the first N test documents")
    curve.add_argument(
        "--new-vendors-only",
        action="store_true",
        help="evaluate only test documents from vendors absent from the history",
    )
    curve.add_argument("--embedder", choices=["fast", "hashing"], default="fast")
    curve.add_argument("--out", help="output directory (default: <data_dir>/eval)")
    curve.set_defaults(handler=_learning_curve)

    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 0
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())

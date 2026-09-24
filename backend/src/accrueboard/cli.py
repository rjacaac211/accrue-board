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


def _bootstrap(args: argparse.Namespace) -> int:
    """Make a fresh installation demo-ready: generate the dataset and seed the client, each
    only if missing (safe to run on every start)."""
    settings = get_settings()
    if not (settings.data_dir / "generated" / args.client / "validation.jsonl").is_file():
        _datagen(argparse.Namespace(client=args.client, seed=args.seed, out=None, no_render=False))
    return _seed(argparse.Namespace(client=args.client, seed=args.seed, embedder=settings.embedder))


def _processor() -> "Any":
    from accrueboard.agents.review_assistant.service import ReviewAssistant
    from accrueboard.clock import SystemClock
    from accrueboard.db.session import get_sessionmaker
    from accrueboard.llm.factory import ORACLE, build_llm
    from accrueboard.pipeline.process import PipelineModels, Processor
    from accrueboard.retrieval.embeddings import configured_embedder

    settings = get_settings()
    models = PipelineModels(
        classify=settings.model_classify,
        extract=settings.model_extract,
        verify=settings.model_verify,
        code=settings.model_code,
    )
    embedder = configured_embedder()
    llm = build_llm(settings)
    clock = SystemClock()
    assistant = (
        ReviewAssistant(llm, settings.model_assistant, embedder, clock)
        if settings.review_assistant and settings.llm_mode != ORACLE
        else None
    )
    return Processor(get_sessionmaker(), llm, models, embedder, clock, assistant=assistant)


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


def _review_assistant_eval(args: argparse.Namespace) -> int:
    from accrueboard.datagen.generate import generate
    from accrueboard.datagen.spec import Split, load_anomaly_catalog, load_client
    from accrueboard.db.scratch import fresh_database, scratch_url
    from accrueboard.db.session import get_sessionmaker
    from accrueboard.eval.review_assistant import evaluate, summarize, to_markdown, write_report
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
    llm = build_llm(settings)
    url = scratch_url("eval")
    print(f"evaluating in a fresh database ({url.rsplit('/', 1)[-1]})")
    with fresh_database(url):
        cases = evaluate(
            records,
            spec,
            get_sessionmaker(),
            llm=llm,
            model=settings.model_assistant,
            embedder=embedder,
            split=Split(args.split),
            limit=args.limit,
        )
    out = Path(args.out) if args.out else settings.data_dir / "eval"
    meta = {
        "client": spec.id,
        "seed": args.seed,
        "split": args.split,
        "limit": args.limit,
        "embedder": args.embedder,
        "model": settings.model_assistant,
    }
    path = write_report(cases, out, meta=meta)
    print(to_markdown(summarize(cases)))
    print(f"wrote {path}")
    return 0


def _eval_models() -> "Any":
    from accrueboard.pipeline.process import PipelineModels

    settings = get_settings()
    return PipelineModels(
        classify=settings.model_classify,
        extract=settings.model_extract,
        verify=settings.model_verify,
        code=settings.model_code,
    )


def _eval_llm(replay: bool, archive: Path, scratch: Path, workdir: Path) -> "Any":
    """The model client for an end-to-end run: committed recordings, or live and recorded."""
    from accrueboard.eval.archive import unpack
    from accrueboard.llm.client import RecordingLLM, ReplayMode
    from accrueboard.llm.factory import build_llm

    if replay:
        count = unpack(archive, workdir)
        print(f"replaying {count} recorded responses from {archive.name}")
        return RecordingLLM(workdir, ReplayMode.REPLAY)
    live = get_settings().model_copy(
        update={"recordings_dir": scratch / "recordings", "llm_mode": "auto"}
    )
    built = build_llm(live)
    if not isinstance(built, RecordingLLM):
        raise TypeError("the end-to-end evaluation records its model calls")
    return built


def _end_to_end(args: argparse.Namespace) -> int:
    import tempfile

    from accrueboard.datagen.generate import generate
    from accrueboard.datagen.spec import load_anomaly_catalog, load_client
    from accrueboard.db.scratch import fresh_database, scratch_url
    from accrueboard.db.session import get_sessionmaker
    from accrueboard.eval.end_to_end import MAX_ESCAPE_RATE, run
    from accrueboard.retrieval.embeddings import FastEmbedder, HashingEmbedder

    settings = get_settings()
    spec = load_client(args.client)
    records = generate(spec, load_anomaly_catalog(), args.seed)
    embedder = (
        HashingEmbedder(dimensions=384)
        if args.embedder == "hashing"
        else FastEmbedder(settings.embedding_model, cache_dir=str(settings.embedding_cache_dir))
    )
    models = _eval_models()
    results = settings.data_dir / "results"
    scratch = settings.data_dir / "eval-run"
    meta = {
        "client": spec.id,
        "seed": args.seed,
        "limit": args.limit,
        "models": {
            "classify": models.classify,
            "extract": models.extract,
            "verify": models.verify,
            "code": models.code,
        },
        "assistant_model": settings.model_assistant,
        "embedder": settings.embedding_model if args.embedder == "fast" else "hashing",
        "max_escape_rate": MAX_ESCAPE_RATE,
    }
    with tempfile.TemporaryDirectory() as workdir:
        llm = _eval_llm(args.replay, results / "recordings.tar.gz", scratch, Path(workdir))
        with fresh_database(scratch_url("e2e")):
            result = run(
                records,
                spec,
                get_sessionmaker(),
                llm=llm,
                models=models,
                assistant_model=settings.model_assistant,
                embedder=embedder,
                limit=args.limit,
                workers=args.workers,
                progress=print,
            )
    return _save_end_to_end(result, llm, meta, replay=args.replay, limited=args.limit is not None)


def _save_end_to_end(
    result: "Any", llm: "Any", meta: dict[str, Any], *, replay: bool, limited: bool
) -> int:
    """Write the results. A full live run also packs its recordings and renders the report;
    a replay is checked against the committed results."""
    import json

    from accrueboard.eval.archive import pack
    from accrueboard.eval.end_to_end import write_results

    settings = get_settings()
    results = settings.data_dir / "results"
    committed = results / "end-to-end.json"
    if not replay and not limited:
        path = write_results(result, results, meta=meta).replace(committed)
        packed = pack(llm.store, llm.used, results / "recordings.tar.gz")
        print(f"wrote {path} and {packed} recordings")
        return _report(argparse.Namespace())
    path = write_results(result, settings.data_dir / "eval-run", meta=meta)
    print(f"wrote {path}")
    if not replay or not committed.is_file():
        return 0
    fresh = json.loads(path.read_text("utf-8"))
    saved = json.loads(committed.read_text("utf-8"))
    same = all(fresh[k] == saved[k] for k in ("calibration", "validation", "test"))
    print(
        "replay reproduced the committed results exactly"
        if same
        else "replay DIFFERS from the committed results"
    )
    return 0 if same else 1


def _report(_: argparse.Namespace) -> int:
    import json

    from accrueboard.eval.report import render

    settings = get_settings()
    source = settings.data_dir / "results" / "end-to-end.json"
    report = settings.data_dir.parent / "docs" / "eval-results.md"
    report.write_text(render(json.loads(source.read_text("utf-8"))), "utf-8", newline="\n")
    print(f"wrote {report}")
    return 0


def _add_eval_commands(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
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

    assistant = experiments.add_parser(
        "review-assistant",
        help="how often the review assistant recommends the right action (calls the model)",
    )
    assistant.add_argument("--client", default="fernhill")
    assistant.add_argument("--seed", type=int, default=7)
    assistant.add_argument("--split", choices=["validation", "test"], default="validation")
    assistant.add_argument("--limit", type=int, help="stop after this many held documents")
    assistant.add_argument("--embedder", choices=["fast", "hashing"], default="fast")
    assistant.add_argument("--out", help="output directory (default: <data_dir>/eval)")
    assistant.set_defaults(handler=_review_assistant_eval)

    e2e = experiments.add_parser(
        "end-to-end",
        help="the whole system on the validation and test splits (the reported numbers)",
    )
    e2e.add_argument("--client", default="fernhill")
    e2e.add_argument("--seed", type=int, default=7)
    e2e.add_argument(
        "--replay",
        action="store_true",
        help="use the committed recordings (no API key) and check the committed results",
    )
    e2e.add_argument("--limit", type=int, help="only the first N documents of each split")
    e2e.add_argument("--workers", type=int, default=4, help="parallel reads in the warm-up")
    e2e.add_argument("--embedder", choices=["fast", "hashing"], default="fast")
    e2e.set_defaults(handler=_end_to_end)

    report = experiments.add_parser("report", help="re-render docs/eval-results.md")
    report.set_defaults(handler=_report)


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

    bootstrap = commands.add_parser(
        "bootstrap", help="generate the dataset and seed the client if missing (idempotent)"
    )
    bootstrap.add_argument("--client", default="fernhill")
    bootstrap.add_argument("--seed", type=int, default=7)
    bootstrap.set_defaults(handler=_bootstrap)

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

    _add_eval_commands(commands)

    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 0
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())

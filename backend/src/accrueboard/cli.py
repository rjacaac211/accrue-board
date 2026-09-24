"""Command-line entry point: `accrueboard <command>`."""

import argparse
import time
from pathlib import Path

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

    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 0
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())

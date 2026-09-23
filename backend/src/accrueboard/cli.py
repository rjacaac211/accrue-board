"""Command-line entry point: `accrueboard <command>`."""

import argparse

from accrueboard import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="accrueboard", description=__doc__)
    parser.add_argument("--version", action="version", version=f"accrueboard {__version__}")
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

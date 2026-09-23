#!/usr/bin/env python3
"""Block forbidden terms and absolute user paths from entering the repository.

The term list is deliberately NOT stored in the repository. It is read from:
  1. the FORBIDDEN_TERMS environment variable (newline- or comma-separated), else
  2. the untracked file .git/info/forbidden-terms.txt

List format: one term per line, '#' comments allowed. Terms match case-insensitively
as whole words, with spaces matching any run of space/underscore/hyphen (or nothing),
and an optional plural 's'. Prefix a line with 're:' to supply a raw regex instead.

Matches are reported by file, line and term *number*, never the term itself, so CI
logs do not disclose the list.

Usage:
  check_forbidden_terms.py FILE...           scan given files (pre-commit)
  check_forbidden_terms.py --all             scan all tracked files
  check_forbidden_terms.py --history         scan every commit (diffs, messages, refs)
  check_forbidden_terms.py --commit-msg FILE scan a commit message
Add --require-terms to fail when no term list is configured (used in CI).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

PATH_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("windows user path", re.compile(r"(?i)\b[a-z]:[\\/]+users[\\/]+")),
    ("macOS user path", re.compile(r"(?<![\w.])/Users/[A-Za-z]")),
]

BINARY_SNIFF_BYTES = 8192


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout


def _term_to_regex(term: str) -> re.Pattern[str]:
    if term.startswith("re:"):
        return re.compile(term[3:], re.IGNORECASE)
    words = [re.escape(w) for w in re.split(r"[\s_\-]+", term) if w]
    body = r"[\s_\-]*".join(words)
    return re.compile(rf"(?<![a-z0-9]){body}s?(?![a-z0-9])", re.IGNORECASE)


def load_terms() -> list[re.Pattern[str]]:
    raw = os.environ.get("FORBIDDEN_TERMS", "")
    if not raw.strip():
        try:
            list_path = Path(
                _git("rev-parse", "--git-path", "info/forbidden-terms.txt").strip()
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            list_path = Path(".git/info/forbidden-terms.txt")
        if list_path.is_file():
            raw = list_path.read_text(encoding="utf-8")
    lines = [line.strip() for line in raw.splitlines()]
    terms = [
        part.strip()
        for line in lines
        if line and not line.startswith("#")
        for part in ([line] if line.startswith("re:") else line.split(","))
    ]
    return [_term_to_regex(t) for t in terms if t]


def scan_text(text: str, terms: list[re.Pattern[str]]) -> list[tuple[int, str]]:
    """Return (line_number, label) for each hit. Labels never contain the matched term."""
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for idx, pattern in enumerate(terms, start=1):
            if pattern.search(line):
                hits.append((lineno, f"forbidden term #{idx}"))
        for label, pattern in PATH_PATTERNS:
            if pattern.search(line):
                hits.append((lineno, label))
    return hits


def read_text_file(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:BINARY_SNIFF_BYTES]:
        return None
    return data.decode("utf-8", errors="replace")


def scan_files(paths: list[str], terms: list[re.Pattern[str]]) -> int:
    failures = 0
    for name in paths:
        path = Path(name)
        # Scan the file name itself too.
        for _, label in scan_text(name, terms):
            print(f"{name}: {label} in file path")
            failures += 1
        text = read_text_file(path)
        if text is None:
            continue
        for lineno, label in scan_text(text, terms):
            print(f"{name}:{lineno}: {label}")
            failures += 1
    return failures


def scan_history(terms: list[re.Pattern[str]]) -> int:
    failures = 0
    refs = _git("for-each-ref", "--format=%(refname)")
    for lineno, label in scan_text(refs, terms):
        print(f"refs:{lineno}: {label}")
        failures += 1
    log = _git("log", "--all", "-p", "--format=commit %H%n%B", "--no-color")
    commit = "?"
    for line in log.splitlines():
        if line.startswith("commit "):
            commit = line.split()[1][:12]
        for _, label in scan_text(line, terms):
            print(f"commit {commit}: {label}")
            failures += 1
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--all", action="store_true", help="scan all tracked files")
    mode.add_argument(
        "--history", action="store_true", help="scan all commits, messages and refs"
    )
    mode.add_argument("--commit-msg", metavar="FILE", help="scan a commit message file")
    parser.add_argument(
        "--require-terms",
        action="store_true",
        help="fail if no term list is configured",
    )
    parser.add_argument("files", nargs="*")
    args = parser.parse_args(argv)

    terms = load_terms()
    if not terms:
        msg = "no forbidden-terms list configured (FORBIDDEN_TERMS or .git/info/forbidden-terms.txt)"
        if args.require_terms:
            print(f"error: {msg}", file=sys.stderr)
            return 2
        print(f"warning: {msg}; checking paths only", file=sys.stderr)

    if args.history:
        failures = scan_history(terms)
    elif args.commit_msg:
        text = Path(args.commit_msg).read_text(encoding="utf-8", errors="replace")
        hits = scan_text(text, terms)
        for lineno, label in hits:
            print(f"commit message:{lineno}: {label}")
        failures = len(hits)
    elif args.all:
        failures = scan_files(
            [p for p in _git("ls-files", "-z").split("\0") if p], terms
        )
    else:
        failures = scan_files(args.files, terms)

    if failures:
        print(f"\n{failures} forbidden match(es) found.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

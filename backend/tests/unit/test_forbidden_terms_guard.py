"""Tests for scripts/check_forbidden_terms.py, using made-up terms only."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check_forbidden_terms.py"


@pytest.fixture(scope="module")
def guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_forbidden_terms", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_multiword_term_matches_spacing_variants(guard: ModuleType) -> None:
    pattern = guard._term_to_regex("zebra widget")
    for text in ("Zebra Widget", "zebra-widget", "ZebraWidget", "zebra_widgets"):
        assert pattern.search(text), text


def test_term_is_whole_word(guard: ModuleType) -> None:
    pattern = guard._term_to_regex("zebra")
    assert pattern.search("a zebra here")
    assert not pattern.search("zebrafish")
    assert not pattern.search("azebra")


def test_regex_terms_are_supported(guard: ModuleType) -> None:
    pattern = guard._term_to_regex(r"re:acme\d+")
    assert pattern.search("ACME42")


def test_detects_absolute_user_paths(guard: ModuleType) -> None:
    # Built from pieces so this file does not itself trip the guard.
    windows_path = "C:" + r"\Users\someone\x"
    mac_path = "/" + "Users/someone/y"
    hits = guard.scan_text(f"data at {windows_path} and {mac_path}", [])
    labels = {label for _, label in hits}
    assert labels == {"windows user path", "macOS user path"}


def test_hit_labels_never_reveal_the_term(guard: ModuleType) -> None:
    hits = guard.scan_text(
        "line one\nmentions zebra widget", [guard._term_to_regex("zebra widget")]
    )
    assert hits == [(2, "forbidden term #1")]


def test_env_var_terms_take_precedence(guard: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORBIDDEN_TERMS", "alpha, beta\n# comment\ngamma")
    assert len(guard.load_terms()) == 3


def test_comments_with_commas_are_ignored_whole(
    guard: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORBIDDEN_TERMS", "# a comment, with a comma\nalpha\nre:x{1,3}")
    terms = guard.load_terms()
    assert len(terms) == 2
    assert terms[1].search("xxx")

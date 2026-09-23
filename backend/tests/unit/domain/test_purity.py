"""The domain layer must stay pure: no IO, network, database, clock or environment access."""

import ast
from pathlib import Path

import pytest

DOMAIN = Path(__file__).resolve().parents[3] / "src" / "accrueboard" / "domain"

ALLOWED_MODULES = {
    "__future__",
    "calendar",
    "collections",
    "dataclasses",
    "datetime",
    "decimal",
    "enum",
    "math",
    "re",
    "statistics",
    "types",
    "typing",
    "pydantic",
}
FORBIDDEN_CALLS = {"now", "utcnow", "today", "open", "getenv"}


def domain_files() -> list[Path]:
    return sorted(DOMAIN.glob("*.py"))


@pytest.mark.parametrize("path", domain_files(), ids=lambda p: p.name)
def test_domain_imports_only_pure_modules(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        else:
            continue
        for name in names:
            top = name.split(".")[0]
            assert top in ALLOWED_MODULES or name.startswith("accrueboard.domain"), (
                f"{path.name} imports {name}"
            )


@pytest.mark.parametrize("path", domain_files(), ids=lambda p: p.name)
def test_domain_never_reads_the_clock_or_environment(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            assert name not in FORBIDDEN_CALLS, f"{path.name} calls {name}()"

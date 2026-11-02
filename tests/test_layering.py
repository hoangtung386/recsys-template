"""Architecture rules, enforced from the source text (no imports executed)."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "recsys"
ENGINE_ALLOWED = set(sys.stdlib_module_names) | {"numpy", "recsys", "__future__"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def _recsys_targets(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {
        node.module.split(".")[1]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("recsys.")
        and len(node.module.split(".")) > 1
    }


def test_engine_depends_on_numpy_and_stdlib_only() -> None:
    offenders = {
        f"{path.name}: {name}"
        for path in (SRC / "engine").rglob("*.py")
        for name in _imports(path) - ENGINE_ALLOWED
    }
    assert not offenders, (
        f"engine pulled in third-party dependencies: {sorted(offenders)}"
    )


def test_engine_imports_nothing_else_from_recsys() -> None:
    offenders = {
        f"{path.name}: {target}"
        for path in (SRC / "engine").rglob("*.py")
        for target in _recsys_targets(path)
        if target != "engine"
    }
    assert not offenders, sorted(offenders)


def test_monitoring_does_not_depend_on_serving_or_training() -> None:
    offenders = {
        f"{path.name}: {target}"
        for path in (SRC / "monitoring").rglob("*.py")
        for target in _recsys_targets(path)
        if target in {"serving", "training", "registry"}
    }
    assert not offenders, sorted(offenders)


def test_policy_layers_are_pure() -> None:
    offenders = {
        f"{path.parent.name}/{path.name}: {target}"
        for package in ("monitoring", "rollout")
        for path in (SRC / package).rglob("*.py")
        for target in _recsys_targets(path)
        if target in {"serving", "training", "loop", "cli"}
    }
    assert not offenders, sorted(offenders)


def test_nothing_imports_the_cli_or_the_loop_layer() -> None:
    offenders = {
        f"{path.parent.name}/{path.name}: {target}"
        for path in SRC.rglob("*.py")
        if path.parent.name not in {"cli", "loop"}
        for target in _recsys_targets(path)
        if target in {"cli", "loop"}
    }
    assert not offenders, sorted(offenders)


def test_serving_does_not_import_training() -> None:
    offenders = {
        f"{path.name}: {target}"
        for path in (SRC / "serving").rglob("*.py")
        for target in _recsys_targets(path)
        if target in {"training", "registry"}
    }
    assert not offenders, sorted(offenders)

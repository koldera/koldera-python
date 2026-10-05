"""The dependency licence gate (scripts/check_licences.py)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def _load() -> ModuleType:
    path = Path(__file__).resolve().parent.parent / "scripts" / "check_licences.py"
    spec = importlib.util.spec_from_file_location("check_licences", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("MIT", "MIT"),
        ("MIT License", "MIT"),
        ("Apache Software License", "Apache-2.0"),
        ("Apache 2.0", "Apache-2.0"),
        ("BSD License", "BSD-3-Clause"),
        ("Mozilla Public License 2.0 (MPL 2.0)", "MPL-2.0"),
        ("Apache-2.0 OR BSD-2-Clause", "Apache-2.0 OR BSD-2-Clause"),
        ("", ""),
    ],
)
def test_normalise(raw: str, expected: str) -> None:
    assert gate.normalise(raw) == expected


@pytest.mark.parametrize("expression", ["MIT", "Apache-2.0 OR GPL-3.0-only", "(MIT OR GPL-3.0-only) AND BSD-3-Clause", "MPL-2.0"])
def test_allowed_expressions_pass(expression: str) -> None:
    assert gate.failing(expression) is None


@pytest.mark.parametrize(
    ("expression", "bad"),
    [
        ("GPL-3.0-only", "GPL-3.0-only"),
        ("AGPL-3.0-only", "AGPL-3.0-only"),
        ("MIT AND GPL-3.0-only", "GPL-3.0-only"),
        ("GPL-2.0-only OR GPL-3.0-only", "GPL-2.0-only"),
        ("Something custom", "Something"),
    ],
)
def test_disallowed_expressions_fail(expression: str, bad: str) -> None:
    assert gate.failing(expression) == bad


def test_the_runtime_dependencies_of_koldera_pass() -> None:
    dists = gate.runtime_closure()
    names = {d.metadata["Name"].lower() for d in dists}
    assert {"koldera", "httpx", "httpcore", "h11", "certifi", "idna", "anyio"} <= names
    assert gate.check(dists) == []

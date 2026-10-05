"""Every Python example of the README compiles and type-checks (mypy --strict) against the package."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from mypy import api

README = Path(__file__).resolve().parent.parent / "README.md"
BLOCKS = re.findall(r"^```python\n(.*?)^```$", README.read_text(encoding="utf8"), flags=re.S | re.M)


def test_the_readme_has_python_examples() -> None:
    assert len(BLOCKS) == 5


@pytest.mark.parametrize("index", range(len(BLOCKS)))
def test_example_compiles(index: int) -> None:
    compile(BLOCKS[index], f"README example {index + 1}", "exec")


def test_examples_type_check(tmp_path: Path) -> None:
    for index, block in enumerate(BLOCKS):
        (tmp_path / f"example_{index + 1}.py").write_text(block, encoding="utf8")
    stdout, stderr, status = api.run(
        ["--strict", "--python-version", "3.10", "--cache-dir", str(tmp_path / ".mypy_cache"), "--no-error-summary", str(tmp_path)]
    )
    assert status == 0, stdout + stderr

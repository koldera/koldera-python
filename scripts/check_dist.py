"""Checks what `uv build` produced: one wheel and one sdist with the expected contents."""

from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path

SDIST_TOP = {"src", "tests", "contracts", "scripts", "LICENSE", "NOTICE", "README.md", "CHANGELOG.md", "pyproject.toml", "PKG-INFO"}
# hatchling always adds this one.
OPTIONAL = {".gitignore"}


def problems(dist: Path) -> list[str]:
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        return [f"expected one wheel and one sdist in {dist}, found {[p.name for p in wheels + sdists]}"]
    found: list[str] = []
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = wheel.namelist()
    if "koldera/py.typed" not in names:
        found.append("the wheel has no koldera/py.typed")
    for required in ("LICENSE", "NOTICE"):
        if not any(n.endswith(f".dist-info/licenses/{required}") for n in names):
            found.append(f"the wheel has no {required}")
    if any(n.split("/")[0] in ("tests", "scripts", "contracts") for n in names):
        found.append("the wheel contains tests, scripts or contracts")
    with tarfile.open(sdists[0]) as sdist:
        members = sdist.getnames()
    top = {m.split("/")[1] for m in members if "/" in m}
    if top - SDIST_TOP - OPTIONAL:
        found.append(f"unexpected files at the top of the sdist: {sorted(top - SDIST_TOP - OPTIONAL)}")
    if SDIST_TOP - top:
        found.append(f"missing at the top of the sdist: {sorted(SDIST_TOP - top - {'.gitignore'})}")
    return found


def main() -> int:
    found = problems(Path(sys.argv[1] if len(sys.argv) > 1 else "dist"))
    for item in found:
        print(f"::error::{item}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())

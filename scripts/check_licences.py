"""Licence gate over the runtime dependencies of koldera (the packages a user installs with it).

Everything must be on the allow-list below; anything else, or unknown, fails until a person reviews it into
EXCEPTIONS. Development tools are not shipped and are not checked. Run it inside the project environment.
"""

from __future__ import annotations

import re
import sys
from importlib.metadata import Distribution, PackageNotFoundError, distribution
from typing import Any

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = "koldera"

ALLOWED = frozenset(
    {
        "MIT", "MIT-0", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "MPL-2.0", "PSF-2.0", "Python-2.0",
        "0BSD", "Zlib", "Unlicense", "CC0-1.0",
    }
)  # fmt: skip

# Reviewed by hand: canonical package name -> why it is acceptable. Keep short.
EXCEPTIONS: dict[str, str] = {}

_ALIASES = (
    (re.compile(r"apache", re.I), "Apache-2.0"),
    (re.compile(r"^(the )?mit( license)?$", re.I), "MIT"),
    (re.compile(r"new bsd|bsd[- ]3|3-clause", re.I), "BSD-3-Clause"),
    (re.compile(r"simplified bsd|bsd[- ]2|2-clause", re.I), "BSD-2-Clause"),
    (re.compile(r"mozilla public license.*2", re.I), "MPL-2.0"),
    (re.compile(r"^bsd( license)?$", re.I), "BSD-3-Clause"),
    (re.compile(r"python software foundation", re.I), "PSF-2.0"),
)
_EXPRESSION = re.compile(r"^\(?[A-Za-z0-9.+-]+\)?(\s+(AND|OR)\s+\(?[A-Za-z0-9.+-]+\)?)*$")


def normalise(raw: str) -> str:
    """An SPDX id or expression for a metadata value, or the text itself when it is not recognised."""
    text = " ".join(raw.split())
    if not text:
        return ""
    if _EXPRESSION.fullmatch(text):
        return text
    for pattern, spdx in _ALIASES:
        if pattern.search(text):
            return spdx
    return text


def licence_of(dist: Distribution) -> str:
    """License-Expression first, then the License field, then the License classifiers."""
    meta: Any = dist.metadata
    expression = meta.get("License-Expression")
    if expression:
        return normalise(str(expression))
    field = meta.get("License")
    if field and len(str(field)) < 100:
        return normalise(str(field))
    classifiers = [str(c).split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if str(c).startswith("License ::")]
    classifiers = [c for c in classifiers if c != "OSI Approved"]
    return " OR ".join(normalise(c) for c in classifiers) if len(classifiers) == 1 else " AND ".join(normalise(c) for c in classifiers)


def failing(expression: str) -> str | None:
    """The first disallowed id of an SPDX expression ("A OR B" passes when one side does), or None if it passes."""
    tokens = expression.replace("(", " ( ").replace(")", " ) ").split()
    position = 0

    def atom() -> str | None:
        nonlocal position
        token = tokens[position]
        position += 1
        if token == "(":
            inner = either()
            position += 1
            return inner
        return None if token in ALLOWED else token

    def both() -> str | None:
        nonlocal position
        bad = atom()
        while position < len(tokens) and tokens[position] == "AND":
            position += 1
            other = atom()
            bad = bad or other
        return bad

    def either() -> str | None:
        nonlocal position
        bad = both()
        while position < len(tokens) and tokens[position] == "OR":
            position += 1
            other = both()
            bad = None if bad is None or other is None else bad
        return bad

    return either() if tokens else expression or "unknown"


def runtime_closure(root: str = ROOT) -> list[Distribution]:
    """The distribution of ``root`` and everything its requirements pull in on this interpreter (no extras)."""
    environment: Any = default_environment()
    seen: dict[str, Distribution] = {}
    pending = [root]
    while pending:
        name = canonicalize_name(pending.pop())
        if name in seen:
            continue
        try:
            dist = distribution(name)
        except PackageNotFoundError:
            raise SystemExit(f"required package {name} is not installed") from None
        seen[name] = dist
        for line in dist.requires or ():
            requirement = Requirement(line)
            if requirement.marker is None or requirement.marker.evaluate({**environment, "extra": ""}):
                pending.append(requirement.name)
    return list(seen.values())


def check(dists: list[Distribution]) -> list[str]:
    problems = []
    for dist in dists:
        name = canonicalize_name(dist.metadata["Name"])
        if name in EXCEPTIONS:
            continue
        licence = licence_of(dist)
        bad = failing(licence) if licence else "unknown"
        if bad is not None:
            problems.append(f"{name} {dist.version}: licence {licence or 'unknown'!r} is not allowed ({bad})")
    return problems


def main() -> int:
    dists = runtime_closure()
    for dist in sorted(dists, key=lambda d: str(d.metadata["Name"]).lower()):
        print(f"{dist.metadata['Name']} {dist.version}: {licence_of(dist)}")
    problems = check(dists)
    for problem in problems:
        print(f"::error::{problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())

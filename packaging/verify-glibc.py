"""Reject release binaries that require glibc newer than manylinux_2_17."""

from __future__ import annotations

import re
import sys
from pathlib import Path


root = Path(sys.argv[1])
pattern = re.compile(rb"GLIBC_(\d+)\.(\d+)")
maximum = (2, 17)
violations: list[str] = []
checked = 0
for path in root.rglob("*"):
    if not path.is_file() or not (path.suffix in {".so", ".bin"} or path.name.startswith("python3")):
        continue
    try:
        data = path.read_bytes()
    except OSError:
        continue
    versions = {(int(major), int(minor)) for major, minor in pattern.findall(data)}
    if not versions:
        continue
    checked += 1
    required = max(versions)
    if required > maximum:
        violations.append(f"{path.relative_to(root)} requires GLIBC_{required[0]}.{required[1]}")
if violations:
    raise SystemExit("glibc compatibility check failed:\n" + "\n".join(violations))
print(f"glibc compatibility passed: {checked} binaries, maximum GLIBC_2.17")

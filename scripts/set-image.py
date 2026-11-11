#!/usr/bin/env python3
"""Point the manifests at a published thruput-validator release."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

RELEASE_COMPONENT = Path(__file__).resolve().parent.parent / "k8s/components/release/kustomization.yaml"
VERSION = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?")


def set_tag(version: str, path: Path = RELEASE_COMPONENT) -> None:
    version = version.removeprefix("v")
    if not VERSION.fullmatch(version):
        raise ValueError(f"not a release version: {version}")
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(r"(?m)^(\s*newTag:\s*).*$", rf"\g<1>{version}", text)
    if count != 1:
        raise ValueError(f"expected exactly one newTag in {path}, found {count}")
    path.write_text(updated, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="Thruput release, e.g. 0.1.33 or v0.1.33")
    args = parser.parse_args()
    try:
        set_tag(args.version)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

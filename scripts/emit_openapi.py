"""Command Center OpenAPI document generator (P5-5).

Regenerates the committed OpenAPI 3.1 document
(``docs/command-center/openapi-v1.json``) from the P5-5 route registry, which
itself is derived from the P5-2 snapshot manifest. The document is the input
for browser-frontend type/client codegen (``apps/command-center-web``).

Modes:
  --check   verify the committed document matches a fresh regeneration
            (default; exit 1 on drift)
  --emit    rewrite ``docs/command-center/openapi-v1.json``
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from arena_hero_agent.command_center.api import RouteTable, openapi_json

AGENT_REPO = Path(__file__).resolve().parents[1]
OPENAPI_REL = Path("docs/command-center/openapi-v1.json")
OPENAPI_PATH = AGENT_REPO / OPENAPI_REL


def regenerate() -> str:
    """Return the deterministic OpenAPI document for the committed route table."""
    return openapi_json(RouteTable())


def run_check(verbose: bool = False) -> int:
    expected = regenerate()
    if not OPENAPI_PATH.is_file():
        print(f"[fail] OpenAPI document missing: {OPENAPI_PATH}", file=sys.stderr)
        return 1
    actual = OPENAPI_PATH.read_text(encoding="utf-8")
    if actual != expected:
        print(
            f"[fail] OpenAPI document drift: {OPENAPI_PATH.relative_to(AGENT_REPO)}",
            file=sys.stderr,
        )
        return 1
    if verbose:
        print(f"[ok] openapi={OPENAPI_PATH.relative_to(AGENT_REPO)} ({len(expected)} bytes)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify committed document (default)")
    parser.add_argument("--emit", action="store_true", help="rewrite the OpenAPI document")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    if args.emit:
        OPENAPI_PATH.write_text(regenerate(), encoding="utf-8")
        print(f"wrote {OPENAPI_PATH.relative_to(AGENT_REPO)}")
    return run_check(verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())

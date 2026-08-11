"""Command Center snapshot manifest regenerator/checker (P5-2).

Reads the committed machine-readable manifest (docs/command-center/snapshot/
command-center-snapshot-v1.json), re-reads the referenced legacy TS sources
(read-only), and recomputes every SHA-256 plus the deterministic manifest hash.

Modes:
  --check      verify committed manifest is reproducible (default; exit 1 on drift)
  --refresh    recompute fixture hashes + manifest hash and rewrite the manifest
  --emit-docs  write the derived endpoints/fixtures markdown views from the manifest

The manifest is the single source of truth for snapshot facts; this script only
recomputes hashes and validates structure. It never copies TS implementation
source into this repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

AGENT_REPO = Path(__file__).resolve().parents[1]
DOCS_DIR = AGENT_REPO / "docs" / "command-center"
MANIFEST_REL = Path("docs/command-center/snapshot/command-center-snapshot-v1.json")
MANIFEST_PATH = AGENT_REPO / MANIFEST_REL

TS_SERVER_REL = Path("packages/command-center/server.ts")
TS_REPO_MARKER = Path("packages/command-center/server.ts")

# app.<method>("<path>", ...) route registrations (covers GET/POST/DELETE/etc.)
ROUTE_RE = re.compile(r'app\.(get|post|put|patch|delete|use)\(\s*"([^"]+)"')


def find_ts_repo() -> Path:
    env = os.environ.get("ARENA_HERO_AGENT_TS_ROOT", "").strip()
    if env:
        cand = Path(env).expanduser().resolve()
        if (cand / TS_REPO_MARKER).is_file():
            return cand
    for parent in [AGENT_REPO, *AGENT_REPO.parents]:
        cand = parent / "arena-hero-agent-ts"
        if (cand / TS_REPO_MARKER).is_file():
            return cand
        if parent == parent.parent:
            break
    raise FileNotFoundError(
        "arena-hero-agent-ts checkout not found: set ARENA_HERO_AGENT_TS_ROOT "
        "or place the checkout beside this repository"
    )


_TS_REPO: Path | None = None


def get_ts_repo() -> Path:
    """Resolve the legacy TS checkout lazily (route/fixture checks only).

    Resolution happens on first use so importing this module stays valid in
    checkouts without the TS repo (for example CI); callers that genuinely
    need the TS sources surface the FileNotFoundError with a clear message.
    """
    global _TS_REPO
    if _TS_REPO is None:
        _TS_REPO = find_ts_repo()
    return _TS_REPO


def sha256_file(path: Path) -> str:
    """Return the canonical SHA-256 of a text fixture (LF-normalized).

    Repositories normalize text files to LF (see .gitattributes), but Windows
    checkouts materialize CRLF working copies. Hashing the LF-normalized bytes
    keeps the manifest reproducible across platforms and checkouts; every
    referenced fixture is a UTF-8 JSON text file.
    """
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk.replace(b"\r\n", b"\n"))
    return digest.hexdigest().upper()


def load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def compute_manifest_hash(manifest: dict) -> str:
    body = {k: v for k, v in manifest.items() if k != "manifest_hash"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_ts_routes(server_ts: Path) -> set[tuple[str, str]]:
    text = server_ts.read_text(encoding="utf-8")
    return {(m.group(1).upper(), m.group(2)) for m in ROUTE_RE.finditer(text)}


def manifest_route_set(manifest: dict) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for group in ("routes", "static_routes"):
        for entry in manifest.get(group, []):
            out.add((entry["method"], entry["path"]))
    return out


def repo_root_for(repo: str) -> Path:
    if repo == "arena-hero-agent-ts":
        return get_ts_repo()
    if repo == "arena-hero-agent":
        return AGENT_REPO
    raise ValueError(f"unknown fixture repo: {repo}")


def check_routes(manifest: dict) -> list[str]:
    problems: list[str] = []
    expected = parse_ts_routes(get_ts_repo() / TS_SERVER_REL)
    actual = manifest_route_set(manifest)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        problems.append(f"routes present in server.ts but missing from manifest: {missing}")
    if extra:
        problems.append(f"routes present in manifest but not in server.ts: {extra}")
    api = [r for r in manifest["routes"] if r["path"].startswith("/api/")]
    static = manifest["static_routes"]
    if len(api) != 66:
        problems.append(f"expected 66 api routes, got {len(api)}")
    if len(static) != 5:
        problems.append(f"expected 5 static routes, got {len(static)}")
    return problems


def recompute_fixture_hashes(manifest: dict) -> dict[str, str]:
    """Return {path: sha256} recomputed from the referenced sources."""
    recomputed: dict[str, str] = {}
    for fx in manifest["fixtures"]:
        root = repo_root_for(fx["repo"])
        path = root / fx["path"]
        recomputed[fx["path"]] = sha256_file(path)
    return recomputed


def check_fixtures(manifest: dict) -> list[str]:
    problems: list[str] = []
    recomputed = recompute_fixture_hashes(manifest)
    for fx in manifest["fixtures"]:
        expected = recomputed[fx["path"]]
        if fx["sha256"] != expected:
            problems.append(
                f"fixture hash drift {fx['repo']}:{fx['path']}: "
                f"manifest={fx['sha256']} actual={expected}"
            )
    return problems


def refresh_fixture_hashes(manifest: dict) -> None:
    recomputed = recompute_fixture_hashes(manifest)
    for fx in manifest["fixtures"]:
        fx["sha256"] = recomputed[fx["path"]]


def _fixture_row(fx: dict) -> str:
    return (
        f"| `{fx['path']}` | `{fx['sha256']}` | {fx['source']} | "
        f"{fx['tenant_isolation']} | {fx['purpose']} |"
    )


def render_endpoints_md(manifest: dict) -> str:
    lines = [
        "# Command Center endpoint inventory (P5-2 snapshot)",
        "",
        "> Derived from `docs/command-center/snapshot/command-center-snapshot-v1.json`;",
        "  do not edit by hand.",
        "> Regenerate with `uv run python scripts/snapshot_command_center.py --emit-docs`.",
        "",
        "## API routes",
        "",
        "| # | Method | Path | Tenant | ETag | Stream | Cache | Write semantics |",
        "|---|--------|------|--------|------|--------|-------|-----------------|",
    ]
    for i, r in enumerate(manifest["routes"], start=1):
        tenant = r["tenant_param"] or "-"
        etag = r["etag"] or "-"
        cache = r["cache"] or "-"
        row = (
            f"| {i} | {r['method']} | `{r['path']}` | {tenant} | {etag} | "
            f"{r['stream_kind']} | {cache} | {r['write_semantics']} |"
        )
        lines.append(row)
    lines += ["", "## Static routes", "", "| Method | Path |", "|--------|------|"]
    for r in manifest["static_routes"]:
        lines.append(f"| {r['method']} | `{r['path']}` |")
    lines += [
        "",
        "## Stream characteristics",
        "",
        "- All Command Center routes are request/response JSON polling (`poll-json`);",
        "  there is no SSE or WebSocket endpoint in the Command Center server.",
        "- `/api/stream` returns a bounded tail of the per-tenant decision stream",
        "  (`n` clamped to 1..200); the name is legacy, the transport is plain JSON polling.",
        "- The WebSocket wire contract lives in the `arena-hero-ts` package",
        "  (`stream-envelope` schema, tick/state/received messages); see the fixture",
        "  inventory for the schema hashes.",
        "- Only `/api/map` uses an HTTP-level weak ETag (`W/<map-sig>`) with",
        "  `cache-control: public, max-age=2` and 304 handling; all other endpoints rely",
        "  on in-memory TTL caches or direct reads.",
        "",
        f"Counts: {len(manifest['routes'])} API routes, "
        f"{len(manifest['static_routes'])} static routes.",
        "",
    ]
    return "\n".join(lines)


def render_fixtures_md(manifest: dict) -> str:
    lines = [
        "# Command Center fixture & golden-data inventory (P5-2 snapshot)",
        "",
        "> Derived from `docs/command-center/snapshot/command-center-snapshot-v1.json`;",
        "  do not edit by hand.",
        "> Regenerate with `uv run python scripts/snapshot_command_center.py --emit-docs`.",
        "",
        "Hashes are SHA-256 over the exact files at the referenced repository path.",
        "",
        "## Wire contracts (arena-hero-ts)",
        "",
        "| Path | SHA-256 | Source | Tenant isolation | Purpose |",
        "|------|---------|--------|------------------|---------|",
    ]
    for fx in manifest["fixtures"]:
        if fx["repo"] != "arena-hero-agent-ts" or "/arena-hero-ts/" not in "/" + fx["path"]:
            continue
        lines.append(_fixture_row(fx))
    lines += [
        "",
        "## TS agent fixtures (arena-agent)",
        "",
        "| Path | SHA-256 | Source | Tenant isolation | Purpose |",
        "|------|---------|--------|------------------|---------|",
    ]
    for fx in manifest["fixtures"]:
        if "/arena-agent/" not in "/" + fx["path"]:
            continue
        lines.append(_fixture_row(fx))
    lines += [
        "",
        "## Python known-answer golden data (arena-hero-agent)",
        "",
        "| Path | SHA-256 | Source | Tenant isolation | Purpose |",
        "|------|---------|--------|------------------|---------|",
    ]
    for fx in manifest["fixtures"]:
        if fx["repo"] != "arena-hero-agent":
            continue
        lines.append(_fixture_row(fx))
    lines += ["", f"Total: {len(manifest['fixtures'])} fixtures.", ""]
    return "\n".join(lines)


def run_check(verbose: bool = False) -> int:
    manifest = load_manifest()
    problems: list[str] = []
    problems += check_routes(manifest)
    problems += check_fixtures(manifest)
    computed = compute_manifest_hash(manifest)
    if computed != manifest.get("manifest_hash"):
        problems.append(
            f"manifest hash drift: committed={manifest.get('manifest_hash')} recomputed={computed}"
        )
    for path in (DOCS_DIR / "endpoints-v1.md", DOCS_DIR / "fixtures-v1.md"):
        if not path.is_file():
            problems.append(f"derived doc missing: {path}")
    if problems:
        for p in problems:
            print(f"[fail] {p}", file=sys.stderr)
        return 1
    if verbose:
        summary = (
            f"[ok] routes={len(manifest['routes'])} "
            f"static={len(manifest['static_routes'])} fixtures={len(manifest['fixtures'])}"
        )
        print(summary)
        print(f"[ok] manifest_hash={computed}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify committed manifest (default)")
    parser.add_argument(
        "--refresh", action="store_true", help="recompute hashes and rewrite the manifest"
    )
    parser.add_argument(
        "--emit-docs", action="store_true", help="rewrite derived endpoints/fixtures markdown"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    manifest = load_manifest()
    if args.refresh:
        refresh_fixture_hashes(manifest)
        manifest["manifest_hash"] = compute_manifest_hash(manifest)
        MANIFEST_PATH.write_text(
            json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"refreshed {MANIFEST_PATH.relative_to(AGENT_REPO)}")
    if args.emit_docs:
        for rel, render in (
            (DOCS_DIR / "endpoints-v1.md", render_endpoints_md),
            (DOCS_DIR / "fixtures-v1.md", render_fixtures_md),
        ):
            rel.write_text(render(manifest), encoding="utf-8")
            print(f"wrote {rel.relative_to(AGENT_REPO)}")
    return run_check(verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())

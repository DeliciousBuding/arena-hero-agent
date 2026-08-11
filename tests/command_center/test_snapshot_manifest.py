"""Frozen validation for the P5-2 Command Center snapshot manifest.

The manifest at docs/command-center/snapshot/command-center-snapshot-v1.json is
the single source of truth for the extracted Command Center contracts. These
tests pin it: regeneration must reproduce the identical manifest hash, the route
inventory must match the legacy server registrations, every fixture hash must
match its source file, derived docs must not drift, and the snapshot must be
free of copied TypeScript source, private paths, credentials, model names, and
session text.
"""

from __future__ import annotations

import json
import re

import pytest

from scripts.snapshot_command_center import (
    AGENT_REPO,
    DOCS_DIR,
    MANIFEST_PATH,
    compute_manifest_hash,
    find_ts_repo,
    load_manifest,
    parse_ts_routes,
    refresh_fixture_hashes,
    render_endpoints_md,
    render_fixtures_md,
    sha256_file,
)


def _ts_repo_or_none():
    """Return the legacy TS checkout when available, else None (tests skip)."""
    try:
        return find_ts_repo()
    except FileNotFoundError:
        return None


MANIFEST = load_manifest()
ROUTES = MANIFEST["routes"]
STATIC_ROUTES = MANIFEST["static_routes"]
FIXTURES = MANIFEST["fixtures"]
BASELINE = MANIFEST["write_api_baseline"]


def _snapshot_text() -> str:
    chunks: list[str] = []
    for path in DOCS_DIR.rglob("*"):
        if path.is_file() and path.suffix in {".md", ".json"}:
            chunks.append(path.read_text(encoding="utf-8"))
    return "\n".join(chunks)


def test_manifest_regenerates_identical_hash() -> None:
    """Recomputing the manifest hash from committed content must be stable."""
    assert compute_manifest_hash(MANIFEST) == MANIFEST["manifest_hash"]


def test_manifest_hash_is_deterministic_across_refresh() -> None:
    """A refresh (recompute hashes) must not change the committed manifest.

    Recomputing fixture hashes reads the referenced sources, including the
    legacy TS checkout; skip when that checkout is not available.
    """
    if _ts_repo_or_none() is None:
        pytest.skip("arena-hero-agent-ts checkout not available in this environment")
    regenerated = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    refresh_fixture_hashes(regenerated)
    assert compute_manifest_hash(regenerated) == MANIFEST["manifest_hash"]


def test_route_inventory_matches_ts_server() -> None:
    ts_repo = _ts_repo_or_none()
    if ts_repo is None:
        pytest.skip("arena-hero-agent-ts checkout not available in this environment")
    server_routes = parse_ts_routes(ts_repo / "packages/command-center/server.ts")
    manifest_routes = {(r["method"], r["path"]) for r in ROUTES} | {
        (r["method"], r["path"]) for r in STATIC_ROUTES
    }
    assert manifest_routes == server_routes


def test_route_counts_frozen() -> None:
    api = [r for r in ROUTES if r["path"].startswith("/api/")]
    assert len(api) == 66
    assert len(STATIC_ROUTES) == 5
    methods = {r["method"] for r in ROUTES} | {r["method"] for r in STATIC_ROUTES}
    assert methods <= {"GET", "POST", "DELETE"}


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: f"{r['method']} {r['path']}")
def test_api_route_entry_schema(route: dict) -> None:
    assert route["path"].startswith("/api/")
    assert route["method"] in {"GET", "POST", "DELETE"}
    for key in ("etag", "cache", "stream_kind", "write_semantics", "tenant_param", "query"):
        assert key in route, f"missing field {key} on {route['method']} {route['path']}"
    assert route["write_semantics"]
    assert route["stream_kind"] == "poll-json"
    # loopback surface: no absolute or external URLs in route paths
    assert not re.search(r"https?://", route["path"])


@pytest.mark.parametrize("route", STATIC_ROUTES, ids=lambda r: f"{r['method']} {r['path']}")
def test_static_route_entry_schema(route: dict) -> None:
    assert not route["path"].startswith("/api/")
    assert route["method"] == "GET"
    assert route["write_semantics"] == "read-only"


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f"{f['repo']}:{f['path']}")
def test_fixture_hash_matches_source(fixture: dict) -> None:
    if fixture["repo"] == "arena-hero-agent-ts":
        ts_repo = _ts_repo_or_none()
        if ts_repo is None:
            pytest.skip("arena-hero-agent-ts checkout not available in this environment")
        root = ts_repo
    else:
        root = AGENT_REPO
    assert sha256_file(root / fixture["path"]) == fixture["sha256"]


def test_fixture_inventory_nonempty() -> None:
    assert len(FIXTURES) >= 20
    assert all(f["sha256"] for f in FIXTURES)
    assert all(f["source"] and f["tenant_isolation"] and f["purpose"] for f in FIXTURES)


def test_write_baseline_schema() -> None:
    for key in (
        "loopback_only",
        "authentication",
        "default_deny",
        "csrf",
        "replay",
        "writes",
        "credential_hygiene",
        "gap_targets_p59",
    ):
        assert key in BASELINE, f"missing write_api_baseline.{key}"
    assert BASELINE["loopback_only"]["bind"] == "127.0.0.1"
    assert BASELINE["authentication"]["present"] is False
    assert BASELINE["default_deny"]["present"] is False
    assert len(BASELINE["writes"]) >= 10
    assert len(BASELINE["gap_targets_p59"]) >= 3


def test_write_endpoints_exist_in_route_inventory() -> None:
    routes = {(r["method"], r["path"]) for r in ROUTES}
    for write in BASELINE["writes"]:
        method, path = write["endpoint"].split(" ", 1)
        assert (method, path) in routes, f"write endpoint not a route: {write['endpoint']}"


def test_derived_docs_match_manifest() -> None:
    assert (DOCS_DIR / "endpoints-v1.md").read_text(encoding="utf-8") == render_endpoints_md(
        MANIFEST
    )
    assert (DOCS_DIR / "fixtures-v1.md").read_text(encoding="utf-8") == render_fixtures_md(MANIFEST)


def test_snapshot_contains_no_ts_source() -> None:
    ts_files = [
        p
        for p in DOCS_DIR.rglob("*")
        if p.is_file() and p.suffix in {".ts", ".tsx", ".js", ".mjs", ".cjs", ".jsx"}
    ]
    assert ts_files == []
    text = _snapshot_text()
    for marker in (
        "import {",
        "node:sqlite",
        "node:fs",
        "Hono",
        "app.get(",
        "=>",
        "interface ",
        "function ",
        "export ",
    ):
        assert marker not in text, f"TS implementation marker present: {marker!r}"


FORBIDDEN_PRIVATE_PATH = re.compile(
    r"[A-Za-z]:[\\/]|\\\\\?\\|/home/|/Users/|/mnt/|/root/|/var/www|C:/"
)
FORBIDDEN_CREDENTIAL = re.compile(
    r"(?i)(api[_-]?key\s*[=:]\s*['\"][^'\"]+|"
    r"password\s*[=:]\s*['\"][^'\"]+|"
    r"secret\s*[=:]\s*['\"][^'\"]+|"
    r"Authorization\s*:\s*Bearer\s+[A-Za-z0-9._-]{8,}|"
    r"simkey-[0-9a-f]{8,}|"
    r"x-shop-cookie\s*[=:]\s*['\"][^'\"]{8,}|"
    r"arena_shop_csrf\s*=\s*[^;\s]{8,})"
)
FORBIDDEN_MODEL = re.compile(r"['\"]?model['\"]?\s*[:=]\s*['\"][^'\"]+['\"]")
FORBIDDEN_SESSION = re.compile(
    r"rollout-[0-9a-f-]+|019f[0-9a-f]{10,}|"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def test_no_private_paths_in_snapshot() -> None:
    text = _snapshot_text()
    hits = FORBIDDEN_PRIVATE_PATH.findall(text)
    assert not hits, f"private path markers found: {sorted(set(hits))}"


def test_no_credentials_in_snapshot() -> None:
    text = _snapshot_text()
    hits = FORBIDDEN_CREDENTIAL.findall(text)
    assert not hits, f"credential markers found: {sorted(set(hits))}"


def test_no_model_names_in_snapshot() -> None:
    text = _snapshot_text()
    hits = FORBIDDEN_MODEL.findall(text)
    assert not hits, f"model-field markers found: {sorted(set(hits))}"


def test_no_session_text_in_snapshot() -> None:
    text = _snapshot_text()
    hits = FORBIDDEN_SESSION.findall(text)
    assert not hits, f"session/id markers found: {sorted(set(hits))}"


def test_manifest_check_passes_end_to_end() -> None:
    """The generator's own check must pass with zero problems.

    The full check recomputes route and fixture hashes from the legacy TS
    checkout; skip when that checkout is not available.
    """
    if _ts_repo_or_none() is None:
        pytest.skip("arena-hero-agent-ts checkout not available in this environment")
    from scripts.snapshot_command_center import run_check

    assert run_check(verbose=False) == 0


def test_scope_provenance_frozen() -> None:
    scope = MANIFEST["scope"]
    assert scope["ts_commit_sha"] == "8cf5cbbcccf396a8feee94404af44969c5388e15"
    assert scope["agent_baseline_commit"] == "0cd1580"
    assert scope["sanitized"]["no_ts_source_copy"] is True
    assert scope["sanitized"]["no_private_paths"] is True
    assert scope["sanitized"]["no_credentials"] is True
    assert scope["sanitized"]["no_model_names"] is True

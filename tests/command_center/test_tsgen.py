"""TypeScript codegen golden tests (P5-7).

tsgen.py must be deterministic (same ``build_openapi`` input -> byte-identical
output), cover every P5-5 route, and fail fast on schema constructs it cannot
map to TypeScript instead of silently degrading. These tests pin:

- the committed artifacts under ``command_center/generated/ts/`` regenerate
  identically and their combined hash is stable (drift guard);
- key types (tenant enums, stream params, path-param delete, ETag map client,
  write-route clients) are present and shaped correctly;
- unknown/unsupported schemas raise :class:`TsGenError` (fail-fast).
"""

from __future__ import annotations

import hashlib

import pytest

from arena_hero_agent.command_center.api import RouteTable, build_openapi
from arena_hero_agent.command_center.api.tsgen import (
    GENERATED_DIR,
    TsGenError,
    generate,
    generate_client_ts,
    generate_types_ts,
)

TABLE = RouteTable()
DOC = build_openapi(TABLE)

GOLDEN_SHA256 = "20eb2981d0b3d49c9d8fe13f5160e37b8eab9b5d374a2cb04ba4b913aaa961df"


def _artifact_hash(artifacts: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for name in ("types.ts", "client.ts"):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(artifacts[name].encode("utf-8"))
    return digest.hexdigest()


def test_golden_hash_is_pinned() -> None:
    assert _artifact_hash(generate(DOC)) == GOLDEN_SHA256


def test_generation_is_deterministic() -> None:
    assert generate(DOC) == generate(build_openapi(RouteTable()))


def test_committed_artifacts_match_regeneration() -> None:
    artifacts = generate(DOC)
    for name, content in artifacts.items():
        disk = (GENERATED_DIR / name).read_text(encoding="utf-8").replace("\r\n", "\n")
        assert disk == content


def test_route_coverage_is_complete() -> None:
    """Every P5-5 API route gets a Params interface and a Response alias."""
    types_ts = generate_types_ts(DOC)
    expected = {f"{op['operationId']}" for path in DOC["paths"].values() for op in path.values()}
    assert len(expected) == 66
    for op_id in sorted(expected):
        type_name = op_id[0].upper() + op_id[1:]
        assert f"interface {type_name}Params" in types_ts
        assert f"type {type_name}Response" in types_ts


def test_tenant_enum_types_derived() -> None:
    types_ts = generate_types_ts(DOC)
    assert 'export type TenantWithAll = "all" | "t1" | "t2" | "t3" | "t4";' in types_ts
    assert 'export type Tenant = "t1" | "t2" | "t3" | "t4";' in types_ts


def test_stream_params_shaped() -> None:
    types_ts = generate_types_ts(DOC)
    assert "export interface GetStreamParams {" in types_ts
    assert "tenant?: Tenant;" in types_ts
    assert "n?: number;" in types_ts
    # 200 schema landed: the response is no longer unknown.
    assert "export type GetStreamResponse = unknown;" not in types_ts
    assert "generatedAt: string" in types_ts
    assert "tenant: string" in types_ts


def test_deeds_journal_params_shaped() -> None:
    types_ts = generate_types_ts(DOC)
    assert "export interface GetDeedsJournalParams {" in types_ts
    for field in (
        "tenant?: TenantWithAll;",
        "window?: number;",
        "category?: string;",
        "minStar?: number;",
    ):
        assert field in types_ts


def test_path_param_delete_shaped() -> None:
    types_ts = generate_types_ts(DOC)
    assert "export interface DeleteRegistryAgentsIdParams {" in types_ts
    assert "id: string;" in types_ts


def test_map_client_uses_etag() -> None:
    client_ts = generate_client_ts(DOC)
    assert (
        "export async function getMap<T = GetMapResponse>(opts: CcRequestOptions = {}): Promise<T | null> {"  # noqa: E501 - emitted TS line
        in client_ts
    )
    assert "ccGetEtag<T>" in client_ts


def test_typed_client_functions_present() -> None:
    client_ts = generate_client_ts(DOC)
    for fn in (
        "export async function getStream<T = GetStreamResponse>(params: GetStreamParams = {}",  # noqa: E501 - emitted TS line
        "export async function postCommand<T = PostCommandResponse>(body: unknown = undefined",  # noqa: E501 - emitted TS line
        "export async function deleteRegistryAgentsId<T = DeleteRegistryAgentsIdResponse>(params: DeleteRegistryAgentsIdParams",  # noqa: E501 - emitted TS line
        "export async function getShopMe<T = GetShopMeResponse>(opts: CcRequestOptions = {}",  # noqa: E501 - emitted TS line
        "export async function getAllianceAdvice<T = GetAllianceAdviceResponse>(opts: CcRequestOptions = {}",  # noqa: E501 - emitted TS line
    ):
        assert fn in client_ts


def _synthetic_doc(path_schema: dict) -> dict:
    return {
        "openapi": "3.1.0",
        "info": {"title": "synthetic", "version": "0.0.0"},
        "paths": {
            "/api/test": {
                "get": {
                    "operationId": "getTest",
                    "parameters": [
                        {"name": "q", "in": "query", "required": False, "schema": path_schema}
                    ],
                    "responses": {
                        "200": {
                            "description": "OK",
                            "content": {"application/json": {"schema": {}}},
                        }
                    },
                }
            }
        },
    }


@pytest.mark.parametrize(
    "bad_schema",
    [
        {"not": {"type": "string"}},
        {"type": "array"},
        {"type": "object", "patternProperties": {"^x": {"type": "string"}}},
        {"type": "weird"},
        {"enum": []},
        {"type": "string", "enum": [1]},
        {"$ref": "#/components/schemas/Missing"},
        {"$ref": "#/definitions/Nope"},
        {"type": "array", "items": {"type": "string"}, "unknownKeyword": True},
    ],
)
def test_unknown_schema_fails_fast(bad_schema: dict) -> None:
    with pytest.raises(TsGenError):
        generate_types_ts(_synthetic_doc(bad_schema))


def test_circular_ref_fails_fast() -> None:
    doc = _synthetic_doc({"$ref": "#/components/schemas/Node"})
    doc["components"] = {
        "schemas": {
            "Node": {
                "type": "object",
                "properties": {"next": {"$ref": "#/components/schemas/Node"}},
            }
        }
    }
    with pytest.raises(TsGenError, match="circular"):
        generate_types_ts(doc)


def test_empty_schema_maps_to_unknown() -> None:
    assert "export type GetTestResponse = unknown;" in generate_types_ts(_synthetic_doc({}))


def test_object_and_component_schemas_supported() -> None:
    doc = _synthetic_doc({"$ref": "#/components/schemas/Widget"})
    doc["components"] = {
        "schemas": {
            "Widget": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "count": {"type": "integer", "nullable": True},
                },
                "required": ["id"],
                "additionalProperties": False,
            }
        }
    }
    types_ts = generate_types_ts(doc)
    assert (
        "export interface GetTestParams {\n  q?: {count?: number | null; id: string};\n}"
        in types_ts
    )


def test_client_groups_by_tag_and_covers_writes() -> None:
    client_ts = generate_client_ts(DOC)
    assert "/* ---- tag: shop ---- */" in client_ts
    assert "/* ---- tag: command ---- */" in client_ts
    # write routes are represented as POST/DELETE clients
    assert "export async function postShopOrder<T = PostShopOrderResponse>" in client_ts
    assert "export async function postRedeem<T = PostRedeemResponse>" in client_ts
    assert "export async function deleteCommand<T = DeleteCommandResponse>" in client_ts

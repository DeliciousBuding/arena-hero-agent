"""OpenAPI 3.1 document generation from the route registry (P5-5).

The generated document is the contract the browser frontend types and client
code are generated from (``apps/command-center-web/README.md``): every API
route from the P5-2 snapshot becomes a path operation with its query/path
parameters and responses, the weak-ETag/304 handling of ``/api/map`` is
documented with headers, and the snapshot facts (cache, stream kind, write
semantics, tenant parameter, ETag) are preserved per operation under
``x-command-center``. Serialization is deterministic (sorted keys) so the
committed artifact regenerates byte-identical.
"""

from __future__ import annotations

import importlib.metadata
import json
import re
from typing import Any

from .routes import ETAG_PREFIX, ETAG_SUFFIX, MAP_CACHE_CONTROL, Route, RouteTable, int_query_keys

OPENAPI_VERSION = "3.1.0"
DEFAULT_API_VERSION = "0.1.0"

_STR = {"type": "string"}
_INT = {"type": "integer"}
_NUM = {"type": "number"}
_NULLABLE_INT = {"type": "integer", "nullable": True}
_NULLABLE_NUM = {"type": "number", "nullable": True}
_OBJ = {"type": "object"}
_OBJ_ROWS = {"type": "array", "items": {"type": "object"}}
_INT_MAP = {"type": "object", "additionalProperties": {"type": "integer"}}
_STR_MAP = {"type": "object", "additionalProperties": {"type": "string"}}

# Shared per-tenant audit shapes. The tenant=all responses for the audit
# endpoints are maps keyed by tenant; single-tenant responses are the shape
# itself, so those operations document both via oneOf.
_DECISION_AUDIT_SHAPE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "generatedAt": _STR,
        "tenant": _STR,
        "window": _INT,
        "currentTick": _NULLABLE_INT,
        "decision": {
            "type": "object",
            "properties": {
                "records": _INT,
                "actionMix": _INT_MAP,
                "intentTop": {"type": "array", "items": _STR},
                "sourceMix": _INT_MAP,
                "planChurn": {"type": "object", "nullable": True},
                "stallTicks": _INT,
            },
            "required": ["records", "actionMix", "intentTop", "sourceMix", "stallTicks"],
        },
        "outcome": {
            "type": "object",
            "properties": {
                "records": _INT,
                "coreDeltaSum": _NUM,
                "coreDeltaPositiveTicks": _INT,
                "depositSucceeded": _INT,
                "depositFailed": _INT,
                "harvestSucceeded": _INT,
                "harvestFailed": _INT,
                "depositSuccessRate": _NULLABLE_NUM,
                "cargoEfficiency": _NULLABLE_NUM,
                "workerMeanDistFromCore": _NULLABLE_NUM,
                "humanApplied": _INT,
                "humanRejected": _INT,
            },
            "required": [
                "records",
                "coreDeltaSum",
                "depositSucceeded",
                "depositFailed",
                "harvestSucceeded",
                "harvestFailed",
            ],
        },
        "cachedAt": _STR,
    },
    "required": ["generatedAt", "tenant", "window", "decision", "outcome"],
}

_HUMAN_CONFLICT_SHAPE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "generatedAt": _STR,
        "tenant": _STR,
        "window": _INT,
        "currentTick": _NULLABLE_INT,
        "applied": _INT,
        "rejected": _INT,
        "rejectedRate": _NULLABLE_NUM,
        "topRejectedReasons": _OBJ_ROWS,
        "commandKinds": _INT_MAP,
        "cachedAt": _STR,
    },
    "required": ["generatedAt", "tenant", "window", "applied", "rejected"],
}


# Minimal 200 response schemas for the main Command Center read endpoints.
# Field names mirror the real payloads produced by the P5-4 projections and the
# request pipeline; nested payload rows stay generic where a projection emits
# heterogeneous records. Every route without a dedicated entry falls back to a
# non-empty object schema, so no operation documents an empty (``unknown``)
# response type and generated TS clients stop being ``unknown``.
_RESPONSE_SCHEMAS: dict[tuple[str, str], dict[str, Any]] = {
    ("GET", "/api/stream"): {
        "type": "object",
        "properties": {
            "tenant": _STR,
            "generatedAt": _STR,
            "rows": {"type": "array", "items": _OBJ},
        },
        "required": ["tenant", "generatedAt", "rows"],
    },
    ("GET", "/api/map"): _OBJ,
    ("GET", "/api/map/lod"): {
        "type": "object",
        "properties": {
            "generatedAt": _STR,
            "tenant": _STR,
            "chunkSize": _INT,
            "chunks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "cx": _INT,
                        "cy": _INT,
                        "tenant": _STR,
                        "resourceCount": _INT,
                        "obstacleCount": _INT,
                        "coreCount": _INT,
                        "lastTick": _INT,
                    },
                },
            },
            "cachedAt": _STR,
        },
        "required": ["generatedAt", "tenant", "chunkSize", "chunks", "cachedAt"],
    },
    ("GET", "/api/alliance/snapshot"): {
        "type": "object",
        "properties": {
            "generatedAt": _STR,
            "cachedAt": _STR,
            "currentTick": _INT,
            "revision": _INT,
            "members": {"type": "object", "additionalProperties": _OBJ},
            "sightings": _OBJ_ROWS,
            "counts": {
                "type": "object",
                "properties": {
                    "currentVisibleCombat": _INT,
                    "recentUniqueCombat": _INT,
                    "historicalSightingCount": _INT,
                    "estimatedForce": _NUM,
                },
                "required": [
                    "currentVisibleCombat",
                    "recentUniqueCombat",
                    "historicalSightingCount",
                    "estimatedForce",
                ],
            },
            "intel": {
                "type": "object",
                "properties": {
                    "currentTick": _INT,
                    "memberReports": _OBJ_ROWS,
                    "currentlyVisible": _OBJ_ROWS,
                    "recentFused": _OBJ_ROWS,
                    "historicalKnown": _OBJ_ROWS,
                    "counts": {
                        "type": "object",
                        "properties": {
                            "currentEnemyUnits": _INT,
                            "currentEnemyCores": _INT,
                            "recentEnemyUnits": _INT,
                            "recentEnemyCores": _INT,
                            "historicalEnemyUnits": _INT,
                            "historicalEnemyCores": _INT,
                        },
                        "required": [
                            "currentEnemyUnits",
                            "currentEnemyCores",
                            "recentEnemyUnits",
                            "recentEnemyCores",
                            "historicalEnemyUnits",
                            "historicalEnemyCores",
                        ],
                    },
                },
                "required": [
                    "currentTick",
                    "memberReports",
                    "currentlyVisible",
                    "recentFused",
                    "historicalKnown",
                    "counts",
                ],
            },
            "threat": {
                "type": "object",
                "properties": {
                    "topCells": _OBJ_ROWS,
                    "cellCount": _INT,
                    "maxDirect": {"type": "object", "nullable": True},
                    "estimatedCombatForce": _NUM,
                    "tickWindow": {"type": "array", "items": _INT, "minItems": 2, "maxItems": 2},
                    "generatedAtMs": _INT,
                },
                "required": [
                    "topCells",
                    "cellCount",
                    "maxDirect",
                    "estimatedCombatForce",
                    "tickWindow",
                    "generatedAtMs",
                ],
            },
            "threatSummaries": _OBJ_ROWS,
            "treasuryTenant": _STR,
            "leaderboardAggression": {
                "type": "object",
                "additionalProperties": {"type": "number"},
            },
        },
        "required": [
            "generatedAt",
            "cachedAt",
            "currentTick",
            "revision",
            "members",
            "sightings",
            "counts",
            "intel",
            "threat",
            "threatSummaries",
            "treasuryTenant",
            "leaderboardAggression",
        ],
    },
    ("GET", "/api/alliance/defense"): {
        "type": "object",
        "properties": {
            "generatedAtMs": _INT,
            "advice": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": _STR,
                        "category": _STR,
                        "severity": _STR,
                        "title": _STR,
                        "detail": _STR,
                        "tenant": _STR,
                        "relatedTenants": {"type": "array", "items": _STR},
                        "evidence": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {"label": _STR, "value": _STR},
                                "required": ["label", "value"],
                            },
                        },
                    },
                    "required": [
                        "id",
                        "category",
                        "severity",
                        "title",
                        "detail",
                        "tenant",
                        "relatedTenants",
                        "evidence",
                    ],
                },
            },
            "endangered": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "tenantId": _STR,
                        "military": _INT,
                        "threatScore": _NUM,
                    },
                    "required": ["tenantId", "military", "threatScore"],
                },
            },
            "pockets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": _STR,
                        "centroid": {
                            "type": "array",
                            "items": _INT,
                            "minItems": 2,
                            "maxItems": 2,
                        },
                        "enemyCores": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "owner": {"type": "string", "nullable": True},
                                    "position": {
                                        "type": "array",
                                        "items": _INT,
                                        "minItems": 2,
                                        "maxItems": 2,
                                    },
                                },
                                "required": ["owner", "position"],
                            },
                        },
                        "threatenedTenants": {"type": "array", "items": _STR},
                        "minDistance": _INT,
                    },
                    "required": [
                        "id",
                        "centroid",
                        "enemyCores",
                        "threatenedTenants",
                        "minDistance",
                    ],
                },
            },
        },
        "required": ["generatedAtMs", "advice", "endangered", "pockets"],
    },
    ("GET", "/api/alliance/advice"): {
        "type": "object",
        "properties": {
            "generatedAt": _STR,
            "advice": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "severity": _STR,
                        "category": _STR,
                        "tenant": {"type": "string", "nullable": True},
                        "title": _STR,
                        "detail": _STR,
                        "action": _STR,
                        "weight": _NUM,
                        "confidence": _NUM,
                        "evidence": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "type": _STR,
                                    "tenant": {"type": "string", "nullable": True},
                                    "ref": {"type": "string", "nullable": True},
                                    "ageTicks": {"type": "integer", "nullable": True},
                                },
                                "required": ["type"],
                            },
                        },
                        "at": _STR,
                    },
                    "required": [
                        "severity",
                        "category",
                        "tenant",
                        "title",
                        "detail",
                        "action",
                        "weight",
                        "confidence",
                        "evidence",
                        "at",
                    ],
                },
            },
            "summary": {
                "type": "object",
                "properties": {
                    "critical": _INT,
                    "high": _INT,
                    "medium": _INT,
                    "info": _INT,
                },
                "required": ["critical", "high", "medium", "info"],
            },
            "dedupCount": _INT,
            "avgConfidence": _NUM,
            "cachedAt": _STR,
        },
        "required": [
            "generatedAt",
            "advice",
            "summary",
            "dedupCount",
            "avgConfidence",
            "cachedAt",
        ],
    },
    ("GET", "/api/alliance/survey/arbitrations"): {
        "type": "object",
        "properties": {
            "generatedAt": _STR,
            "arbitrations": _OBJ_ROWS,
        },
        "required": ["generatedAt", "arbitrations"],
    },
    ("GET", "/api/alliance/survey"): {
        "type": "object",
        "properties": {
            "generatedAt": _STR,
            "colors": _STR_MAP,
            "tenantSummaries": {"type": "object", "additionalProperties": _OBJ},
            "enemyCores": _OBJ_ROWS,
            "resources": _OBJ_ROWS,
            "obstacles": _OBJ_ROWS,
            "chunks": _OBJ_ROWS,
            "lifecycle": {
                "type": "object",
                "additionalProperties": {"type": "object", "nullable": True},
            },
            "conflicts": {
                "type": "object",
                "properties": {
                    "resourceOverlaps": _OBJ_ROWS,
                    "obstacleResourceConflicts": _OBJ_ROWS,
                },
            },
            "consensusResources": _OBJ_ROWS,
            "consensusCores": _OBJ_ROWS,
            "consensusChunks": _OBJ_ROWS,
            "cachedAt": _STR,
        },
        "required": ["generatedAt"],
    },
    ("GET", "/api/alliance/exploration"): {
        "type": "object",
        "properties": {
            "generatedAt": _STR,
            "world": {
                "type": "object",
                "properties": {
                    "chunkSize": _INT,
                    "observedSpan": {"type": "object", "nullable": True},
                    "spanChunks": _INT,
                    "exploredChunks": _INT,
                    "coveragePct": _NULLABLE_NUM,
                },
                "required": ["chunkSize", "spanChunks", "exploredChunks"],
            },
            "perTenant": {"type": "object", "additionalProperties": _OBJ},
            "alliance": {
                "type": "object",
                "properties": {
                    "unionChunks": _INT,
                    "unionRecent": _INT,
                    "coveragePct": _NULLABLE_NUM,
                    "exclusiveByTenant": _INT_MAP,
                },
                "required": ["unionChunks", "unionRecent", "exclusiveByTenant"],
            },
            "gaps": _OBJ_ROWS,
            "resurveyTargets": _OBJ_ROWS,
            "cachedAt": _STR,
        },
        "required": [
            "generatedAt",
            "world",
            "perTenant",
            "alliance",
            "gaps",
            "resurveyTargets",
            "cachedAt",
        ],
    },
    ("GET", "/api/survey/mine-patterns"): {
        "type": "object",
        "properties": {
            "generatedAt": _STR,
            "tenant": _STR,
            "tenants": {
                "type": "object",
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "tenant": _STR,
                        "total": _INT,
                        "visible": _INT,
                        "stale": _INT,
                        "avgAgeTicks": _INT,
                        "medianSeenCount": _INT,
                        "harvestSuccessRate": _NULLABLE_NUM,
                        "harvestSucceeded": _INT,
                        "harvestFailed": _INT,
                        "topActive": _OBJ_ROWS,
                        "refill": {"type": "object", "nullable": True},
                        "refillSource": _STR,
                        "absentStats": {"type": "object", "nullable": True},
                        "deadMines": _OBJ_ROWS,
                        "predictions": _OBJ_ROWS,
                        "predictionAccuracy": _NULLABLE_NUM,
                    },
                },
            },
            "modelCaveat": _STR,
            "cachedAt": _STR,
        },
        "required": ["generatedAt", "tenant", "tenants", "modelCaveat", "cachedAt"],
    },
    ("GET", "/api/alliance/survey/mining"): {
        "type": "object",
        "properties": {
            "generatedAt": _STR,
            "resources": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "x": _NUM,
                        "y": _NUM,
                        "cell": _STR,
                        "assignedTenant": {"type": "string", "nullable": True},
                        "miningStatus": {"type": "string", "nullable": True},
                        "gapAgeTicks": _NULLABLE_NUM,
                        "threatLevel": _INT,
                        "threatCombat": _NUM,
                    },
                },
            },
            "summary": {
                "type": "object",
                "properties": {
                    "assigned": _INT,
                    "open": _INT,
                    "stale": _INT,
                    "harvested": _INT,
                    "harvestedByOther": _INT,
                    "highThreat": _INT,
                    "topStale": _OBJ_ROWS,
                },
                "required": [
                    "assigned",
                    "open",
                    "stale",
                    "harvested",
                    "harvestedByOther",
                    "highThreat",
                    "topStale",
                ],
            },
            "colors": _STR_MAP,
            "tenantSummaries": {"type": "object", "additionalProperties": _OBJ},
            "cachedAt": _STR,
        },
        "required": [
            "generatedAt",
            "resources",
            "summary",
            "colors",
            "tenantSummaries",
            "cachedAt",
        ],
    },
    ("GET", "/api/alliance/cluster"): {
        "type": "object",
        "properties": {
            "generatedAtMs": _INT,
            "groups": _OBJ_ROWS,
            "members": _OBJ_ROWS,
            "summary": {
                "type": "object",
                "properties": {
                    "memberCount": _INT,
                    "groupCount": _INT,
                    "isolatedCount": _INT,
                    "maxCohesion": _NUM,
                    "avgCohesion": _NUM,
                },
            },
        },
        "required": ["generatedAtMs", "groups", "members", "summary"],
    },
    ("GET", "/api/alliance/mining"): {
        "type": "object",
        "properties": {
            "generatedAt": _STR,
            "currentTick": _NULLABLE_INT,
            "assignments": _OBJ_ROWS,
            "perTenant": {"type": "object", "additionalProperties": _OBJ},
            "unassigned": _OBJ_ROWS,
            "global": {
                "type": "object",
                "properties": {
                    "totalCandidates": _INT,
                    "assigned": _INT,
                    "shared": _INT,
                    "conflict": _INT,
                    "unassigned": _INT,
                },
            },
            "cachedAt": _STR,
        },
        "required": ["generatedAt", "assignments", "perTenant", "unassigned", "global", "cachedAt"],
    },
    ("GET", "/api/audit/decisions"): {
        "oneOf": [
            _DECISION_AUDIT_SHAPE,
            {"type": "object", "additionalProperties": _DECISION_AUDIT_SHAPE},
        ],
    },
    ("GET", "/api/audit/decisions/trend"): {
        "type": "object",
        "properties": {
            "generatedAt": _STR,
            "tenant": _STR,
            "window": _INT,
            "steps": _INT,
            "trend": _OBJ_ROWS,
            "cachedAt": _STR,
        },
        "required": ["generatedAt", "tenant", "window", "steps", "trend"],
    },
    ("GET", "/api/audit/workers"): {
        "type": "object",
        "properties": {
            "generatedAt": _STR,
            "tenant": _STR,
            "window": _INT,
            "totals": {
                "type": "object",
                "properties": {
                    "eventCount": _INT,
                    "affectedWorkers": _INT,
                    "repeatedWorkers": _INT,
                    "recentWorkers": _INT,
                },
            },
            "tenants": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "tenant": _STR,
                        "currentTick": _NULLABLE_INT,
                        "eventCount": _INT,
                        "affectedWorkers": _INT,
                        "repeatedWorkers": _INT,
                        "byKind": _INT_MAP,
                        "latestByWorker": _OBJ_ROWS,
                    },
                },
            },
            "cachedAt": _STR,
        },
        "required": ["generatedAt", "tenant", "window", "totals", "tenants"],
    },
    ("GET", "/api/audit/trail"): {
        "type": "object",
        "properties": {
            "generatedAt": _STR,
            "entries": _OBJ_ROWS,
            "counts": _INT_MAP,
            "filters": {"type": "object", "properties": {"tenant": _STR, "source": _STR}},
            "cachedAt": _STR,
        },
        "required": ["generatedAt", "entries", "counts", "filters"],
    },
    ("GET", "/api/audit/human"): {
        "type": "object",
        "properties": {
            "generatedAt": _STR,
            "tenant": _STR,
            "count": _INT,
            "records": _OBJ_ROWS,
        },
        "required": ["generatedAt", "tenant", "count", "records"],
    },
    ("GET", "/api/audit/human/conflicts"): {
        "oneOf": [
            _HUMAN_CONFLICT_SHAPE,
            {"type": "object", "additionalProperties": _HUMAN_CONFLICT_SHAPE},
        ],
    },
    ("GET", "/api/audit/mines"): {
        "type": "object",
        "properties": {
            "generatedAt": _STR,
            "tenant": _STR,
            "tenants": {
                "type": "object",
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "tenant": _STR,
                        "currentTick": _NULLABLE_INT,
                        "total": _INT,
                        "harvested": _INT,
                        "neverHarvested": _INT,
                        "visibleNever": _INT,
                        "staleNever": _INT,
                        "utilizationRate": _NULLABLE_NUM,
                        "medianTimeToFirstHarvest": _NULLABLE_NUM,
                        "maxGapAgeTicks": _NULLABLE_INT,
                        "medianGapAgeTicks": _NULLABLE_NUM,
                        "candidates": _OBJ_ROWS,
                        "topMines": _OBJ,
                    },
                    "required": ["tenant", "total", "harvested", "neverHarvested"],
                },
            },
            "cachedAt": _STR,
        },
        "required": ["generatedAt", "tenant", "tenants"],
    },
    ("GET", "/api/audit/mines/trend"): {
        "type": "object",
        "properties": {
            "generatedAt": _STR,
            "tenant": _STR,
            "window": _INT,
            "steps": _INT,
            "currentTick": _NULLABLE_INT,
            "trend": _OBJ_ROWS,
            "cachedAt": _STR,
        },
        "required": ["generatedAt", "tenant", "window", "steps", "trend"],
    },
    ("GET", "/api/audit/mining-effectiveness"): {
        "type": "object",
        "properties": {
            "generatedAt": _STR,
            "currentTick": _NULLABLE_INT,
            "items": _OBJ_ROWS,
            "perTenant": {"type": "object", "additionalProperties": _OBJ},
            "global": {
                "type": "object",
                "properties": {
                    "assigned": _INT,
                    "harvested": _INT,
                    "harvestedByOther": _INT,
                    "open": _INT,
                    "stale": _INT,
                    "effectiveRate": _NULLABLE_NUM,
                    "progressRate": _NULLABLE_NUM,
                },
            },
            "cachedAt": _STR,
        },
        "required": ["generatedAt", "items", "perTenant", "global"],
    },
    ("GET", "/api/shop/history"): {
        "type": "object",
        "properties": {
            "generatedAt": _STR,
            "snapshots": _INT,
            "productCount": _INT,
            "lastSnapshotAt": {"type": "string", "nullable": True},
            "trends": _OBJ_ROWS,
            "refreshedAt": {"type": "string", "nullable": True},
            "cachedAt": _STR,
        },
        "required": ["generatedAt", "snapshots", "productCount", "trends"],
    },
    ("GET", "/api/intel/heat"): {
        "type": "object",
        "properties": {
            "generatedAt": _STR,
            "tenant": _STR,
            "currentTick": _INT,
            "buckets": _OBJ_ROWS,
            "fullBuckets": _OBJ_ROWS,
            "summary": {
                "type": "object",
                "properties": {
                    "totalSightings": _INT,
                    "distinctCells": _INT,
                    "combatSightings": _INT,
                    "workerSightings": _INT,
                    "tenants": _INT,
                },
                "required": [
                    "totalSightings",
                    "distinctCells",
                    "combatSightings",
                    "workerSightings",
                    "tenants",
                ],
            },
            "cachedAt": _STR,
        },
        "required": [
            "generatedAt",
            "tenant",
            "currentTick",
            "buckets",
            "fullBuckets",
            "summary",
            "cachedAt",
        ],
    },
    ("GET", "/api/registry/agents"): {
        "type": "object",
        "properties": {
            "generatedAt": _STR,
            "agents": _OBJ_ROWS,
        },
        "required": ["generatedAt", "agents"],
    },
}


_ETAG_HEADERS = {
    "ETag": {"schema": {"type": "string", "example": f"{ETAG_PREFIX}<map-sig>{ETAG_SUFFIX}"}},
    "Cache-Control": {"schema": {"type": "string", "example": MAP_CACHE_CONTROL}},
}

_TENANT_ENUMS = {
    "all|tN": ["all", "t1", "t2", "t3", "t4"],
    "t1": ["t1", "t2", "t3", "t4"],
    "tN": ["t1", "t2", "t3", "t4"],
}

__all__ = ["DEFAULT_API_VERSION", "OPENAPI_VERSION", "build_openapi", "openapi_json"]


def _package_version() -> str:
    try:
        return importlib.metadata.version("arena-hero-agent")
    except importlib.metadata.PackageNotFoundError:
        return DEFAULT_API_VERSION


def _operation_id(route: Route) -> str:
    """Deterministic camelCase operation id (``getMap``, ``deleteRegistryAgentsId``)."""
    segments: list[str] = []
    for segment in route.path.split("/"):
        if not segment or segment == "api":
            continue
        for token in re.split(r"[^A-Za-z0-9]+", segment):
            if token:
                segments.append(token[0].upper() + token[1:])
    return route.method.lower() + "".join(segments)


def _query_parameters(route: Route) -> list[dict[str, Any]]:
    int_keys = int_query_keys()
    parameters: list[dict[str, Any]] = []
    for name in route.query:
        if name == "tenant":
            enum = _TENANT_ENUMS.get(str(route.tenant_param))
            schema: dict[str, Any] = (
                {"type": "string", "enum": enum} if enum else {"type": "string"}
            )
        elif name in int_keys:
            schema = {"type": "integer"}
            if name in {"n", "limit", "window", "steps"}:
                schema["minimum"] = 1
            if name == "n":
                schema["maximum"] = 200
        else:
            schema = {"type": "string"}
        parameters.append({"name": name, "in": "query", "required": False, "schema": schema})
    return parameters


def _path_parameters(route: Route) -> list[dict[str, Any]]:
    if ":id" not in route.path:
        return []
    return [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}]


def _responses(route: Route) -> dict[str, Any]:
    schema = _RESPONSE_SCHEMAS.get((route.method, route.path), {"type": "object"})
    responses: dict[str, Any] = {
        "200": {"description": "OK", "content": {"application/json": {"schema": schema}}},
        "400": {"description": "Invalid query parameters or tenant"},
        "404": {"description": "Route not found"},
        "500": {"description": "Internal error"},
    }
    if route.etag is not None:
        responses["200"]["headers"] = _ETAG_HEADERS
        responses["304"] = {
            "description": "Not Modified: If-None-Match equals the weak ETag",
            "headers": _ETAG_HEADERS,
        }
    return responses


def _operation(route: Route) -> dict[str, Any]:
    operation: dict[str, Any] = {
        "operationId": _operation_id(route),
        "summary": route.notes or f"{route.method} {route.path}",
        "parameters": [*_query_parameters(route), *_path_parameters(route)],
        "responses": _responses(route),
        "x-command-center": {
            "stream_kind": route.stream_kind,
            "cache": route.cache,
            "write_semantics": route.write_semantics,
            "tenant_param": route.tenant_param,
            "etag": route.etag,
            "query": list(route.query),
        },
    }
    return operation


def build_openapi(table: RouteTable) -> dict[str, Any]:
    """Build the OpenAPI 3.1 document for a route table."""
    paths: dict[str, Any] = {}
    for route in table.api_routes:
        path_key = route.path.replace(":id", "{id}")
        paths.setdefault(path_key, {})[route.method.lower()] = _operation(route)
    return {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": "Arena Hero Command Center API",
            "version": _package_version(),
            "description": (
                "Loopback-only JSON polling API for the Arena Hero Command Center "
                f"({len(table.api_routes)} API + {len(table.static_routes)} static routes). "
                "Every route is request/response poll-json; there is no SSE or WebSocket "
                "surface. Only /api/map uses a weak HTTP ETag (304 when If-None-Match "
                "matches) with cache-control public, max-age=2. The WebSocket wire "
                "contract lives in the arena-hero-ts package."
            ),
        },
        "paths": paths,
        "x-command-center": {
            "api_route_count": len(table.api_routes),
            "static_route_count": len(table.static_routes),
            "static_routes": [
                {"method": route.method, "path": route.path} for route in table.static_routes
            ],
            "stream_kind": "poll-json",
            "etag_routes": [route.path for route in table.api_routes if route.etag],
            "write_routes": [
                {"method": route.method, "path": route.path}
                for route in table.api_routes
                if route.write_semantics != "read-only"
            ],
        },
    }


def openapi_json(table: RouteTable) -> str:
    """Serialize the OpenAPI document deterministically (sorted keys)."""
    return json.dumps(build_openapi(table), sort_keys=True, indent=2, ensure_ascii=False) + "\n"

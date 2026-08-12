"""Alliance cluster situational view (port of legacy ``alliance-cluster.ts``).

Pure, deterministic, I/O-free cluster view: tenants whose cores are within
``CLUSTER_LINK_DIST`` (Chebyshev) form a defense cluster (simple connected
grouping), each tenant gets a cohesion index (normalized distance to its
cluster centroid), and each cluster gets a centroid, radius, and summed
military/worker strength. ``/api/alliance/cluster``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any

from ._common import current_epoch_ms, num
from .alliance_snapshot import load_alliance_snapshot

CLUSTER_LINK_DIST = 120
COHESION_MAX_DIST = 300

__all__ = [
    "CLUSTER_LINK_DIST",
    "COHESION_MAX_DIST",
    "build_alliance_cluster_view",
    "cluster_input_of_members",
    "load_alliance_cluster",
]


def _chebyshev(a: Sequence[int], b: Sequence[int]) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def _group_members(input_members: list[dict[str, Any]], link_dist: int) -> list[int]:
    n = len(input_members)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        root_a = find(a)
        root_b = find(b)
        if root_a != root_b:
            parent[root_b] = root_a

    for i in range(n):
        core_a = input_members[i].get("core")
        if core_a is None:
            continue
        for j in range(i + 1, n):
            core_b = input_members[j].get("core")
            if core_b is None:
                continue
            if _chebyshev(core_a, core_b) <= link_dist:
                union(i, j)
    return [find(i) for i in range(n)]


def build_alliance_cluster_view(
    input_members: list[dict[str, Any]],
    now_ms: int,
) -> dict[str, Any]:
    """Build the cluster view (TS parity). Input sorted by tenantId internally."""
    sorted_members = sorted(input_members, key=lambda member: str(member["tenantId"]))
    cluster_of = _group_members(sorted_members, CLUSTER_LINK_DIST)
    id_by_root: dict[int, int] = {}
    next_id = 0
    for root in cluster_of:
        if root not in id_by_root:
            id_by_root[root] = next_id
            next_id += 1
    cluster_ids = [id_by_root[root] for root in cluster_of]

    groups: list[dict[str, Any]] = []
    for group_id in range(next_id):
        idxs = [i for i in range(len(sorted_members)) if cluster_ids[i] == group_id]
        cores = [
            sorted_members[i]["core"] for i in idxs if sorted_members[i].get("core") is not None
        ]
        centroid = (
            None
            if not cores
            else [
                round(sum(c[0] for c in cores) / len(cores)),
                round(sum(c[1] for c in cores) / len(cores)),
            ]
        )
        radius = 0
        if centroid is not None:
            for core in cores:
                radius = max(radius, _chebyshev(core, centroid))
        groups.append(
            {
                "id": group_id,
                "tenantIds": [sorted_members[i]["tenantId"] for i in idxs],
                "centroid": centroid,
                "military": sum(sorted_members[i]["military"] for i in idxs),
                "workers": sum(sorted_members[i]["workers"] for i in idxs),
                "radius": radius,
            }
        )

    members: list[dict[str, Any]] = []
    for i, member in enumerate(sorted_members):
        group_id = cluster_ids[i]
        group = groups[group_id]
        core = member.get("core")
        centroid = group["centroid"]
        if core is None or centroid is None or len(group["tenantIds"]) < 2:
            cohesion = 0
        else:
            cohesion = max(0, 1 - _chebyshev(core, centroid) / COHESION_MAX_DIST)
        members.append(
            {
                "tenantId": member["tenantId"],
                "core": core,
                "military": member["military"],
                "workers": member["workers"],
                "status": member["status"],
                "clusterId": group_id,
                "clusterSize": len(group["tenantIds"]),
                "cohesion": round(cohesion * 1000) / 1000,
            }
        )

    grouped = [member for member in members if member["clusterSize"] > 1]
    max_cohesion = max((member["cohesion"] for member in grouped), default=0)
    avg_cohesion = (
        round(sum(member["cohesion"] for member in grouped) / len(grouped) * 1000) / 1000
        if grouped
        else 0
    )

    return {
        "generatedAtMs": now_ms,
        "groups": sorted(groups, key=lambda group: group["id"]),
        "members": members,
        "summary": {
            "memberCount": len(sorted_members),
            "groupCount": len(groups),
            "isolatedCount": sum(1 for member in members if member["clusterSize"] == 1),
            "maxCohesion": max_cohesion,
            "avgCohesion": avg_cohesion,
        },
    }


def cluster_input_of_members(
    members: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Build the cluster member input from alliance-snapshot members.

    Mirrors the TS oracle ``clusterInputOfMembers``: ``core`` is the member's
    core position (or None), ``military`` is vanguards + rangers, and status is
    carried through for the member table.
    """
    out: list[dict[str, Any]] = []
    for tenant_id, member in members.items():
        if not isinstance(member, dict):
            continue
        core = member.get("core") or {}
        position = core.get("position")
        core_position: list[int] | None = None
        if isinstance(position, (list, tuple)) and len(position) >= 2:
            core_position = [int(num(position[0])), int(num(position[1]))]
        out.append(
            {
                "tenantId": str(tenant_id),
                "core": core_position,
                "military": num(member.get("vanguards")) + num(member.get("rangers")),
                "workers": num(member.get("workers")),
                "status": str(member.get("status") or ""),
            }
        )
    return out


def load_alliance_cluster(
    data_root: str | os.PathLike[str],
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Load the alliance cluster situational view (TS ``loadAllianceCluster``).

    Composes the P5-4 ``load_alliance_snapshot`` members (core/strength per
    tenant) into the pure cluster aggregation. Fail-open: an empty snapshot
    yields one empty member set with zero groups/cohesion.
    """
    snapshot = load_alliance_snapshot(data_root, now_ms=now_ms)
    members = snapshot.get("members") or {}
    now = now_ms if now_ms is not None else current_epoch_ms()
    return build_alliance_cluster_view(cluster_input_of_members(members), now)

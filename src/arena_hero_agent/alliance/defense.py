"""Alliance joint-defense coordination read model (W21).

Port of the legacy TypeScript ``packages/command-center/lib/alliance-defense.ts``
(2026-08-08 抱团 Phase 2 决策支持层): pure, deterministic, I/O-free decision
support that turns the alliance snapshot (members / threat summaries / enemy
core sightings) into actionable defense advice. Pure functions only; no I/O,
no API imports — the Command Center projection layer
(``command_center/projections/alliance_defense.py``) stays a thin wrapper.

Four advice categories (TS ``DefenseCategory``):

  1. ENDANGERED — core destroyed while respawning (CRITICAL) or military <= 1
     with high threat (HIGH/CRITICAL); military == 0 is endangered
     unconditionally (zero combat units cannot defend, t3 73094 evidence);
  2. REINFORCE — the nearest military-surplus neighbor (>= 2 combat units,
     Chebyshev distance < 400) of each endangered member, with a quantified
     raid force (enemy count x 1.5, capped by neighbor surplus);
  3. FORMATION — pairwise core median distance across the alliance (tight /
     loose / dispersed triangle posture);
  4. POCKET — enemy-core clusters (Chebyshev <= 120) threatening >= 2 member
     cores within 200 cells; see ``build_defense_pockets``.

Determinism notes (parity with the TS oracle):

- ``generatedAtMs`` is injectable via ``now_ms`` (TS ``Date.now()`` is wall
  clock and not oracle-comparable); callers pass an explicit epoch-ms value.
- Numbers interpolated into advice strings follow JS ``String(number)``
  semantics (integral floats render without a trailing ``.0``) so the wire
  text matches the oracle byte-for-byte.
- Cluster centroids use JS ``Math.round`` (round-half-up) semantics.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from arena_hero_agent.domain import Coordinate

# --- TS constants ---------------------------------------------------------

ENDANGERED_COMBAT_MAX = 1
ENDANGERED_THREAT_MIN = 6
REINFORCE_RANGE = 400
REINFORCE_COMBAT_MIN = 2
FORMATION_TIGHT_MAX = 120
FORMATION_LOOSE_MAX = 300
DEFAULT_POCKET_CLUSTER_DIST = 120
DEFAULT_POCKET_THREAT_RADIUS = 200

_SEV_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "INFO": 3}


class DefenseCategory(StrEnum):
    """Advice category (TS ``DefenseCategory``)."""

    __canonical_name__ = "arena-hero.defense-category.v1"

    ENDANGERED = "ENDANGERED"
    REINFORCE = "REINFORCE"
    FORMATION = "FORMATION"
    POCKET = "POCKET"


class DefenseSeverity(StrEnum):
    """Advice severity (TS ``DefenseSeverity``)."""

    __canonical_name__ = "arena-hero.defense-severity.v1"

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    INFO = "INFO"


@dataclass(frozen=True, slots=True)
class DefenseMemberInput:
    """One alliance member's defense posture (TS ``DefenseMemberInput``)."""

    __canonical_name__ = "arena-hero.defense-member-input.v1"

    tenant_id: str
    core: Coordinate | None
    military: int
    status: str
    threat_score: float = 0.0
    threat_directions: tuple[str, ...] = ()
    threat_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, str) or not self.tenant_id:
            raise TypeError("tenant_id must be a non-empty string")
        if self.core is not None and not isinstance(self.core, Coordinate):
            raise TypeError("core must be a Coordinate or None")
        if isinstance(self.military, bool) or not isinstance(self.military, int):
            raise TypeError("military must be an integer")
        if self.military < 0:
            raise ValueError("military cannot be negative")
        if not isinstance(self.status, str):
            raise TypeError("status must be a string")
        if isinstance(self.threat_score, bool) or not isinstance(self.threat_score, (int, float)):
            raise TypeError("threat_score must be a number")
        if not math.isfinite(float(self.threat_score)):
            raise ValueError("threat_score must be finite")
        if not isinstance(self.threat_directions, tuple) or not all(
            isinstance(item, str) for item in self.threat_directions
        ):
            raise TypeError("threat_directions must be a tuple of strings")
        if isinstance(self.threat_count, bool) or not isinstance(self.threat_count, int):
            raise TypeError("threat_count must be an integer")
        if self.threat_count < 0:
            raise ValueError("threat_count cannot be negative")


@dataclass(frozen=True, slots=True)
class PocketEnemyCore:
    """Enemy-core sighting from the alliance snapshot (TS ``PocketEnemyCore``)."""

    __canonical_name__ = "arena-hero.pocket-enemy-core.v1"

    key: str
    owner: str | None
    position: Coordinate
    last_seen_tick: int

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key:
            raise TypeError("key must be a non-empty string")
        if self.owner is not None and not isinstance(self.owner, str):
            raise TypeError("owner must be a string or None")
        if not isinstance(self.position, Coordinate):
            raise TypeError("position must be a Coordinate")
        if isinstance(self.last_seen_tick, bool) or not isinstance(self.last_seen_tick, int):
            raise TypeError("last_seen_tick must be an integer")


@dataclass(frozen=True, slots=True)
class PocketConfig:
    """POCKET clustering configuration (TS ``PocketConfig``)."""

    __canonical_name__ = "arena-hero.pocket-config.v1"

    cluster_dist: int = DEFAULT_POCKET_CLUSTER_DIST
    threat_radius: int = DEFAULT_POCKET_THREAT_RADIUS

    def __post_init__(self) -> None:
        for name in ("cluster_dist", "threat_radius"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 0:
                raise ValueError(f"{name} cannot be negative")


def resolve_pocket_config(
    config: Mapping[str, Any] | PocketConfig | None = None,
) -> PocketConfig:
    """Resolve a partial POCKET config to validated defaults (TS spread merge)."""
    if config is None:
        return PocketConfig()
    if isinstance(config, PocketConfig):
        return config
    if not isinstance(config, Mapping):
        raise TypeError("config must be a PocketConfig or a mapping")
    cluster = _sanitize_int(config.get("cluster_dist"), DEFAULT_POCKET_CLUSTER_DIST)
    radius = _sanitize_int(config.get("threat_radius"), DEFAULT_POCKET_THREAT_RADIUS)
    return PocketConfig(cluster_dist=cluster, threat_radius=radius)


@dataclass(frozen=True, slots=True)
class DefenseAdvice:
    """One defense advice entry (TS ``DefenseAdvice``)."""

    __canonical_name__ = "arena-hero.defense-advice.v1"

    id: str
    category: DefenseCategory
    severity: DefenseSeverity
    title: str
    detail: str
    tenant: str
    related_tenants: tuple[str, ...]
    evidence: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise TypeError("id must be a non-empty string")
        if not isinstance(self.category, DefenseCategory):
            raise TypeError("category must be a DefenseCategory")
        if not isinstance(self.severity, DefenseSeverity):
            raise TypeError("severity must be a DefenseSeverity")
        if not isinstance(self.title, str) or not isinstance(self.detail, str):
            raise TypeError("title and detail must be strings")
        if not isinstance(self.tenant, str):
            raise TypeError("tenant must be a string")
        if not isinstance(self.related_tenants, tuple) or not all(
            isinstance(item, str) for item in self.related_tenants
        ):
            raise TypeError("related_tenants must be a tuple of strings")
        if not isinstance(self.evidence, tuple) or not all(
            isinstance(item, tuple)
            and len(item) == 2
            and isinstance(item[0], str)
            and isinstance(item[1], str)
            for item in self.evidence
        ):
            raise TypeError("evidence must be a tuple of (label, value) string pairs")


@dataclass(frozen=True, slots=True)
class DefensePocket:
    """A clustered enemy-core formation threatening >= 2 members (TS ``DefensePocket``)."""

    __canonical_name__ = "arena-hero.defense-pocket.v1"

    id: str
    centroid: Coordinate
    enemy_cores: tuple[tuple[str | None, Coordinate], ...]
    threatened_tenants: tuple[str, ...]
    min_distance: int

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise TypeError("id must be a non-empty string")
        if not isinstance(self.centroid, Coordinate):
            raise TypeError("centroid must be a Coordinate")
        if not isinstance(self.enemy_cores, tuple) or not all(
            isinstance(item, tuple)
            and len(item) == 2
            and (item[0] is None or isinstance(item[0], str))
            and isinstance(item[1], Coordinate)
            for item in self.enemy_cores
        ):
            raise TypeError("enemy_cores must be a tuple of (owner, Coordinate) pairs")
        if not isinstance(self.threatened_tenants, tuple) or not all(
            isinstance(item, str) for item in self.threatened_tenants
        ):
            raise TypeError("threatened_tenants must be a tuple of strings")
        if isinstance(self.min_distance, bool) or not isinstance(self.min_distance, int):
            raise TypeError("min_distance must be an integer")


@dataclass(frozen=True, slots=True)
class DefenseEndangered:
    """One endangered member summary (TS ``DefensePayload.endangered``)."""

    __canonical_name__ = "arena-hero.defense-endangered.v1"

    tenant_id: str
    military: int
    threat_score: float

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, str) or not self.tenant_id:
            raise TypeError("tenant_id must be a non-empty string")
        if isinstance(self.military, bool) or not isinstance(self.military, int):
            raise TypeError("military must be an integer")
        if isinstance(self.threat_score, bool) or not isinstance(self.threat_score, (int, float)):
            raise TypeError("threat_score must be a number")
        if not math.isfinite(float(self.threat_score)):
            raise ValueError("threat_score must be finite")


@dataclass(frozen=True, slots=True)
class DefensePayload:
    """Coordination output (TS ``DefensePayload``)."""

    __canonical_name__ = "arena-hero.defense-payload.v1"

    generated_at_ms: int
    advice: tuple[DefenseAdvice, ...]
    endangered: tuple[DefenseEndangered, ...]
    pockets: tuple[DefensePocket, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.generated_at_ms, bool) or not isinstance(self.generated_at_ms, int):
            raise TypeError("generated_at_ms must be an integer")
        if not isinstance(self.advice, tuple) or not all(
            isinstance(item, DefenseAdvice) for item in self.advice
        ):
            raise TypeError("advice must be a tuple of DefenseAdvice")
        if not isinstance(self.endangered, tuple) or not all(
            isinstance(item, DefenseEndangered) for item in self.endangered
        ):
            raise TypeError("endangered must be a tuple of DefenseEndangered")
        if not isinstance(self.pockets, tuple) or not all(
            isinstance(item, DefensePocket) for item in self.pockets
        ):
            raise TypeError("pockets must be a tuple of DefensePocket")


# --- pure helpers ---------------------------------------------------------


def _require_finite_number(name: str, value: object) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _sanitize_int(value: object, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _js_number(value: int | float) -> str:
    """Render a number the way JS ``String(number)`` does (integral -> no .0)."""
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return str(number)


def _js_round(value: float) -> int:
    """JS ``Math.round`` (round half up toward +infinity)."""
    return math.floor(value + 0.5)


def chebyshev(first: Coordinate, second: Coordinate) -> int:
    """King-move grid distance (TS ``chebyshev``)."""
    return max(abs(first.x - second.x), abs(first.y - second.y))


def direction_of(a: Coordinate, b: Coordinate) -> str:
    """Eight-direction sector of ``b`` relative to ``a`` (TS ``directionOf``).

    Mirrors the threat-summary sector convention: ``dy > 0`` is north, and a
    target within 3 cells in both axes is the same cell ("C").
    """
    dx = b.x - a.x
    dy = b.y - a.y
    ax = abs(dx)
    ay = abs(dy)
    if ax < 3 and ay < 3:
        return "C"
    if ax * 2 < ay:
        return "N" if dy > 0 else "S"
    if ay * 2 < ax:
        return "E" if dx > 0 else "W"
    if dx > 0 and dy > 0:
        return "NE"
    if dx > 0 and dy < 0:
        return "SE"
    if dx < 0 and dy < 0:
        return "SW"
    return "NW"


def suggested_raid_force(enemy_count: int, ally_surplus: int) -> tuple[int, int] | None:
    """Quantified reinforcement (TS ``suggestedRaidForce``).

    At least 2 Vanguard, scaled to ``enemy_count * 1.5``, capped by the ally's
    surplus (keep 1 defender home); enemies >= 5 add up to 2 Ranger pursuers.
    Returns ``(vanguard, ranger)`` or ``None`` when the ally has no surplus.
    """
    if ally_surplus <= 0:
        return None
    want = max(2, math.ceil(enemy_count * 1.5))
    send = min(want, ally_surplus)
    if send <= 0:
        return None
    ranger = min(2, send // 3) if send >= 5 else 0
    return send - ranger, ranger


def endangered_of(member: DefenseMemberInput) -> tuple[bool, str]:
    """Classify one member as endangered (TS ``endangeredOf``)."""
    if member.status == "RESPAWNING":
        return True, "respawn"
    if member.military == 0:
        return True, "zero"
    if member.military <= ENDANGERED_COMBAT_MAX and member.threat_score >= ENDANGERED_THREAT_MIN:
        return True, "weak"
    return False, ""


def _advice_sort_key(item: DefenseAdvice) -> tuple[int, str]:
    """Severity-order then id (TS comparator ``SEV_ORDER || id.localeCompare``)."""
    return (_SEV_ORDER[item.severity.value], item.id)


# --- POCKET clustering ----------------------------------------------------


def build_defense_pockets(
    members: Sequence[DefenseMemberInput],
    enemy_cores: Sequence[PocketEnemyCore],
    config: Mapping[str, Any] | PocketConfig | None = None,
) -> tuple[DefensePocket, ...]:
    """Cluster enemy cores into joint-defense pockets (TS ``buildDefensePockets``).

    Enemy cores within ``cluster_dist`` (Chebyshev) are greedily merged into
    connected groups; a group with >= 2 cores that threatens >= 2 member cores
    (nearest core within ``threat_radius``) becomes a pocket with a rounded
    centroid and the minimal threatened distance.
    """
    resolved = resolve_pocket_config(config)
    for member in members:
        if not isinstance(member, DefenseMemberInput):
            raise TypeError("members must contain only DefenseMemberInput")
    for core in enemy_cores:
        if not isinstance(core, PocketEnemyCore):
            raise TypeError("enemy_cores must contain only PocketEnemyCore")
    groups: list[list[PocketEnemyCore]] = []
    for core in enemy_cores:
        merged = False
        for group in groups:
            if any(
                chebyshev(existing.position, core.position) <= resolved.cluster_dist
                for existing in group
            ):
                group.append(core)
                merged = True
                break
        if not merged:
            groups.append([core])
    pockets: list[DefensePocket] = []
    for group in groups:
        if len(group) < 2:
            continue
        cx = sum(item.position.x for item in group) / len(group)
        cy = sum(item.position.y for item in group) / len(group)
        threatened: list[tuple[str, int]] = []
        for member in members:
            if member.core is None:
                continue
            min_dist = min(chebyshev(member.core, item.position) for item in group)
            if min_dist <= resolved.threat_radius:
                threatened.append((member.tenant_id, min_dist))
        if len(threatened) < 2:
            continue
        pockets.append(
            DefensePocket(
                id="pocket:" + "+".join(sorted(item.key for item in group)),
                centroid=Coordinate(_js_round(cx), _js_round(cy)),
                enemy_cores=tuple((item.owner, item.position) for item in group),
                threatened_tenants=tuple(tenant for tenant, _dist in threatened),
                min_distance=min(dist for _tenant, dist in threatened),
            )
        )
    return tuple(pockets)


def build_defense_pocket_advice(
    members: Sequence[DefenseMemberInput],
    enemy_cores: Sequence[PocketEnemyCore],
    config: Mapping[str, Any] | PocketConfig | None = None,
) -> tuple[DefenseAdvice, ...]:
    """Turn pockets into POCKET advice entries (TS ``buildDefensePocketAdvice``)."""
    return tuple(
        DefenseAdvice(
            id=f"defense:pocket:{pocket.id}",
            category=DefenseCategory.POCKET,
            severity=DefenseSeverity.MEDIUM,
            title="联防圈：{} 之间的敌核群".format(
                "/".join(tenant.upper() for tenant in pocket.threatened_tenants)
            ),
            detail=(
                "{} 个敌核（中心 {},{}）威胁 {}（最近 {} 格）"
                "——建议协同设防或收缩核心避其锋芒".format(
                    len(pocket.enemy_cores),
                    pocket.centroid.x,
                    pocket.centroid.y,
                    "/".join(tenant.upper() for tenant in pocket.threatened_tenants),
                    pocket.min_distance,
                )
            ),
            tenant=pocket.threatened_tenants[0],
            related_tenants=pocket.threatened_tenants,
            evidence=(
                (
                    "敌核",
                    "、".join(
                        owner if owner is not None else "?" for owner, _pos in pocket.enemy_cores
                    ),
                ),
                ("中心", f"{pocket.centroid.x},{pocket.centroid.y}"),
                ("最近核距", f"{pocket.min_distance} 格"),
            ),
        )
        for pocket in build_defense_pockets(members, enemy_cores, config)
    )


# --- coordination ---------------------------------------------------------


def build_defense_coordination(
    members: Sequence[DefenseMemberInput],
    *,
    now_ms: int = 0,
) -> DefensePayload:
    """Build the joint-defense coordination payload (TS ``buildDefenseCoordination``).

    Recognizes endangered members, recommends the nearest military-surplus
    neighbor for each, and reports formation compactness; POCKET advice is a
    separate composition step (the Command Center endpoint merges both).
    """
    for member in members:
        if not isinstance(member, DefenseMemberInput):
            raise TypeError("members must contain only DefenseMemberInput")
    advice: list[DefenseAdvice] = []
    endangered: list[DefenseEndangered] = []
    by_id = {member.tenant_id: member for member in members}

    def core_of(tenant: str) -> Coordinate | None:
        member = by_id.get(tenant)
        return member.core if member is not None else None

    # 1) endangered recognition
    for member in members:
        is_end, reason = endangered_of(member)
        if not is_end:
            continue
        endangered.append(
            DefenseEndangered(
                tenant_id=member.tenant_id,
                military=member.military,
                threat_score=float(member.threat_score),
            )
        )
        if reason == "respawn":
            advice.append(
                DefenseAdvice(
                    id=f"defense:endangered:{member.tenant_id}:respawn",
                    category=DefenseCategory.ENDANGERED,
                    severity=DefenseSeverity.CRITICAL,
                    title=f"{member.tenant_id.upper()} 核心被打爆重生中",
                    detail="新核心区军事=0，敌人可能乘胜追击——联盟需协防新核心或立即补 Vanguard",
                    tenant=member.tenant_id,
                    related_tenants=(),
                    evidence=(("状态", "RESPAWNING"), ("军事", str(member.military))),
                )
            )
        elif reason == "zero":
            severity = (
                DefenseSeverity.CRITICAL
                if member.threat_score >= ENDANGERED_THREAT_MIN
                else DefenseSeverity.HIGH
            )
            threat_note = (
                f"、威胁分={_js_number(member.threat_score)}" if member.threat_score > 0 else ""
            )
            advice.append(
                DefenseAdvice(
                    id=f"defense:endangered:{member.tenant_id}:zero",
                    category=DefenseCategory.ENDANGERED,
                    severity=severity,
                    title=f"{member.tenant_id.upper()} 零军事——无防御反击能力",
                    detail=(
                        f"军事=0{threat_note}——建议立即补 Vanguard（防御是底线，不能等威胁逼近）"
                    ),
                    tenant=member.tenant_id,
                    related_tenants=(),
                    evidence=(
                        ("军事", "0"),
                        ("威胁分", _js_number(member.threat_score)),
                    ),
                )
            )
        else:
            severity = (
                DefenseSeverity.CRITICAL if member.threat_score >= 10 else DefenseSeverity.HIGH
            )
            advice.append(
                DefenseAdvice(
                    id=f"defense:endangered:{member.tenant_id}:weak",
                    category=DefenseCategory.ENDANGERED,
                    severity=severity,
                    title=f"{member.tenant_id.upper()} 军事薄弱且受威胁",
                    detail=(
                        f"军事={member.military}、威胁分={_js_number(member.threat_score)}"
                        "——建议立即补 Vanguard 或向盟友收缩"
                    ),
                    tenant=member.tenant_id,
                    related_tenants=(),
                    evidence=(
                        ("军事", str(member.military)),
                        ("威胁分", _js_number(member.threat_score)),
                    ),
                )
            )

    # 2) reinforcement: nearest military-surplus neighbor per endangered member
    endangered_ids = {entry.tenant_id for entry in endangered}
    for entry in endangered:
        ec = core_of(entry.tenant_id)
        if ec is None:
            continue
        best: tuple[str, int, int, Coordinate] | None = None
        for neighbor in members:
            if neighbor.tenant_id == entry.tenant_id or neighbor.tenant_id in endangered_ids:
                continue
            if neighbor.military < REINFORCE_COMBAT_MIN:
                continue
            nc = neighbor.core
            if nc is None:
                continue
            dist = chebyshev(ec, nc)
            if dist > REINFORCE_RANGE:
                continue
            if best is None or dist < best[1]:
                best = (neighbor.tenant_id, dist, neighbor.military, nc)
        if best is None:
            continue
        best_tenant, best_dist, best_military, best_core = best
        endangered_member = by_id[entry.tenant_id]
        threat_dirs = endangered_member.threat_directions
        flank_dir = direction_of(ec, best_core) if best_core is not None else None
        on_flank = flank_dir is not None and flank_dir != "C" and flank_dir in threat_dirs
        if threat_dirs and flank_dir is not None:
            flank_note = (
                (
                    f"；注意 {best_tenant.upper()} 位于威胁锋面（{'/'.join(threat_dirs)}）侧"
                    "——驰援需绕行或先清剿"
                )
                if on_flank
                else (
                    f"；{best_tenant.upper()} 从 {flank_dir} 侧进入"
                    f"可避开威胁锋面（{'/'.join(threat_dirs)}）"
                )
            )
        else:
            flank_note = ""
        enemy_count = endangered_member.threat_count
        force = suggested_raid_force(enemy_count, max(0, best_military - 1))
        if force is not None:
            vanguard, ranger = force
            force_note = (
                f"——建议编成 {vanguard} Vanguard"
                + (f" + {ranger} Ranger" if ranger > 0 else "")
                + (f"（对应敌 {enemy_count} 单位）" if enemy_count > 0 else "（防御底线）")
            )
        else:
            force_note = ""
        evidence: list[tuple[str, str]] = [
            ("核距", f"{best_dist} 格"),
            ("可调配", f"{best_military} 战斗单位"),
            ("濒危方", entry.tenant_id.upper()),
        ]
        if flank_dir is not None:
            evidence.append(("援军方位", flank_dir))
        if force is not None:
            evidence.append(
                (
                    "建议编成",
                    f"{vanguard}V" + (f"+{ranger}R" if ranger > 0 else ""),
                )
            )
        advice.append(
            DefenseAdvice(
                id=f"defense:reinforce:{best_tenant}:{entry.tenant_id}",
                category=DefenseCategory.REINFORCE,
                severity=DefenseSeverity.HIGH,
                title=f"{best_tenant.upper()} 可驰援 {entry.tenant_id.upper()}",
                detail=(
                    f"距 {best_dist} 格、{best_military} 战斗单位可调配——濒危租户 "
                    f"{entry.tenant_id.upper()} 需外援{force_note}{flank_note}"
                ),
                tenant=best_tenant,
                related_tenants=(entry.tenant_id,),
                evidence=tuple(evidence),
            )
        )

    # 3) formation compactness (pairwise core median)
    cores = [(member.tenant_id, member.core) for member in members if member.core is not None]
    if len(cores) >= 3:
        dists = [
            chebyshev(left, right)
            for index, (_left_tenant, left) in enumerate(cores)
            for _right_tenant, right in cores[index + 1 :]
        ]
        dists.sort()
        med = dists[len(dists) // 2]
        if med < FORMATION_TIGHT_MAX:
            label = "紧凑"
            severity = DefenseSeverity.INFO
        elif med < FORMATION_LOOSE_MAX:
            label = "松散"
            severity = DefenseSeverity.INFO
        else:
            label = "离散"
            severity = DefenseSeverity.MEDIUM
        advice.append(
            DefenseAdvice(
                id="defense:formation",
                category=DefenseCategory.FORMATION,
                severity=severity,
                title=f"联盟阵型{label}",
                detail=(
                    f"{len(cores)} 租户核心两两核距中位 {med} 格（{label}）——"
                    + (
                        "建议收缩成三角态势以缩短驰援时间"
                        if med >= FORMATION_LOOSE_MAX
                        else "联防响应半径可接受"
                    )
                ),
                tenant=cores[0][0],
                related_tenants=tuple(tenant for tenant, _core in cores),
                evidence=(("核距中位", f"{med} 格"),),
            )
        )

    advice.sort(key=_advice_sort_key)
    return DefensePayload(
        generated_at_ms=now_ms,
        advice=tuple(advice),
        endangered=tuple(endangered),
    )


__all__ = [
    "DEFAULT_POCKET_CLUSTER_DIST",
    "DEFAULT_POCKET_THREAT_RADIUS",
    "DefenseAdvice",
    "DefenseCategory",
    "DefenseEndangered",
    "DefenseMemberInput",
    "DefensePayload",
    "DefensePocket",
    "DefenseSeverity",
    "ENDANGERED_COMBAT_MAX",
    "ENDANGERED_THREAT_MIN",
    "FORMATION_LOOSE_MAX",
    "FORMATION_TIGHT_MAX",
    "PocketConfig",
    "PocketEnemyCore",
    "REINFORCE_COMBAT_MIN",
    "REINFORCE_RANGE",
    "build_defense_coordination",
    "build_defense_pocket_advice",
    "build_defense_pockets",
    "chebyshev",
    "direction_of",
    "endangered_of",
    "resolve_pocket_config",
    "suggested_raid_force",
]

"""Pure projections from runtime artifacts to API read models (P5-4).

Ports the legacy TypeScript Command Center audit / map / alliance / shop
projections to Python on top of the P5-3 data base (``paths`` / ``jsonl`` /
``cache`` / ``registry`` / ``survey_db``). Each module exposes a deterministic
aggregation core (testable with synthetic fixtures) plus a thin loader that
reads the same runtime artifacts the TypeScript oracle reads.

Aggregation semantics are ported 1:1 from the TS oracle and verified by golden
tests; where the P5-3 survey schema lacks a TS table (``sync_meta`` /
``resource_events`` / ``chunks``) the loader degrades to empty inputs and the
difference is registered in the module docstring (fail-closed, never guessed).
"""

from .alignment import aggregate_alignment, load_alignment_audit
from .alliance_advice import build_alliance_advice_payload, load_alliance_advice
from .alliance_cluster import (
    CLUSTER_LINK_DIST,
    COHESION_MAX_DIST,
    build_alliance_cluster_view,
    cluster_input_of_members,
    load_alliance_cluster,
)
from .alliance_defense import build_alliance_defense_payload, load_alliance_defense
from .alliance_mining import assign_alliance_mining, build_observers_by_cell, load_alliance_mining
from .alliance_snapshot import build_alliance_snapshot_payload, load_alliance_snapshot
from .alliance_survey import TENANT_COLORS, aggregate_alliance_survey, load_alliance_survey
from .arbitrations import arbitration_file, list_arbitrations, load_arbitrations
from .conflicts import DEFAULT_WINDOW as CONFLICT_DEFAULT_WINDOW
from .conflicts import aggregate_human_conflict, load_human_conflict
from .consensus_mining import enrich_consensus_mining, load_consensus_mining
from .core_trails import load_core_trails_from_survey_db
from .decision_input import build_decision_input, load_decision_input
from .decisions import (
    DECISION_TREND_STEPS,
    DECISION_TREND_WINDOW,
    DEFAULT_RECORDS,
    aggregate_decision_audit,
    aggregate_decision_trend,
    load_decision_audit,
    load_decision_trend,
)
from .enemy_cores import (
    DEFAULT_ENEMY_CORE_OPTS,
    build_enemy_core_states,
    load_enemy_cores,
)
from .enemy_heat import load_enemy_heat
from .events import EVENT_KINDS, load_events
from .exploration import load_exploration
from .exploration_coverage import (
    CHUNK_SIZE,
    RESURVEY_CAP,
    RESURVEY_RADIUS_CHUNKS,
    compute_exploration_stats,
    load_alliance_exploration,
)
from .human import DEFAULT_LIMIT as HUMAN_AUDIT_DEFAULT_LIMIT
from .human import MAX_KEEP as HUMAN_AUDIT_MAX_KEEP
from .human import load_human_audit, read_human_audit
from .leaderboard import SNAPSHOT_STALE_SECONDS, load_leaderboard_intel
from .lifecycle import aggregate_lifecycle, load_lifecycle_audit
from .map_lod import MAP_LOD_CHUNK, aggregate_map_lod, load_map_lod
from .mine_patterns import load_mine_patterns
from .mines import (
    DEFAULT_TREND_STEPS,
    DEFAULT_TREND_WINDOW,
    RESOURCE_FRESH_WINDOW_TICKS,
    aggregate_mine_utilization,
    aggregate_mine_utilization_trend,
    load_mine_utilization,
    load_mine_utilization_trend,
)
from .mining_effectiveness import (
    FRESH_TICKS,
    aggregate_allocation_effectiveness,
    load_mining_effectiveness,
)
from .redeem import MAX_KEEP as REDEEM_MAX_KEEP
from .redeem import load_redeem_history
from .shop_history import (
    aggregate_shop_history,
    load_shop_history,
    load_shop_history_entries,
    normalize_products,
    should_append,
    snapshot_signature,
)
from .snapshots import load_plan, load_world
from .survey import (
    load_chunks_db,
    load_lifecycle_db,
    load_spend_trend,
    load_survey,
    load_survey_db,
    load_tenant_survey_cached,
    load_unit_lifecycle_db,
)
from .survey_mine import load_survey_mine
from .trail import DEFAULT_LIMIT as TRAIL_DEFAULT_LIMIT
from .trail import SOURCES as AUDIT_SOURCES
from .trail import load_audit_trail, merge_audit_trails, normalize_audit_trails
from .workers import DEFAULT_WINDOW as WORKERS_DEFAULT_WINDOW
from .workers import RECENT_TICKS as WORKERS_RECENT_TICKS
from .workers import aggregate_worker_liveness, load_worker_liveness_audit

__all__ = [
    "AUDIT_SOURCES",
    "aggregate_alignment",
    "load_alignment_audit",
    "CLUSTER_LINK_DIST",
    "build_alliance_defense_payload",
    "build_alliance_snapshot_payload",
    "COHESION_MAX_DIST",
    "CONFLICT_DEFAULT_WINDOW",
    "DECISION_TREND_STEPS",
    "DEFAULT_ENEMY_CORE_OPTS",
    "EVENT_KINDS",
    "DECISION_TREND_WINDOW",
    "DEFAULT_RECORDS",
    "DEFAULT_TREND_STEPS",
    "DEFAULT_TREND_WINDOW",
    "FRESH_TICKS",
    "HUMAN_AUDIT_DEFAULT_LIMIT",
    "HUMAN_AUDIT_MAX_KEEP",
    "MAP_LOD_CHUNK",
    "REDEEM_MAX_KEEP",
    "RESOURCE_FRESH_WINDOW_TICKS",
    "TENANT_COLORS",
    "TRAIL_DEFAULT_LIMIT",
    "TRAIL_DEFAULT_LIMIT",
    "build_enemy_core_states",
    "WORKERS_DEFAULT_WINDOW",
    "WORKERS_RECENT_TICKS",
    "aggregate_allocation_effectiveness",
    "aggregate_alliance_survey",
    "aggregate_decision_audit",
    "aggregate_decision_trend",
    "aggregate_human_conflict",
    "aggregate_map_lod",
    "aggregate_mine_utilization",
    "aggregate_mine_utilization_trend",
    "aggregate_shop_history",
    "aggregate_worker_liveness",
    "arbitration_file",
    "assign_alliance_mining",
    "cluster_input_of_members",
    "build_decision_input",
    "load_alliance_cluster",
    "load_alliance_mining",
    "build_alliance_cluster_view",
    "build_observers_by_cell",
    "list_arbitrations",
    "load_alliance_defense",
    "load_alliance_survey",
    "load_alliance_snapshot",
    "aggregate_lifecycle",
    "load_arbitrations",
    "load_audit_trail",
    "load_lifecycle_audit",
    "load_decision_audit",
    "load_decision_input",
    "load_events",
    "load_decision_trend",
    "load_human_audit",
    "load_human_conflict",
    "load_map_lod",
    "load_mine_utilization",
    "load_mine_utilization_trend",
    "CHUNK_SIZE",
    "RESURVEY_CAP",
    "RESURVEY_RADIUS_CHUNKS",
    "SNAPSHOT_STALE_SECONDS",
    "build_alliance_advice_payload",
    "compute_exploration_stats",
    "enrich_consensus_mining",
    "load_alliance_advice",
    "load_alliance_exploration",
    "load_consensus_mining",
    "load_core_trails_from_survey_db",
    "load_enemy_heat",
    "load_enemy_heat",
    "load_enemy_cores",
    "load_leaderboard_intel",
    "load_mine_patterns",
    "load_mining_effectiveness",
    "load_plan",
    "load_shop_history",
    "load_shop_history",
    "load_chunks_db",
    "load_exploration",
    "load_lifecycle_db",
    "load_spend_trend",
    "load_survey",
    "load_survey_db",
    "load_tenant_survey_cached",
    "load_unit_lifecycle_db",
    "load_survey_mine",
    "load_redeem_history",
    "load_shop_history_entries",
    "load_worker_liveness_audit",
    "merge_audit_trails",
    "normalize_audit_trails",
    "normalize_products",
    "read_human_audit",
    "load_world",
    "should_append",
    "snapshot_signature",
]

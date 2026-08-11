/* Arena 指挥面板前端 - API 拉取（无 DOM/state 依赖；URL 由调用方传入）。
 *
 * P5-7：数据访问层已切换到 OpenAPI 生成的类型化 client
 * （../../../../src/arena_hero_agent/command_center/generated/ts/），本文件
 * re-export 全部生成的类型化函数与类型；getJSON / fetchJSONWithETag 保留为
 * legacy 兼容入口（已标 @deprecated），内部委托给生成 client，签名不变。
 * 新代码请直接使用生成函数（getMap / getStream / getShop ...）。 */

import { ccGet, ccGetEtag } from "../../../../src/arena_hero_agent/command_center/generated/ts/client.ts";

export {
  deleteCommand, deleteRegistryAgentsId, getAgents, getAllianceAdvice, getAllianceCluster, getAllianceDefense, getAllianceDirector, getAllianceExploration, getAllianceMining, getAllianceSnapshot, getAllianceSurvey, getAllianceSurveyArbitrations,
  getAllianceSurveyMining, getAuditAlignment, getAuditDecisions, getAuditDecisionsTrend, getAuditHuman, getAuditHumanConflicts, getAuditLifecycle, getAuditMines, getAuditMinesTrend, getAuditMiningEffectiveness, getAuditOverview, getAuditTrail,
  getAuditWorkers, getCommands, getDeeds, getDeedsJournal, getEvents, getExploration, getHealthPipeline, getIntel, getIntelHeat, getLeaderboard, getMap, getMapLod,
  getOverview, getPlan, getRedeemHistory, getRegistryAgents, getReplay, getShop, getShopHistory, getShopMe, getShopOrders, getStream, getSurvey, getSurveyDecisionInput,
  getSurveyEnemyCores, getSurveyMine, getSurveyMinePatterns, getTenants, getWorld, postAllianceSurveyArbitrate, postAllianceSurveyArbitrateClear, postCommand, postCommandClear, postCommandGoal, postCommandMode, postIngestAgents,
  postLeaderboardRefresh, postRedeem, postRegistryAgents, postRegistryKeys, postShopHistoryRefresh, postShopOrder,
} from "../../../../src/arena_hero_agent/command_center/generated/ts/client.ts";

export type {
  DeleteCommandParams, DeleteRegistryAgentsIdParams, GetAgentsParams, GetAllianceAdviceParams, GetAllianceClusterParams, GetAllianceDefenseParams, GetAllianceDirectorParams, GetAllianceExplorationParams, GetAllianceMiningParams, GetAllianceSnapshotParams, GetAllianceSurveyParams, GetAllianceSurveyArbitrationsParams,
  GetAllianceSurveyMiningParams, GetAuditAlignmentParams, GetAuditDecisionsParams, GetAuditDecisionsTrendParams, GetAuditHumanParams, GetAuditHumanConflictsParams, GetAuditLifecycleParams, GetAuditMinesParams, GetAuditMinesTrendParams, GetAuditMiningEffectivenessParams, GetAuditOverviewParams, GetAuditTrailParams,
  GetAuditWorkersParams, GetCommandsParams, GetDeedsParams, GetDeedsJournalParams, GetEventsParams, GetExplorationParams, GetHealthPipelineParams, GetIntelParams, GetIntelHeatParams, GetLeaderboardParams, GetMapParams, GetMapLodParams,
  GetOverviewParams, GetPlanParams, GetRedeemHistoryParams, GetRegistryAgentsParams, GetReplayParams, GetShopParams, GetShopHistoryParams, GetShopMeParams, GetShopOrdersParams, GetStreamParams, GetSurveyParams, GetSurveyDecisionInputParams,
  GetSurveyEnemyCoresParams, GetSurveyMineParams, GetSurveyMinePatternsParams, GetTenantsParams, GetWorldParams, PostAllianceSurveyArbitrateParams, PostAllianceSurveyArbitrateClearParams, PostCommandParams, PostCommandClearParams, PostCommandGoalParams, PostCommandModeParams, PostIngestAgentsParams,
  PostLeaderboardRefreshParams, PostRedeemParams, PostRegistryAgentsParams, PostRegistryKeysParams, PostShopHistoryRefreshParams, PostShopOrderParams,
  DeleteCommandResponse, DeleteRegistryAgentsIdResponse, GetAgentsResponse, GetAllianceAdviceResponse, GetAllianceClusterResponse, GetAllianceDefenseResponse, GetAllianceDirectorResponse, GetAllianceExplorationResponse, GetAllianceMiningResponse, GetAllianceSnapshotResponse, GetAllianceSurveyResponse, GetAllianceSurveyArbitrationsResponse,
  GetAllianceSurveyMiningResponse, GetAuditAlignmentResponse, GetAuditDecisionsResponse, GetAuditDecisionsTrendResponse, GetAuditHumanResponse, GetAuditHumanConflictsResponse, GetAuditLifecycleResponse, GetAuditMinesResponse, GetAuditMinesTrendResponse, GetAuditMiningEffectivenessResponse, GetAuditOverviewResponse, GetAuditTrailResponse,
  GetAuditWorkersResponse, GetCommandsResponse, GetDeedsResponse, GetDeedsJournalResponse, GetEventsResponse, GetExplorationResponse, GetHealthPipelineResponse, GetIntelResponse, GetIntelHeatResponse, GetLeaderboardResponse, GetMapResponse, GetMapLodResponse,
  GetOverviewResponse, GetPlanResponse, GetRedeemHistoryResponse, GetRegistryAgentsResponse, GetReplayResponse, GetShopResponse, GetShopHistoryResponse, GetShopMeResponse, GetShopOrdersResponse, GetStreamResponse, GetSurveyResponse, GetSurveyDecisionInputResponse,
  GetSurveyEnemyCoresResponse, GetSurveyMineResponse, GetSurveyMinePatternsResponse, GetTenantsResponse, GetWorldResponse, PostAllianceSurveyArbitrateResponse, PostAllianceSurveyArbitrateClearResponse, PostCommandResponse, PostCommandClearResponse, PostCommandGoalResponse, PostCommandModeResponse, PostIngestAgentsResponse,
  PostLeaderboardRefreshResponse, PostRedeemResponse, PostRegistryAgentsResponse, PostRegistryKeysResponse, PostShopHistoryRefreshResponse, PostShopOrderResponse,
  Tenant,
  TenantWithAll,
} from "../../../../src/arena_hero_agent/command_center/generated/ts/types.ts";

/** @deprecated 用生成的类型化函数（getOverview / getStream / ...，见本文件 re-export）。 */
export async function getJSON<T = any>(url: string, timeout = 20000): Promise<T> {
  return ccGet<T>(url, { timeoutMs: timeout });
}

/** @deprecated 同上；仅弱 ETag 端点（/api/map）需要，等价生成函数 getMap()。 */
export async function fetchJSONWithETag<T = any>(url: string, timeout = 20000): Promise<T | null> {
  return ccGetEtag<T>(url, { timeoutMs: timeout });
}

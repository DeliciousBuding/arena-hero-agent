/** 官方商店 API 客户端：Cookie 本地保存 + 经面板服务器内存转发，不落盘。
 *
 * P5-7：数据访问层切换到 OpenAPI 生成的类型化 client
 * （../../../../src/arena_hero_agent/command_center/generated/ts/）。shopRequest
 * 保留为 legacy 兼容入口（已标 @deprecated），内部委托给生成 client，签名不变；
 * 本文件 re-export 生成的商店/兑换函数与类型，新代码优先用它们。
 * 生成响应类型当前为 unknown（OpenAPI 200 schema 暂未落地），调用时传显式泛型，
 * 如 getShop<{ products?: ShopProduct[] }>()。 */

import { ccGet, ccSend } from "../../../../src/arena_hero_agent/command_center/generated/ts/client.ts";
import type { CcRequestOptions } from "../../../../src/arena_hero_agent/command_center/generated/ts/client.ts";

export const SHOP_COOKIE_KEY = "arena-cc.shop-cookie";

/** @deprecated 手写商店商品类型；与生成端点 /api/shop 重叠，用 getShop<...>() 泛型代替。 */
export interface ShopProduct {
  id: string;
  name?: string;
  description?: string;
  resource_cost?: number;
  available_stock?: number;
  purchase_limit?: number;
  out_of_stock?: boolean;
}

/** @deprecated 手写商店账户类型；与生成端点 /api/shop/me 重叠，用 getShopMe<...>() 泛型代替。 */
export interface ShopMe {
  username?: string;
  resources?: number;
}

/** @deprecated 手写商店订单类型；与生成端点 /api/shop/orders 重叠，用 getShopOrders<...>() 泛型代替。 */
export interface ShopOrder {
  id?: string;
  product_name?: string;
  status?: string;
  created_at?: string;
}

export function shopCookieValue(): string {
  return (localStorage.getItem(SHOP_COOKIE_KEY) ?? "").trim();
}

export function saveShopCookie(value: string): void {
  localStorage.setItem(SHOP_COOKIE_KEY, value.trim());
}

/** @deprecated 通用路径入口；委托给生成 client（getShop / getShopMe / getShopOrders / postShopOrder / getLeaderboard / postLeaderboardRefresh ...）。 */
export async function shopRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers ?? {});
  const cookie = shopCookieValue();
  if (cookie) headers.set("X-Shop-Cookie", cookie);
  const opts: CcRequestOptions = { headers: Object.fromEntries(headers.entries()) };
  const method = (options.method ?? "GET").toUpperCase();
  if (method === "GET") return ccGet<T>(path, opts);
  return ccSend<T>(path, { ...options, method }, opts);
}

export interface RedeemHistoryEntry {
  at: string;
  code: string;
  status: string;
}

export function loadRedeemHistory(): RedeemHistoryEntry[] {
  try {
    const list = JSON.parse(localStorage.getItem("arena-cc.redeem-history") ?? "[]");
    return Array.isArray(list) ? (list as RedeemHistoryEntry[]) : [];
  } catch {
    return [];
  }
}

export function pushRedeemHistory(entry: RedeemHistoryEntry): RedeemHistoryEntry[] {
  const list = [entry, ...loadRedeemHistory()].slice(0, 20);
  localStorage.setItem("arena-cc.redeem-history", JSON.stringify(list));
  return list;
}

export function clearRedeemHistory(): void {
  localStorage.removeItem("arena-cc.redeem-history");
}

export {
  getShop,
  getShopHistory,
  getShopMe,
  getShopOrders,
  postShopHistoryRefresh,
  postShopOrder,
  getRedeemHistory,
  postRedeem,
} from "../../../../src/arena_hero_agent/command_center/generated/ts/client.ts";

export type {
  GetShopParams,
  GetShopResponse,
  GetShopHistoryParams,
  GetShopHistoryResponse,
  GetShopMeParams,
  GetShopMeResponse,
  GetShopOrdersParams,
  GetShopOrdersResponse,
  PostShopHistoryRefreshParams,
  PostShopHistoryRefreshResponse,
  PostShopOrderParams,
  PostShopOrderResponse,
  GetRedeemHistoryParams,
  GetRedeemHistoryResponse,
  PostRedeemParams,
  PostRedeemResponse,
} from "../../../../src/arena_hero_agent/command_center/generated/ts/types.ts";

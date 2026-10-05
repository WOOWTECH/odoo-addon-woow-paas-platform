/** @odoo-module **/

import { session } from "@web/session";

/**
 * #275 cosmetic feature gate — 同步讀 bootstrap 注入的 woow_show_projects。
 * paas_app 把它放進 session_info；Odoo 啟動時 session service 消費
 * `odoo.__session_info__` 並存進 @web/session 的 `session` 物件。router 與
 * nav 共用此單一同步來源，避免 async /api/me 造成的 redirect race / Projects 閃現。
 *
 * fail-closed：缺 key / 非 true → false（隱藏），與後端 _show_projects 對齊。
 *
 * ⚠️ #275 headed E2E 教訓：勿改讀 bare global `odoo.__session_info__` ——
 * Odoo 的 session service 於 boot 時讀取後即 `delete odoo.__session_info__`
 * （防洩漏），故 runtime（nav/router render 時）永遠讀到 undefined →
 * Projects 連 flag ON 也永遠隱藏。實測 `@web/session` 確在 assets_paas
 * bundle 內（moduleCount 1310），且 `session.woow_show_projects` 帶正確值。
 */
export function showProjects() {
    return session.woow_show_projects === true;
}

/**
 * #1453：「說明與支援」網址的唯一來源（側欄、頭像選單、儲值頁共用）。
 * 後端 `_help_url()` 注入 session（config_parameter `woow_paas_platform.help_url`，
 * 缺值時用預設幫助中心）；這裡的預設值只在 session 沒帶時（舊頁面快取）兜底，
 * 與後端 DEFAULT_HELP_URL 相同。原本三處寫死的 docs.woowtech.io 是不存在的網域。
 */
export const DEFAULT_HELP_URL = "https://aiot.woowtech.io/blog/help-center-7";

export function helpUrl() {
    const url = session.woow_help_url;
    return typeof url === "string" && /^https?:\/\//.test(url) ? url : DEFAULT_HELP_URL;
}

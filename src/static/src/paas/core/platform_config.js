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

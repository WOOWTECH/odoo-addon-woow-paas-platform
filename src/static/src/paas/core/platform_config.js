/** @odoo-module **/

/**
 * #275 cosmetic feature gate — 同步讀 bootstrap（paas_app 把 woow_show_projects
 * 注入 odoo.__session_info__）。router 與 nav 共用此單一同步來源，避免 async
 * /api/me 造成的 redirect race / Projects 閃現。
 *
 * fail-closed：缺 key / 非 true → false（隱藏），與後端 _show_projects 對齊。
 * 直讀 global 與既有 ai_service.js:149 / AiChat.js:573（bare `odoo.*`）慣例一致，
 * 不 import @web/session（assets_paas 以 web.assets_backend 為基，不保證該 ESM
 * module 在 app bundle 內）。
 */
export function showProjects() {
    return odoo?.__session_info__?.woow_show_projects === true;
}

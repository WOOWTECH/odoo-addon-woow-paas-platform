/** @odoo-module **/

import { formatUserDate } from "../utils/datetime_helpers";

/**
 * Workspace role constants
 */
export const ROLES = {
    OWNER: 'owner',
    ADMIN: 'admin',
    USER: 'user',
    GUEST: 'guest',
};

/**
 * Roles that can manage workspace members
 */
export const MANAGEMENT_ROLES = [ROLES.OWNER, ROLES.ADMIN];

/**
 * Get the CSS class for a role badge
 * @param {string} role - The role value (owner, admin, user, guest)
 * @returns {string} CSS class name for the badge
 */
export function getRoleBadgeClass(role) {
    const classes = {
        [ROLES.OWNER]: "o_woow_badge_purple",
        [ROLES.ADMIN]: "o_woow_badge_blue",
        [ROLES.USER]: "o_woow_badge_green",
        [ROLES.GUEST]: "o_woow_badge_gray",
    };
    return classes[role] || "o_woow_badge_gray";
}

/**
 * 依 Odoo 使用者語系格式化日期。
 *
 * ⚠ #107 前這裡寫死 `zh-TW`（全 app 唯一一處非 en-US 的硬編值），
 * 與 `utils/billing_helpers.js` 寫死的 `en-US` 並存 ⇒ 同一畫面上兩種日期格式。
 *
 * ⚠ 原本的 `options.long`（長月名 vs 短月名）已移除：Odoo 的 localization
 * 只提供單一 `res.lang.date_format`，沒有長短兩版的概念。
 * 全 repo 8 個呼叫端中有 2 個原本傳 `{ long: true }`
 * （`WorkspaceDetailPage.js` 與 `pages/service/tabs/OverviewTab.js`）
 * ⇒ 這兩處的月份呈現會改變（原 `2026年8月26日` 長月名 → 現 `2026年08月26日`），
 * 已同批把多餘的參數移除。
 *
 * @param {string|null|undefined} dateString
 * @returns {string} 空值或無法解析時回空字串（維持原契約）
 */
export function formatDate(dateString) {
    return formatUserDate(dateString, "");
}

/**
 * Get initials from a name
 * @param {string} name - Full name
 * @returns {string} Up to 2 character initials
 */
export function getInitials(name) {
    if (!name) return "?";
    return name.split(" ").map(n => n[0]).join("").toUpperCase().slice(0, 2);
}

/**
 * Get priority star states for a 3-star display
 * @param {string|number} priority - Priority value ('0'-'3')
 * @returns {boolean[]} Array of 3 booleans indicating filled stars
 */
export function getPriorityStars(priority) {
    const level = parseInt(priority, 10) || 0;
    return [level >= 1, level >= 2, level >= 3];
}

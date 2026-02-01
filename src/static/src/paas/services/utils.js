/** @odoo-module **/

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
 * Format a date string for display
 * @param {string} dateString - ISO date string
 * @param {Object} options - Formatting options
 * @param {boolean} options.long - Use long month format (default: false)
 * @returns {string} Formatted date string
 */
export function formatDate(dateString, options = {}) {
    if (!dateString) return "";
    const date = new Date(dateString);
    return date.toLocaleDateString("zh-TW", {
        year: "numeric",
        month: options.long ? "long" : "short",
        day: "numeric",
    });
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

/** @odoo-module **/

/**
 * Route → navigation section (#1310).
 *
 * Sidebar and BottomNav used to keep their own `isActive` lists and both
 * missed sub-pages (e.g. nothing was highlighted on a workspace's service
 * page in the sidebar; Billing lost its highlight on Top-up / Invoices in the
 * bottom nav). Every route name in `router.js` is mapped here once.
 */
const SECTION_BY_ROUTE = {
  dashboard: "dashboard",
  workspaces: "workspaces",
  "workspace-detail": "workspaces",
  "workspace-team": "workspaces",
  "smarthome-detail": "workspaces",
  "security-access-detail": "workspaces",
  marketplace: "workspaces",
  configure: "workspaces",
  "service-detail": "workspaces",
  deployments: "workspaces",
  "ai-projects": "projects",
  "project-detail": "projects",
  "ai-project-kanban": "projects",
  "ai-tasks": "projects",
  "ai-task-detail": "projects",
  "ai-chat": "projects",
  // LLM-01c：只有一個 routeName（子頁共用），側欄與底欄都亮「AI 模型」。
  llm: "llm",
  billing: "billing",
  "billing-subscriptions": "billing",
  "billing-transactions": "billing",
  "billing-invoices": "invoices",
  "billing-invoice-detail": "invoices",
  "billing-topup": "topup",
  settings: "settings",
  "settings-profile": "settings",
  "settings-payment-methods": "settings",
  "settings-payment-methods-add": "settings",
  "reconcile-report": "reconcile",
};

/** Sidebar granularity: Invoices / Top-up / Reconciliation have their own items. */
export function sidebarSection(routeName) {
  return SECTION_BY_ROUTE[routeName] || null;
}

/** Bottom-nav granularity: only five or six tabs, so finance sub-pages fold into Billing. */
export function bottomNavSection(routeName) {
  const section = SECTION_BY_ROUTE[routeName] || null;
  if (section === "invoices" || section === "topup") return "billing";
  if (section === "reconcile") return "settings";
  return section;
}

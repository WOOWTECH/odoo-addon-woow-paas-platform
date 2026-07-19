/** @odoo-module **/

/**
 * 決定性預設：最低 price × month × 0 席（base 自身）。
 * month 組優先（無 month 才退全體）；組內 price 升序、id tie-breaker。
 * 空清單回 undefined。取代 AppConfigurationPage 等處的 `plans[0].id` 語意任意預設。
 */
export function pickDefaultPlan(plans) {
  const months = plans.filter((p) => p.unit === "month");
  const pool = months.length ? months : plans;
  if (!pool.length) return undefined;
  return [...pool].sort((a, b) => a.price - b.price || a.id - b.id)[0].id;
}

/**
 * 選中 plan（base 或變體）的絕對總價與週期單位；找不到回 null。
 * 總價條與（B2 的）席數 chip 共用此取價，確保顯示同一絕對數字、單位一致。
 * 變體與其 base 同 cycle，故 unit 一律取 base 的 unit。
 */
export function resolveSelected(plans, selectedPlanId) {
  for (const plan of plans) {
    if (plan.id === selectedPlanId) {
      return { price: plan.price, unit: plan.unit };
    }
    const v = (plan.variants || []).find((x) => x.plan_id === selectedPlanId);
    if (v) return { price: v.price, unit: plan.unit };
  }
  return null;
}

/**
 * 依 group_key 收攏 plans，每 group 含 month/year 兩 cycle 的 base。
 * group_key 為 null（無 xmlid）時 fallback `id:<id>` 當獨立 group（不與他人配對）。
 * 回 [{groupKey, name, byCycle:{month, year}}]，group 依 base price 升序
 * （price 與 tier 單調；絕不用 group_key 字串排序——SA entry/pro 會亂序）。
 */
export function groupPlansByCycle(plans) {
  const groups = new Map();
  for (const p of plans) {
    const key = p.group_key || `id:${p.id}`;
    if (!groups.has(key)) {
      groups.set(key, { groupKey: key, name: p.name, byCycle: {} });
    }
    groups.get(key).byCycle[p.unit] = p;
  }
  return [...groups.values()].sort((a, b) => {
    const pa = (a.byCycle.month || a.byCycle.year).price;
    const pb = (b.byCycle.month || b.byCycle.year).price;
    return pa - pb;
  });
}

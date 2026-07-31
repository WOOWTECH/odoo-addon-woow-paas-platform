/** @odoo-module **/

/**
 * Template 價格標籤的單一格式化來源（#478）。
 *
 * marketplace 卡片與 configure 頁 header 顯示同一件事（這個商品從多少起跳），
 * 兩邊各寫一份就會漂移——#478 本身就是「卡片一個判準、plan 區另一個判準」的產物，
 * 同型前例還有 F-449-4（Reset 按鈕前端 slug 白名單 vs 後端 has_admin_gui）。
 * 要改顯示規則就改這裡一處。
 *
 * 資料來自後端 `_template_pricing`（controllers/paas.py），可見性判準已與前台
 * plans 端點對齊（active ＋ 非 storefront_hidden）。
 *
 * @param {{has_plans: boolean, min_price: number|null, unit: string|null,
 *          tier_count: number, is_free: boolean}|null|undefined} pricing
 * @returns {string} 可直接顯示的標籤；**空字串代表不該渲染標籤**（查無價格時
 *                   保持沉默，而不是像 #478 修前那樣編一個 "Free" 出來）
 */
export function formatTemplatePrice(pricing) {
  if (!pricing || !pricing.has_plans || pricing.min_price === null ||
      pricing.min_price === undefined) {
    return "";
  }
  // 多個價位（如 n8n T1–T4）才是「起價」；單一價位加「起」會讓使用者以為還有
  // 更貴的選項。
  const isFrom = pricing.tier_count > 1;
  if (pricing.is_free) {
    // ⚠ 起價 0 但**還有更貴的階**（freemium：0 元入門＋付費階）時不能只寫
    // "Free" —— 那是 #478「謊報免費」的鏡像：使用者以為整個商品免費，點進去
    // 才看到付費階。這種商品目前 seed 觸及不到，但規則先立好。
    return isFrom ? "Free 起" : "Free";
  }
  // 單位縮寫沿用 PlanSelector._unitLabel 的三態慣例（mo／yr／原樣），讓卡片、
  // header 與下一頁的 plan 選卡區、總計列字樣一致；day/week 之類的週期照原字串
  // 顯示，而不是被誤標成 mo。
  const unit =
    pricing.unit === "month" ? "mo" : pricing.unit === "year" ? "yr" : pricing.unit;
  const price = `${pricing.min_price} pts/${unit}`;
  return isFrom ? `${price} 起` : price;
}

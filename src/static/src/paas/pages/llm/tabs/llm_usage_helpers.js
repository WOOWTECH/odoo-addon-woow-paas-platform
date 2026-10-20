/** @odoo-module **/

/** Pure helpers shared by the usage tab and its UI harness. */
export const USAGE_MAX_DAYS = 31;
export const USAGE_HISTORY_DAYS = 180;

export function taipeiToday(now = new Date()) {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Taipei", year: "numeric", month: "2-digit", day: "2-digit",
  }).format(now);
}

function shiftDay(isoDate, delta) {
  const date = new Date(`${isoDate}T12:00:00Z`);
  date.setUTCDate(date.getUTCDate() + delta);
  return date.toISOString().slice(0, 10);
}

export function rangeDates(range, today = taipeiToday()) {
  if (range === "custom") return null;
  if (range === "today") return { start: today, end: today };
  if (range === "7d") return { start: shiftDay(today, -6), end: today };
  if (range === "30d") return { start: shiftDay(today, -29), end: today };
  if (range === "month") return { start: `${today.slice(0, 7)}-01`, end: today };
  if (range === "last_month") {
    const firstThisMonth = `${today.slice(0, 7)}-01`;
    const lastMonthEnd = shiftDay(firstThisMonth, -1);
    return { start: `${lastMonthEnd.slice(0, 7)}-01`, end: lastMonthEnd };
  }
  return null;
}

export function validateUsageDates(start, end, today = taipeiToday()) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(start || "") || !/^\d{4}-\d{2}-\d{2}$/.test(end || "")) {
    return "date";
  }
  const startMs = Date.parse(`${start}T00:00:00Z`);
  const endMs = Date.parse(`${end}T00:00:00Z`);
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs)
      || new Date(startMs).toISOString().slice(0, 10) !== start
      || new Date(endMs).toISOString().slice(0, 10) !== end) return "date";
  if (start > end || (endMs - startMs) / 86400000 + 1 > USAGE_MAX_DAYS) return "range";
  if (end > today || start < shiftDay(today, -(USAGE_HISTORY_DAYS - 1))) return "history";
  return null;
}

export function usageRequestParams(filters, page = 1) {
  const fixed = filters.range === "custom" ? { start: filters.start, end: filters.end } : rangeDates(filters.range);
  return {
    range: filters.range,
    ...(fixed || {}),
    ...(filters.keyId ? { key_id: Number(filters.keyId) } : {}),
    ...(filters.model ? { model: filters.model } : {}),
    ...(filters.status ? { status: filters.status } : {}),
    page,
    page_size: 20,
  };
}

export function shouldRetryUsageFill(response, retries) {
  return retries < 3 && !!response?.success && response.data?.complete === false
    && response.data?.data_as_of == null;
}

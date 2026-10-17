/** @odoo-module **/
import { _t } from "@web/core/l10n/translation";

/**
 * spec `ignored_when`（INFRA-TS-004-1）：某個條件下 chart 會略過的設定欄位。
 *
 * 形態 `"ignored_when": {"key": "<同一份 specs 裡的條件欄位>", "values": ["<值>", …]}`，
 * 例：VPN 節點控制平面選「同工作區 Headscale」時 chart 忽略 `tailscale.tags`。
 * 判準與後端 `CloudAppTemplate._ignored_when_value_specs`／`_ignored_when_violation`
 * 相同：宣告寫錯一律當作沒有（fail-open）；後端是 authoritative 的那一道，這裡只是
 * 讓租戶在送出前就看得到原因。
 */

function specList(specs) {
  if (Array.isArray(specs)) {
    return specs;
  }
  return [...(specs?.required || []), ...(specs?.optional || [])];
}

/**
 * 依 spec 取值：先扁平 dot-path 鍵，再巢狀，最後 spec 預設（與 HelmValueForm.getValue 同序）。
 */
export function lookupSpecValue(values, spec) {
  const source = values || {};
  if (source[spec.key] !== undefined) {
    return source[spec.key];
  }
  let node = source;
  for (const part of String(spec.key).split(".")) {
    if (node === null || typeof node !== "object" || node[part] === undefined) {
      node = undefined;
      break;
    }
    node = node[part];
  }
  if (node !== undefined) {
    return node;
  }
  return spec.default !== undefined ? spec.default : "";
}

export function isBlankSpecValue(value) {
  if (value === null || value === undefined || value === false) {
    return true;
  }
  if (typeof value === "string") {
    return value.trim() === "";
  }
  if (Array.isArray(value)) {
    return value.length === 0;
  }
  return false;
}

/**
 * 條件成立時回 `{ condition, choice }`（條件欄位的 label 與目前選項的 label），否則 null。
 */
export function ignoredWhenState(spec, specs, values) {
  const rule = spec?.ignored_when;
  if (!rule || typeof rule !== "object" || Array.isArray(rule)) {
    return null;
  }
  const condKey = rule.key;
  const allowed = rule.values;
  if (typeof condKey !== "string" || condKey === spec.key) {
    return null;
  }
  if (!Array.isArray(allowed) || !allowed.length || !allowed.every((v) => typeof v === "string")) {
    return null;
  }
  const condSpec = specList(specs).find((s) => s && s.key === condKey);
  if (!condSpec) {
    return null;
  }
  const current = lookupSpecValue(values, condSpec);
  if (current === null || current === undefined) {
    return null;
  }
  const choiceValue = String(current).trim();
  if (!allowed.map((v) => v.trim()).includes(choiceValue)) {
    return null;
  }
  const option = (condSpec.options || []).find(
    (opt) => opt && typeof opt === "object" && opt.value === choiceValue
  );
  return {
    condition: condSpec.label || condKey,
    choice: (option && option.label) || choiceValue,
  };
}

/** 編輯表單：欄位下的說明（條件不成立回 null）。 */
export function ignoredWhenMessage(spec, specs, values) {
  const state = ignoredWhenState(spec, specs, values);
  if (!state) {
    return null;
  }
  return _t('Not applied when "%(condition)s" is "%(choice)s"; leave this field empty.', state);
}

/** 檢視模式：欄位有值但條件成立（例：舊服務在 ignored_when 上線前存的 tags）。 */
export function ignoredWhenViewNote(spec, specs, values) {
  if (isBlankSpecValue(lookupSpecValue(values, spec))) {
    return null;
  }
  const state = ignoredWhenState(spec, specs, values);
  if (!state) {
    return null;
  }
  return _t('Not applied: "%(condition)s" is "%(choice)s".', state);
}

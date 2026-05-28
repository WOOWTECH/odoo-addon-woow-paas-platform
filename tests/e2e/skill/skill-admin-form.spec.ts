/**
 * E2E (browser) — skill admin form transition button UI wiring.
 *
 * Spec: docs/superpowers/specs/2026-05-28-skill-e2e-and-is-latest-drift-fix.md §④-b
 *
 * 測什麼（UI wiring）：
 * - admin 登入 → Skills menu → list → 開 draft skill form
 * - draft 初態：Publish + Archive 可見、Deprecate 隱藏、badge=Draft
 * - 點 Publish → badge=Published、statusbar 推進、is_latest=True、
 *   Publish 隱藏、Deprecate 出現
 * - 點 Deprecate → badge=Deprecated、is_latest=False、Publish 重現、
 *   Deprecate 隱藏
 * - 點 Archive → badge=Archived、所有 transition button 隱藏
 *
 * 每個關鍵切換點 screenshot 落地 /tmp/skill-e2e-screenshots/。
 *
 * 不測什麼（model test / Phase C / view smoke 已覆蓋）：
 * - lifecycle 規則正確性、跨多版本 recompute、menu admin-only、
 *   bundle 下載 / token auth、ACL/ir.rule。
 *
 * 前置：
 * 1. ORM 預建一筆 draft skill（建議透過 odoo shell / fixture script）
 * 2. 改 .py 後務必 rollout restart deploy/feat-skill-list-odoo-dev-sandbox-odoo
 *    並等 rollout 完成，否則 serve 進程跑舊 code 會誤判 button 行為
 *    （leader 強調曾因此誤判 action_deprecate）
 */
import { test, expect } from '@playwright/test';

const BASE_URL = process.env.SANDBOX_BASE_URL ?? 'http://localhost';
const ADMIN_LOGIN = process.env.ADMIN_LOGIN ?? 'admin';
const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD ?? 'admin';
const DB_NAME = process.env.DB_NAME ?? 'woow_feat_skill_list';
const SKILL_NAME = process.env.SKILL_E2E_NAME ?? 'browser-e2e-skill';
const SCREENSHOT_DIR = '/tmp/skill-e2e-screenshots';

async function loginAdmin(page) {
  await page.goto(`${BASE_URL}/web/login`);
  // 單 DB 部署可能無 db 欄位 — catch 並忽略
  await page.fill('input[name="db"]', DB_NAME).catch(() => {});
  await page.fill('input[name="login"]', ADMIN_LOGIN);
  await page.fill('input[name="password"]', ADMIN_PASSWORD);
  await page.click('button[type="submit"]');
  await page.waitForURL(/\/odoo|\/web/);
}

async function openSkillForm(page, skillName) {
  // 進 Woow PaaS → Skills menu（admin only） → list → 開 form
  await page.goto(`${BASE_URL}/odoo`);
  // 點 Woow PaaS app（具體 selector 依 admin theme 微調）
  await page.getByRole('link', { name: /woow paas/i }).click();
  await page.getByRole('link', { name: /^skills$/i }).click();
  // list 中點該 skill 進 form
  await page.getByText(skillName, { exact: false }).first().click();
  await page.waitForSelector('.o_form_view');
}

test.describe('Skill admin form — transition button UI wiring', () => {
  test('draft → publish → deprecate → archive', async ({ page }) => {
    await loginAdmin(page);
    await openSkillForm(page, SKILL_NAME);

    // ---- draft 初態 ----
    await expect(
      page.locator('.o_statusbar_buttons button:has-text("Publish")'),
    ).toBeVisible();
    await expect(
      page.locator('.o_statusbar_buttons button:has-text("Archive")'),
    ).toBeVisible();
    await expect(
      page.locator('.o_statusbar_buttons button:has-text("Deprecate")'),
    ).toBeHidden();
    // statusbar 高亮 draft
    await expect(
      page.locator('.o_statusbar_status .btn[data-value="draft"]'),
    ).toHaveClass(/active|btn-primary/);
    await page.screenshot({ path: `${SCREENSHOT_DIR}/01-draft.png` });

    // ---- 點 Publish ----
    await page
      .locator('.o_statusbar_buttons button:has-text("Publish")')
      .click();
    await page.waitForLoadState('networkidle');
    await expect(
      page.locator('.o_statusbar_status .btn[data-value="published"]'),
    ).toHaveClass(/active|btn-primary/);
    await expect(
      page.locator('.o_statusbar_buttons button:has-text("Publish")'),
    ).toBeHidden();
    await expect(
      page.locator('.o_statusbar_buttons button:has-text("Deprecate")'),
    ).toBeVisible();
    // is_latest checkbox / toggle 應為 True
    // 具體 selector 依 Odoo widget；boolean field 通常是
    // input[type=checkbox] 或 .o_boolean_toggle
    await expect(
      page.locator('[name="is_latest"] input').first(),
    ).toBeChecked();
    await page.screenshot({ path: `${SCREENSHOT_DIR}/02-published.png` });

    // ---- 點 Deprecate ----
    await page
      .locator('.o_statusbar_buttons button:has-text("Deprecate")')
      .click();
    await page.waitForLoadState('networkidle');
    await expect(
      page.locator('.o_statusbar_status .btn[data-value="deprecated"]'),
    ).toHaveClass(/active|btn-primary/);
    await expect(
      page.locator('.o_statusbar_buttons button:has-text("Publish")'),
    ).toBeVisible();  // deprecated 可 re-publish
    await expect(
      page.locator('.o_statusbar_buttons button:has-text("Deprecate")'),
    ).toBeHidden();
    await expect(
      page.locator('[name="is_latest"] input').first(),
    ).not.toBeChecked();
    await page.screenshot({ path: `${SCREENSHOT_DIR}/03-deprecated.png` });

    // ---- 點 Archive ----
    await page
      .locator('.o_statusbar_buttons button:has-text("Archive")')
      .click();
    await page.waitForLoadState('networkidle');
    await expect(
      page.locator('.o_statusbar_status .btn[data-value="archived"]'),
    ).toHaveClass(/active|btn-primary/);
    await expect(
      page.locator('.o_statusbar_buttons button:has-text("Publish")'),
    ).toBeHidden();
    await expect(
      page.locator('.o_statusbar_buttons button:has-text("Deprecate")'),
    ).toBeHidden();
    await expect(
      page.locator('.o_statusbar_buttons button:has-text("Archive")'),
    ).toBeHidden();
    await page.screenshot({ path: `${SCREENSHOT_DIR}/04-archived.png` });
  });
});

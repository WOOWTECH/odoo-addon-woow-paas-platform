/**
 * E2E (headed browser) — 服務詳情頁「Reset Admin Password」按鈕可見性。
 *
 * Issue: #464（#460 S3）｜Spec: #460 的 spec comment「Testing Decisions」段
 * 依據: docs/tracking/test-reports/449-password-carrier-stg-e2e-2026-07-29.md §8.2
 *
 * 測什麼（外部行為，非 getter 內部實作）：
 * - 真瀏覽器登入後逐一開啟四個**代表模板**的服務詳情頁，斷言 Reset 按鈕的可見性
 *   與該模板的 has_admin_gui 一致：
 *     hermes-agent  True  → 可見（#449／PR #455 新修族，修好的能力要按得到）
 *     mcp-k8s       True  → 可見（#375 三型 MCP 遺漏族，後端一直是好的）
 *     mcp-odoo      True  → 可見（原 slug 白名單族，不得回歸）
 *     n8n           False → 不可見（不得誤現一顆按了必然失敗的按鈕）
 * - 同時攔截該頁的 service payload，斷言 `template.has_admin_gui` 存在、型別為
 *   boolean、且與 DB ground truth 一致 —— 把「後端補欄位」與「前端讀欄位」兩段
 *   S1 修復在同一條真瀏覽器路徑上一起釘死。
 *
 * 不測什麼（Python 合約測試 src/tests/test_service_payload_has_admin_gui_462.py
 * 與 repo-lint 守線已覆蓋）：
 * - payload 欄位鏡射的單元層正確性、slug 與欄位反直覺搭配、include_details 路徑
 * - 「不得長出第三個判準」的靜態守線
 * - 按下按鈕之後的重設流程（#449 已於 stg 真機 E2E 驗過；#461 逾時議題不在此範圍）
 *
 * 為什麼不需要真部署：按鈕判準是 service payload 的 `template.has_admin_gui`，
 * 純前端渲染。fixture 只要 DB 裡有 state=running 且掛對 template 的 CloudService
 * 列即可 —— 毋須 operator、毋須真 Helm release。
 *
 * 前置（詳見 README.md）：
 *   1. sandbox 跑著且跑的是含 #460 S1 的程式碼
 *   2. bash .claude/skills/k8s-dev-sandbox/scripts/e2e-seed-reset-button.sh <slug> --context <ctx>
 *   3. export SANDBOX_BASE_URL=<sandbox 對外 URL>
 */
import { test, expect, Page } from '@playwright/test';
import * as fs from 'fs';

const BASE_URL = process.env.SANDBOX_BASE_URL ?? 'http://localhost:8069';
const ADMIN_LOGIN = process.env.ADMIN_LOGIN ?? 'admin';
const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD ?? 'admin';
const DB_NAME = process.env.DB_NAME ?? '';
// MUST stay in lock-step with e2e-seed-reset-button.sh 的 --manifest 預設值
const FIXTURE_PATH =
  process.env.RESET_BTN_E2E_FIXTURE ?? '/tmp/reset-button-e2e-fixture.json';
const SCREENSHOT_DIR =
  process.env.RESET_BTN_E2E_SHOTS ?? '/tmp/reset-button-e2e-screenshots';

const RESET_BUTTON_LABEL = 'Reset Admin Password';
/** 一定會渲染的對照按鈕 —— 沒有它就代表 action bar 根本沒出來（見 openServiceDetail）。 */
const CONTROL_BUTTON_LABEL = '變更方案';

interface FixtureService {
  service_id: number;
  template_id: number;
  template_name: string;
  has_admin_gui: boolean;
}
interface Fixture {
  workspace_id: number;
  workspace_name: string;
  services: Record<string, FixtureService>;
}

/**
 * 四個代表模板與期望值。期望值**寫死在測試裡**（不從 fixture 讀），這樣
 * 「有人把 seed 的 has_admin_gui 改壞」也會被這支測試抓到，而不是跟著一起漂。
 * slug 必須與 e2e-seed-reset-button.sh 的 TEMPLATE_XMLIDS 對齊。
 */
const CASES: { slug: string; expectVisible: boolean; why: string }[] = [
  {
    slug: 'hermes-agent',
    expectVisible: true,
    why: '#449／PR #455 新修族 —— 修好的重設能力必須對租戶可達',
  },
  {
    slug: 'mcp-k8s',
    expectVisible: true,
    why: '#375 三型 MCP 遺漏族 —— 後端 reset 一直是好的、按鈕自上架就漏',
  },
  {
    slug: 'mcp-odoo',
    expectVisible: true,
    why: '原 slug 白名單族 —— 本次收斂不得造成回歸',
  },
  {
    slug: 'n8n',
    expectVisible: false,
    why: 'has_admin_gui=False —— 不得誤現一顆按了必然失敗的按鈕',
  },
];

function loadFixture(): Fixture {
  if (!fs.existsSync(FIXTURE_PATH)) {
    throw new Error(
      `Fixture manifest not found at ${FIXTURE_PATH}. ` +
        'Run .claude/skills/k8s-dev-sandbox/scripts/e2e-seed-reset-button.sh first.'
    );
  }
  return JSON.parse(fs.readFileSync(FIXTURE_PATH, 'utf-8')) as Fixture;
}

async function loginAdmin(page: Page): Promise<void> {
  await page.goto(`${BASE_URL}/web/login`);
  // 單 DB 部署的登入頁**沒有** db 欄位 —— 短 timeout + catch，別讓預設 30s
  // actionability wait 把整個 hook 的預算吃光（實際踩過）。
  if (DB_NAME) {
    await page
      .fill('input[name="db"]', DB_NAME, { timeout: 2000 })
      .catch(() => {});
  }
  await page.fill('input[name="login"]', ADMIN_LOGIN);
  await page.fill('input[name="password"]', ADMIN_PASSWORD);
  await page.click('button[type="submit"]');
  await page.waitForURL(/\/odoo|\/web/);
}

/**
 * 開啟服務詳情頁，回傳該頁 service payload 裡的 template 物件。
 *
 * 導航策略：/woow 是 OWL hash router。`router.init()` 在載入時就會 `parseRoute()`，
 * 所以帶 hash 直接 goto 是可行的（歷史上的「深路由 404」是 hash 少寫了 `/`：
 * `parseRoute` 做 `hash.slice(2)`，必須是 `#/workspace/...` 而非 `#workspace/...`）。
 *
 * ⚠️ 每個 case 之間先 `about:blank` 再 goto，兩個原因疊在一起（都實際踩過）：
 *   1. service-detail → service-detail 的 hashchange 只換 props，OWL 沿用同一個
 *      ServiceDetailPage 實例、`onMounted` 不重跑 → 不發新的 payload 請求。
 *   2. 兩個 case 的目標 URL **只差 hash**，`page.goto` 在這種情況下走的是
 *      hash navigation、**不會重新載入頁面** → 同樣不重新 mount。
 * 先斷到 about:blank 才能保證下一次 goto 是真正的整頁導航，四個 case 互相獨立。
 */
async function openServiceDetail(
  page: Page,
  workspaceId: number,
  serviceId: number,
  expectedName: string
): Promise<Record<string, unknown>> {
  const targetHash = `#/workspace/${workspaceId}/services/${serviceId}`;
  // 只認詳情端點本身（`…/services/<id>` 結尾），不要被同前綴的
  // `/rollback`、`/revisions`、`/reset-admin-password` 誤匹配。
  const detailUrl = `/api/workspaces/${workspaceId}/services/${serviceId}`;

  await page.goto('about:blank');
  const payloadPromise = page.waitForResponse(
    (res) =>
      new URL(res.url()).pathname === detailUrl &&
      res.request().method() === 'POST'
  );

  // domcontentloaded 而非預設的 'load'：/woow 有 bus/websocket 與 long-poll，
  // 'load' / networkidle 在這頁不是可靠的完成信號。
  await page.goto(`${BASE_URL}/woow${targetHash}`, {
    waitUntil: 'domcontentloaded',
  });

  const payload = await (await payloadPromise).json();
  // Odoo type="json" route 回 JSON-RPC envelope：{ jsonrpc, id, result: {...} }
  const body = (payload.result ?? payload) as {
    success: boolean;
    data?: Record<string, any>;
    error?: string;
  };
  expect(
    body.success,
    `service payload 應成功回傳（error=${body.error ?? ''}）`
  ).toBe(true);

  // 頁面真的渲染出這個服務（而非 loading / error state）——沒有這道，
  // 「按鈕不可見」的斷言會被「頁面根本沒載入」假裝成通過。
  await expect(page.locator('.o_woow_error_state')).toHaveCount(0);
  await expect(page.locator('.o_woow_service_header_title h1')).toHaveText(
    expectedName
  );

  return body.data!.template as Record<string, unknown>;
}

// serial：四個 case 共用同一個已登入的 page，靠 hashchange 換服務（比每個 case
// 重登快，也更接近租戶「在 SPA 內逛」的真實路徑）。
// timeout 120s：第一次進 /woow 要編譯 assets bundle，預設 30s 不夠。
test.describe.configure({ mode: 'serial', timeout: 120_000 });

test.describe('服務詳情頁 Reset 按鈕可見性 —— 判準＝template.has_admin_gui', () => {
  let page: Page;
  let fixture: Fixture;

  test.beforeAll(async ({ browser }) => {
    // 冷啟的 Odoo 第一個 request 要載 registry ＋ 編譯 assets bundle，
    // 預設 30s hook 預算不夠。
    test.setTimeout(180_000);
    fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
    fixture = loadFixture();
    page = await browser.newPage();
    await loginAdmin(page);
  });

  test.afterAll(async () => {
    await page?.close();
  });

  for (const { slug, expectVisible, why } of CASES) {
    test(`${slug} → Reset 按鈕${expectVisible ? '可見' : '不可見'}（${why}）`, async () => {
      const svc = fixture.services[slug];
      expect(svc, `fixture 缺少 ${slug} 的服務（seed 是否跑過？）`).toBeTruthy();

      const template = await openServiceDetail(
        page,
        fixture.workspace_id,
        svc.service_id,
        `e2e-${slug}`
      );

      // ── (a) payload 合約：欄位存在、型別為 boolean、與 DB ground truth 一致 ──
      expect(
        template,
        'service payload 的 template 物件必須帶 has_admin_gui（#460 S1 後端側）'
      ).toHaveProperty('has_admin_gui');
      expect(typeof template.has_admin_gui).toBe('boolean');
      expect(template.has_admin_gui).toBe(svc.has_admin_gui);
      // 期望值與模板能力對齊 —— seed 被改壞時這裡先炸
      expect(template.has_admin_gui).toBe(expectVisible);
      expect(template.slug).toBe(slug);

      // ── (b) action bar 真的渲染了（對照組，防「頁面沒載入」造成的假通過）──
      const actions = page.locator('.o_woow_service_header_actions');
      await expect(
        actions.getByRole('button', { name: CONTROL_BUTTON_LABEL })
      ).toBeVisible();

      // ── (c) 受測斷言：Reset 按鈕可見性 ──
      const resetButton = actions.getByRole('button', {
        name: RESET_BUTTON_LABEL,
      });
      if (expectVisible) {
        await expect(resetButton).toBeVisible();
        // running 狀態下 canPerformActions 為真 → 按鈕可按，不只是渲染出來
        await expect(resetButton).toBeEnabled();
      } else {
        await expect(resetButton).toHaveCount(0);
      }

      await page.screenshot({
        path: `${SCREENSHOT_DIR}/${slug}-${expectVisible ? 'visible' : 'absent'}.png`,
        fullPage: false,
      });
    });
  }

  /**
   * 紅燈對照 —— 沒有這道，上面四條「可見」都可能是「按鈕本來就無條件渲染」的假綠。
   *
   * 攔截 hermes-agent 的 service payload，**只**把 `template.has_admin_gui` 這個
   * 鍵拿掉（其餘原封不動），斷言按鈕隨之消失。同時就是 #460 spec 要求的
   * fail-closed 行為（欄位缺失＝視同 False）的真瀏覽器實證。
   */
  test('紅燈對照：payload 抽掉 has_admin_gui → hermes-agent 按鈕消失（fail-closed）', async () => {
    const svc = fixture.services['hermes-agent'];
    const detailPath = `/api/workspaces/${fixture.workspace_id}/services/${svc.service_id}`;

    await page.route(
      (url) => url.pathname === detailPath,
      async (route) => {
        const response = await route.fetch();
        const body = await response.json();
        delete body.result.data.template.has_admin_gui;
        await route.fulfill({ response, json: body });
      }
    );

    try {
      await page.goto('about:blank');
      await page.goto(
        `${BASE_URL}/woow#/workspace/${fixture.workspace_id}/services/${svc.service_id}`,
        { waitUntil: 'domcontentloaded' }
      );

      const actions = page.locator('.o_woow_service_header_actions');
      // 對照組：頁面確實載入了（否則「按鈕不見」毫無意義）
      await expect(page.locator('.o_woow_service_header_title h1')).toHaveText(
        'e2e-hermes-agent'
      );
      await expect(
        actions.getByRole('button', { name: CONTROL_BUTTON_LABEL })
      ).toBeVisible();
      // 受測：同一個服務、同一個頁面，只少了那個欄位 → 按鈕不該出現
      await expect(
        actions.getByRole('button', { name: RESET_BUTTON_LABEL })
      ).toHaveCount(0);

      await page.screenshot({
        path: `${SCREENSHOT_DIR}/control-hermes-agent-field-stripped.png`,
        fullPage: false,
      });
    } finally {
      await page.unroute((url) => url.pathname === detailPath);
    }
  });
});

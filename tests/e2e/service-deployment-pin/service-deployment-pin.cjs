/** Browser regression against an isolated synthetic Odoo database only. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const base = process.env.PIN_BROWSER_BASE_URL || 'http://127.0.0.1:28069';
assert(['127.0.0.1', 'localhost'].includes(new URL(base).hostname), 'Use a localhost sandbox');
const accounts = JSON.parse(fs.readFileSync(process.env.PIN_BROWSER_ACCOUNTS, 'utf8'));
const serviceId = Number(process.env.PIN_BROWSER_SERVICE_ID);
assert(Number.isInteger(serviceId) && serviceId > 0, 'Supply the synthetic service id');
const evidence = process.env.PIN_BROWSER_EVIDENCE || '/tmp/paseo-pin-browser-evidence';
fs.mkdirSync(evidence, { recursive: true });
const image = { image: { repository: 'registry.example/open-design',
  tag: `candidate@sha256:${'a'.repeat(64)}` } };
const checks = [];

async function openForm(page, role) {
  await page.goto(`${base}/web/login?db=paseo_pin_test`);
  await page.locator('input[name="login"]').fill(accounts[`${role}_login`]);
  await page.locator('input[name="password"]').fill(accounts[`${role}_password`]);
  await page.locator('button[type="submit"]').click();
  await page.waitForURL(url => !url.pathname.includes('/login'));
  await page.goto(`${base}/odoo/action-woow_paas_platform.action_cloud_service/${serviceId}`);
  await page.getByRole('tab', { name: 'Helm Config', exact: true }).click({ timeout: 60000 });
  const title = page.locator('.oe_title [name="name"]');
  const input = title.locator('input');
  assert.match(await input.count() ? await input.inputValue() : await title.innerText(),
    /Synthetic deployment pin browser/);
}

async function save(page) {
  const response = page.waitForResponse(r => r.url().includes('/cloud_service/web_save')
    || r.url().includes('/woow_paas_platform.cloud_service/web_save'));
  await page.locator('.o_form_button_save').click();
  const payload = await (await response).json();
  assert(!payload.error, payload.error?.data?.message || 'Save must succeed');
  await page.locator('.o_form_button_save').waitFor({ state: 'hidden' });
}

async function readPin(page) {
  const payload = await page.evaluate(async id => {
    const response = await fetch('/web/dataset/call_kw', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ jsonrpc: '2.0', method: 'call', params: {
        model: 'woow_paas_platform.cloud_service', method: 'read',
        args: [[id], ['deployment_policy', 'deployment_chart_version', 'deployment_image_overrides']],
        kwargs: {},
      } }),
    });
    return response.json();
  }, serviceId);
  assert(!payload.error, 'Administrator can verify saved pin');
  return payload.result[0];
}

(async () => {
  const browser = await chromium.launch({
    headless: process.env.PIN_BROWSER_HEADLESS === '1',
    executablePath: process.env.CHROMIUM_EXECUTABLE || undefined,
    args: ['--no-sandbox'],
  });
  let page;
  try {
    const admin = await browser.newContext({ viewport: { width: 1440, height: 1100 } });
    page = await admin.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await openForm(page, 'admin');
    const policy = page.locator('[name="deployment_policy"] select');
    assert(await policy.isVisible());
    checks.push('admin sees deployment policy');
    if ((await policy.inputValue()).includes('pinned')) {
      await policy.selectOption({ label: 'Follow product template' });
      await page.locator('[name="deployment_chart_version"] input').waitFor({ state: 'hidden' });
      await save(page);
    }
    await policy.selectOption({ label: 'Pinned deployment' });
    await page.locator('[name="deployment_chart_version"] input').fill('0.5.8');
    const editor = page.locator('[name="deployment_image_overrides"] textarea');
    assert(await editor.isEditable());
    checks.push('JSON textarea is editable');

    await editor.fill('{');
    await page.locator('.o_form_button_save').click();
    await page.locator('[name="deployment_image_overrides"].o_field_invalid').waitFor();
    assert.equal((await readPin(page)).deployment_policy, 'template');
    checks.push('malformed JSON cannot save');

    await editor.fill(JSON.stringify(image, null, 2));
    await save(page);
    const pinned = await readPin(page);
    assert.equal(pinned.deployment_policy, 'pinned');
    assert.equal(pinned.deployment_chart_version, '0.5.8');
    assert.deepEqual(pinned.deployment_image_overrides, image);
    checks.push('pinned chart and JSON saved to database');
    await page.reload();
    await page.getByRole('tab', { name: 'Helm Config', exact: true }).click();
    assert.deepEqual(JSON.parse(await editor.inputValue()), image);
    checks.push('saved JSON survives browser reload');
    await page.screenshot({ path: path.join(evidence, '01-admin-pinned.png'), fullPage: true,
      animations: 'disabled' });

    await policy.selectOption({ label: 'Follow product template' });
    await editor.waitFor({ state: 'hidden' });
    await save(page);
    const unpinned = await readPin(page);
    assert.equal(unpinned.deployment_policy, 'template');
    assert(!unpinned.deployment_chart_version);
    assert(!unpinned.deployment_image_overrides
      || Object.keys(unpinned.deployment_image_overrides).length === 0);
    checks.push('template switch clears both overrides and saves');
    await page.reload();
    await page.getByRole('tab', { name: 'Helm Config', exact: true }).click();
    assert.match(await policy.inputValue(), /template/);
    await editor.waitFor({ state: 'hidden' });
    await page.screenshot({ path: path.join(evidence, '02-admin-template.png'), fullPage: true,
      animations: 'disabled' });
    assert.deepEqual(errors, [], 'No browser runtime errors');
    await admin.close();

    const tenant = await browser.newContext({ viewport: { width: 1440, height: 1100 } });
    page = await tenant.newPage();
    page.on('pageerror', error => errors.push(error.message));
    await openForm(page, 'tenant');
    checks.push('tenant can open its own synthetic service');
    for (const name of ['deployment_policy', 'deployment_chart_version', 'deployment_image_overrides']) {
      assert.equal(await page.locator(`[name="${name}"]`).count(), 0, name);
    }
    checks.push('tenant has none of the three admin fields in DOM');
    await page.screenshot({ path: path.join(evidence, '03-tenant-no-pin-fields.png'), fullPage: true,
      animations: 'disabled' });
    assert.deepEqual(errors, [], 'No tenant browser runtime errors');
    await tenant.close();
    const result = { passed: checks.length, checks, headless: process.env.PIN_BROWSER_HEADLESS === '1' };
    fs.writeFileSync(path.join(evidence, 'browser-result.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result));
  } catch (error) {
    if (page && !page.isClosed()) {
      await page.screenshot({ path: path.join(evidence, 'browser-failure.png'), fullPage: true });
    }
    console.error(error.message);
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();

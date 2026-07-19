# Skill Admin Form — Browser E2E

Spec: `docs/plans/2026-Q2/2026-05-28-skill-e2e-and-is-latest-drift-fix-design.md` §④-b
Plan: `docs/plans/2026-Q2/2026-05-28-skill-e2e-and-is-latest-drift-fix.md` Task 4

## 前置

1. **Sandbox 已啟動且 .py 已 rollout restart**：

   ```bash
   kubectl --context docker-desktop rollout restart \
     deploy/feat-skill-list-odoo-dev-sandbox-odoo -n dev-sandbox-feat-skill-list
   kubectl --context docker-desktop rollout status \
     deploy/feat-skill-list-odoo-dev-sandbox-odoo -n dev-sandbox-feat-skill-list
   ```

   > 注意：上面的 deploy 名是字面寫法，僅在 slug 夠短時正確。helm fullname 會 `trunc 43`，
   > 長 branch slug 會被截斷；穩健做法是用 label selector 取名：
   > `kubectl get deploy -n dev-sandbox-<slug> -l app.kubernetes.io/component=odoo -o name`。
   > `e2e-seed.sh` 已採此 label-selector 探查。

2. **取得 sandbox 對外 URL**（用 list/status 腳本看 ingress / nodePort）：

   ```bash
   bash .claude/skills/k8s-dev-sandbox/scripts/status.sh feat-skill-list
   # 把 URL 設成 SANDBOX_BASE_URL
   export SANDBOX_BASE_URL=<URL>
   ```

3. **ORM 預建 draft skill**（已自動化，不需再手動 odoo shell）：

   ```bash
   # 冪等 seed：建 project + draft skill 並 commit
   bash .claude/skills/k8s-dev-sandbox/scripts/e2e-seed.sh feat-skill-list --context docker-desktop
   # 或與 module test 一併跑（測試通過後自動 seed）：
   bash .claude/skills/k8s-dev-sandbox/scripts/test.sh feat-skill-list --e2e-seed
   ```

   - `e2e-seed.sh` 的 `--name` / `--version` 預設（`browser-e2e-skill` / `1.0.0`）必須與 spec 的
     `SKILL_E2E_NAME` 對齊，spec 才找得到列。
   - spec 會把該筆推進到 archived，所以重跑請帶 `--reset`（`test.sh --e2e-seed` 已預設帶）；
     要清掉 fixture 用 `e2e-seed.sh <slug> --purge`。

4. **建立 screenshot 目錄**：

   ```bash
   mkdir -p /tmp/skill-e2e-screenshots
   ```

## 執行

```bash
# 用 playwright-cli skill
# 詳見 .claude/skills/playwright-cli/

cd tests/e2e/skill
playwright test skill-admin-form.spec.ts --headed=false
```

## 截圖落地

- `/tmp/skill-e2e-screenshots/01-draft.png`
- `/tmp/skill-e2e-screenshots/02-published.png`
- `/tmp/skill-e2e-screenshots/03-deprecated.png`
- `/tmp/skill-e2e-screenshots/04-archived.png`

每張用於肉眼複查 statusbar / button visibility / badge / is_latest 渲染是否正確。

## 常見 pitfall

- **改 .py 沒 rollout restart**：serve 進程跑舊 code，看到的 button 行為是舊版的 — 必須先 restart 並等 rollout status ready。
- **fixture skill 不存在**：步驟 3 的 `e2e-seed.sh` 沒跑（或 seed 後 spec 已把它推到 archived 而沒帶 `--reset`）。
- **base URL 不對**：sandbox ingress 與 cluster 內部 service 不同，playwright 必須走對外 URL。
- **selectors 假設**：本 spec 的 `.o_statusbar_buttons` / `.o_statusbar_status .btn[data-value=...]` / `[name="is_latest"] input` 是 Odoo 18 admin theme 的合理猜測；實際落地用 `playwright codegen` 或 DOM inspector 對齊 sandbox 真實渲染為準。

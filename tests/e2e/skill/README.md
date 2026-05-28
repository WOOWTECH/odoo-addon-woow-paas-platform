# Skill Admin Form — Browser E2E

Spec: `docs/superpowers/specs/2026-05-28-skill-e2e-and-is-latest-drift-fix.md` §④-b
Plan: `docs/superpowers/plans/2026-05-28-skill-e2e-and-is-latest-drift-fix.md` Task 4

## 前置

1. **Sandbox 已啟動且 .py 已 rollout restart**：

   ```bash
   kubectl --context docker-desktop rollout restart \
     deploy/feat-skill-list-odoo-dev-sandbox-odoo -n dev-sandbox-feat-skill-list
   kubectl --context docker-desktop rollout status \
     deploy/feat-skill-list-odoo-dev-sandbox-odoo -n dev-sandbox-feat-skill-list
   ```

2. **取得 sandbox 對外 URL**（用 list/status 腳本看 ingress / nodePort）：

   ```bash
   bash .claude/skills/k8s-dev-sandbox/scripts/status.sh feat-skill-list
   # 把 URL 設成 SANDBOX_BASE_URL
   export SANDBOX_BASE_URL=<URL>
   ```

3. **ORM 預建 draft skill**（透過 odoo shell or REST；fixture script 待落地）：

   ```bash
   # 範例：odoo shell
   kubectl --context docker-desktop exec -it -n dev-sandbox-feat-skill-list \
     deploy/feat-skill-list-odoo-dev-sandbox-odoo -- \
     odoo shell -d woow_feat_skill_list -c /etc/odoo/odoo.conf
   # 進 shell 後：
   # project = env['project.project'].create({'name': 'e2e-proj'})
   # env['woow_paas_platform.skill'].create({'name': 'browser-e2e-skill',
   #     'version': '1.0.0', 'project_id': project.id,
   #     'bundle_sha256': 'a' * 64, 'state': 'draft'})
   # env.cr.commit()
   ```

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
- **fixture skill 不存在**：步驟 3 沒跑或 commit。
- **base URL 不對**：sandbox ingress 與 cluster 內部 service 不同，playwright 必須走對外 URL。
- **selectors 假設**：本 spec 的 `.o_statusbar_buttons` / `.o_statusbar_status .btn[data-value=...]` / `[name="is_latest"] input` 是 Odoo 18 admin theme 的合理猜測；實際落地用 `playwright codegen` 或 DOM inspector 對齊 sandbox 真實渲染為準。

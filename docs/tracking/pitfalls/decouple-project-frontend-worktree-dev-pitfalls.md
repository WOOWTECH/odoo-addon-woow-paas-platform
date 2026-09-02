---
name: decouple-project-frontend-worktree-dev-pitfalls
description: Cycle 6 decouple-project frontend cycle 撞到的 3 條 worktree 開發環境陷阱 — odoo --http-port 避常駐 / OWL asset bundle 必 rebuild / docker compose restart 才生效
metadata:
  type: pitfall
---

# Pitfall: Decouple-project frontend cycle 的 3 條 worktree 開發環境陷阱

> 來源：cycle 6（decouple project frontend + controller 收尾，2026-05-28）。Implementation / tester / playwright 階段累積的 dev-env 教訓；不影響 production code，但會把 dev cycle 變成「跑紅 / 看舊版本 / 改錯地方」的 wild goose chase，浪費小時級時間。3 條都是「Odoo 18 / worktree dev 環境」idiom 沒寫進 onboarding，新人或上手不久的 agent 容易撞。

## Pitfall 1 — Odoo addon test 用 `--http-port=8071` 避開常駐 web container 8069 占用

### Symptom

- Worktree 有常駐 `web` container 跑 dev server on `8069`（`./scripts/start-dev.sh` 起的）
- 試圖 run tests 走 `docker compose run --rm web odoo -c odoo.conf -u woow_paas_platform --test-enable --stop-after-init`
- 啟動瞬間炸：`OSError: [Errno 98] Address already in use` (port 8069)
- 或更陰：test 跑得起來但**走的是常駐 container 的 db state**（migrations 不 isolate），test fixture 看到舊 row → assertion 紅燈在「為什麼有這個我沒建的 record」

### Why it happens

- `docker compose run --rm web` 預設**繼承** compose service `web` 的 `ports` 與 `volumes`，所以 test container 試圖 bind 8069
- 常駐 web container 仍在 listen 8069 → test container bind 失敗
- 解法是 test container 用不同 port（`--http-port=8071`）+ 不掛 volume（test container 跑 init scenario 不該寫 dev db）

### How to avoid

跑 addon test 時的 idiom（cycle 6 從 tester + caqa 階段反覆撞才整理出來）：

```bash
# Cycle 6 worktree test 範例（避 8069 衝突 + 不沾 dev db）
docker compose run --rm \
  --no-deps \
  -p 8071:8071 \
  web odoo \
  -c /etc/odoo/odoo.conf \
  --http-port=8071 \
  -d test_decouple_project \
  -i woow_paas_platform \
  --test-enable \
  --test-tags '/woow_paas_platform:TestProjectDecoupleFrontendEndpoint' \
  --stop-after-init
```

關鍵點：
- `--http-port=8071` — 不撞常駐 8069
- `-p 8071:8071` — host port mapping 不撞
- `-d test_decouple_project` — 獨立 test db，不沾 dev db
- `-i woow_paas_platform` — install（test db 是空的）
- `--test-tags '/woow_paas_platform:TestClass'` — narrow scope（但見 [[tag-scoped-test-misses-full-u-failures]]，full `-u` 才抓 cross-class stale schema）

### Related

- [[tag-scoped-test-misses-full-u-failures]] — `--test-tags` narrow 跑綠 ≠ full `-u <module>` 全綠
- `CLAUDE.md` §Worktree Development — 各 worktree port hash 規則
- `scripts/test-addon.sh` — 工程化 wrapper

---

## Pitfall 2 — OWL JS / XML 改動必須 `odoo -u <module>` rebuild asset bundle，否則 playwright 看舊版

### Symptom

- 改 `src/static/src/paas/components/modal/CreateProjectModal.js / .xml` / `support_service.js`（cycle 6 frontend 改動）
- restart `web` container（`docker compose restart web`）—— **不夠**
- Browser hard reload（cmd+shift+R）—— 仍看到舊版
- Playwright 走 `agent-browser navigate` 或 `playwright_navigate` —— 仍看到舊版 UI（舊 modal 行為 / 缺新 dropdown option）
- 浪費 30+ 分鐘 debug「為什麼我的 fix 沒生效」

### Why it happens

Odoo 的 OWL asset bundle 是**啟動時 build**，存進 `ir.attachment` 作為合併壓縮的 single bundle file（如 `woow_paas_platform.assets_paas`）。Bundle 內容是改動 JS / XML / SCSS source file 的 union 字串，**只在 `odoo -u <module>` / install 時 rebuild**。

- Browser 收到的是 bundled file（`/web/assets/...`），不是 source file
- 只 `restart web` → web container 繼續 serve 舊 bundle（bundle 已存 DB attachment 不會自動 invalidate）
- 必須走 `odoo -u woow_paas_platform --stop-after-init`（或 dev environment 自動 `--dev=xml,assets,...` 機制）才 trigger asset rebuild

### How to avoid

OWL frontend touch 後的 idiom：

```bash
# Cycle 6 OWL 改動後（CreateProjectModal.js/.xml + support_service.js）必跑
docker compose run --rm web odoo \
  -c /etc/odoo/odoo.conf \
  -d <dev_db_name> \
  -u woow_paas_platform \
  --stop-after-init

# 然後 restart 常駐 web container（重 bind 新 bundle）
docker compose restart web
```

**Dev mode 替代方案**（`--dev=all` 或 `--dev=xml,assets,reload,qweb`）自動 watch source file change rebuild bundle — 但本 repo 預設 dev mode 沒 enable assets reload（除非顯式 set `ODOO_DEV_MODE` env），所以**手動跑 `-u` 是 cycle 6 實測有效的最穩 idiom**。

**Browser cache 是次因**：bundle 重 build 後，bundle URL 內的 hash 會變（Odoo asset versioning），browser 自動拿新 bundle。不需要手動 hard reload，但若手動 reload，請 ctrl+shift+R 強制。

### Related

- Pitfall 3（同 cycle，asset rebuild 與 web restart 兩件事不一樣）
- Odoo 18 dev mode 文檔（`--dev` flag）

---

## Pitfall 3 — `-u <module> --stop-after-init` 不會 restart 主 web service，修 controller python 必 `docker compose restart web` 否則 playwright 看舊 code

### Symptom

- 改 `src/controllers/ai_assistant.py`（cycle 6 D1+D2+D3 + T3/T4 fix）
- 跑 `odoo -u woow_paas_platform --stop-after-init`（pitfall 2 idiom，rebuild asset bundle）
- Playwright trigger frontend → 仍打到舊 controller code（attack reproducer 仍 return 舊 data；新 endpoint 路徑 404 / message_subscribe 沒 fire / `_list_projects` 仍 leak）

### Why it happens

- `-u <module> --stop-after-init` 起一個**新 odoo process** 去 update module schema / load asset / run `__init__` hooks，**然後 exit**
- 常駐 `web` container 內的 odoo process 是**獨立的另一個 process**，它仍持有 cycle 6 改動**之前**的 Python module（loaded into memory）
- Python 對 `@route` 裝飾、`models.Model` 註冊都是 import-time 行為 → controller 改動必須 odoo process 整個 restart 才生效

### How to avoid

Controller / model python 改動後的完整 idiom（cycle 6 經驗）：

```bash
# 1. 改 controller / model python（如 ai_assistant.py D1+D2+D3 + T3+T4 fix）

# 2. Rebuild asset bundle（也順手 reload schema/data）
docker compose run --rm web odoo \
  -c /etc/odoo/odoo.conf \
  -d <dev_db_name> \
  -u woow_paas_platform \
  --stop-after-init

# 3. Restart 常駐 web container（讓常駐 odoo process 重 import）
docker compose restart web

# 4. 等 web container 起來（~5-10s），browser / playwright 才會打到新 code
```

**順序很關鍵**：先跑 step 2（更新 schema + asset），再跑 step 3（reload Python module）。反過來會看到「asset 是新版但 controller 是舊版」的混合錯誤狀態。

**只動 SCSS / XML 純樣式**：理論上 step 2 後就夠（asset rebuild + bundle hash 變），不一定要 step 3 — 但保險起見統一跑兩步。

### Related

- Pitfall 2（asset rebuild）
- `docs/guides/testing/TESTING.md` §Run in K8s Dev Sandbox — pod 內只升級不測試／`--stop-after-init` 用法（原 CLAUDE.md 的 odoo-bin 段已於 2026-09 移除：本 repo 沒有 odoo-bin）

---

## Why these matter（不只是「dev 環境問題」）

3 條合起來會把 cycle 6 風格的 PR（同時改 controller + frontend OWL + 新 test）的 dev iteration 變成：

| 不知道 idiom | 知道 idiom |
|------------|-----------|
| 跑 test 撞 8069 → 改 docker-compose → 弄壞 dev container → 半小時 debug 環境 | `--http-port=8071` 一行解 |
| 改 frontend 看不到變化 → 試 hard reload → 試 incognito → 試重啟 → 試清 cache，1 小時 debug 框架 | `odoo -u woow_paas_platform` 一次解 |
| 改 controller 看不到變化 → 以為 fix 沒 commit → 重新比對 diff → 改更多東西 → 累積到「不知道哪行有改」 | step 2 + step 3 兩步固定流程 |

每條都不是難題，但**任一條被踩**就讓「改 code → 驗證 → 修正」cycle 從幾分鐘變幾十分鐘到小時。整批寫進 onboarding / `CLAUDE.md` 是低成本高回報的 dev productivity 投資。

## Related

- [[orm-test-green-endpoint-leak-blindness]] — cycle 6 另一條 pitfall，這是 dev-env 層；那是 test 設計層
- `CLAUDE.md` §Docker Development（建議補上「frontend / controller 改動後 rebuild 流程」段落）
- `scripts/start-dev.sh` / `scripts/test-addon.sh`（建議補上 worktree port 衝突的 fallback）

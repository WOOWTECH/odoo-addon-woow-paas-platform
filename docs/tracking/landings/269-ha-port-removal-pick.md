# project 269 ha port removal pick

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_269_ha_port_removal_pick`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#269（Smart Home 移除 HA Port 欄位壓死 8123）code 完成：PR #314 待 merge、E2E 三目標 PASS；含 arm64 原生 e2e 環境 recipe 與 #270 接續"

**2026-07-07 #269 完成、merged → develop**（PR #314，merge commit e2931d6；worktree/本地+遠端 branch 已清；**issue ✅ 已關 2026-07-08**——prod .48 小補驗包 artifact 觀測收束：payload 字面 object 無 ha_port；DB 舊編譯 bundle c408d9f 含舊碼但已被新 version 394d8aa 取代、新 session 拿不到）。4 檔 −33+2 行：CreateSmartHomeModal.xml（刪 port form group）、CreateSmartHomeModal.js（刪 haPort state/onPortInput/驗證/payload）、workspace_service.js（刪 ha_port 參數+`|| 8123` fallback）、`__manifest__.py` bump **18.0.1.0.48**。後端零改動（model default=8123 由既有測試 test_smart_home.py:19-27 釘住，無需補測）。Eugene 拍板兩決策皆落實：payload 完全不帶 ha_port；service 層 fallback 一併移除。worktree=`.claude/worktrees/feat-269-remove-ha-port`（merge 後可清）。

**E2E 三目標 PASS**（ultracode workflow：implement→3 路 review 0 findings→headed E2E→PR）：headed modal 無 port 欄位（DOM 斷言+截圖 worktree/.e2e-269/）、OWL console 零 error（僅 favicon/avatar 環境噪音）、odoo shell create 不帶 ha_port→8123。smart home 模組 9 test class 57 tests 0 failed。

**arm64 原生 e2e 環境 recipe（新發現，比叢集法輕）**：deploy-local skill 目標 compose 環境不存在＝不可行；改用 `odoo:18.0`（arm64 原生，免 Rosetta 牆）+ worktree src + 8 個 CI depends（`/Users/eugene/Documents/woow/woow-paas-repo/paas-odoo-ci/addons`）docker build + postgres:15 fresh `-i` 安裝 → localhost:18069 headed playwright + odoo shell,全程 ~4 分鐘。適合純前端/不依賴 amd64 行為的驗收；amd64 敏感行為仍走 [[reference_odoo_test_amd64_woowk3s_cp_overlay]]。image `e269-test:local`（2.25GB）保留可重驗。

**已知缺口**：未真按 Create 送出（本機無 CF 憑證，provision 會炸噪音）→「POST body 不帶 ha_port」由 code diff 靜態確認，線上觀測留 stg。**→ 2026-07-08 已收束**（stg 停在 .47 不含 #269，改以 prod .48 部署 artifact 觀測 + 對抗覆核；覆核加值排除 DB 舊 bundle 殘留）。

**接續**：#270 於 2026-07-07 晚間**⏸ pending 再討論**（Eugene 判定）——技術定案（無新 port＋無平台 token＋hostname 直通）⇒ remote-access 客戶事實上已含 MCP 能力、平台無 enforcement 點,與會議「拆成兩個 subscription plan」收費意圖衝突。重啟前不動工;待討論題與 gating 選項已留言 issue #270 + issue-for-prod note。技術查證事實（HA ≥2025.11、/api/mcp、configure_tunnel 單-hostname hazard→SA 式 routes-sync 修法）仍有效。HA 側 ha-components/woow_paas_smart_home#9 維持 blocked。**#267 亦於同晚 ⏸ pending（Eugene 判定）**：可能不是 Hermes MCP cloud service（platform 側）的工作——Gateway(:8642)/Dashboard(:9119) 屬分離的 Hermes agent deployment，ownership（platform CS 曝露 vs agent deployment 側自帶）＝原本未拍板的部署拓撲決策本身；待討論題（ownership 歸屬／若歸 platform 的選型照原案／O-5 連線 UI 歸屬）與仍有效查證已留言 issue #267 + issue-for-prod note。**W2 可動工項清空**；接下來＝prod rollout 執行 + 驗證債 + W3 條件式決策（等 PM）。

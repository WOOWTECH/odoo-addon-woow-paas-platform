# MCP95 八個模板 pin 與隔離 Odoo 驗證

本來源分支以平台 `c42850a33d48e0c7c4ce7169bca45034e8c09844` 為基底，只更新八個 MCP 模板的 `image.tag`。映像來自正常合併的 woow-mcp-server PR95 `3c93d0e727fdc948f1ea17cb5fa7d1ad1887a883`；每個 tag、manifest digest、config digest 均有 Aristotle 的實際發布證據，並已獨立重算本機 manifest/config bytes 的 SHA256。完整前後 pin、啟動來源 hash 與限制見 [發布及差異證據](2026-10-30-mcp95-template-pins.json)。

來源 PR 不改模組版本；正常 `release/*`、正式 artifact 真人核可、CD 與租戶服務 rollout 由後續 release owner 處理。27 筆模板中，19 筆完整保留；八筆除 image tag 外的所有欄位也完整保留。legacy `odoo-manage`、隱藏狀態、chart、PVC、credential carrier、四個 Odoo 開關與其他治理設定均不變；沒有新增 EMQX 模板。

## 啟動契約與證據範圍

逐一覆核 PR95 的 app `main.py`、Dockerfile、pyproject、uv.lock 與共用 LaunchPolicy。Odoo 仍為 `woow-odoo-mcp` 的原生 streamable HTTP；n8n 仍為 `n8n-mcp-http`。OpenDesign 的歷史 seed 雖仍寫 supergateway，現有來源明確在 core lifespan 前將該特定舊格式遷移為 `opendesign-mcp-server`，而固定 LaunchPolicy 不執行設定裡的 command/args。Hermes 保留 chart 預設啟動設定。這兩項既有相容行為已列入證據，沒有假裝 seed 與 effective argv 全部逐字相同。

發布證據涵蓋 authenticated manifest/config 與16個 core/static layer 的 fresh GET/hash；每映像43個 core runtime 檔案吻合 merge source。不是所有 layer 的全新下載，也不是本 lane 的實際八產品 MCP 執行證明。Aristotle 另外擁有三產品 HTTP/PVC fixture；本 lane 不重複建立。

## 本機已準備的守線

既有家族 seed tests 對九個現存家族成員逐筆比對完整 image repository/tag：八個新發布 pin 與一個保留的 legacy pin。反向家族覆蓋斷言要求完整集合相等，八個可更新模板的 `ir.model.data.noupdate` 必須為 false。既有 launch、admin password、storage、pricing、plan linkage 守線繼續適用；沒有新增測試 class 或調整 baseline。

本機 XML/JSON 語意比較只能證明來源差異範圍，不能取代 Odoo ORM 或模組升級。下列沙盒步驟尚未執行，必須先取得 Main 的新資源 lease。

## 找到的 sandbox 工具與隔離要求

工具位於工作區 `work/smart-home-connect-20261011/sandbox-tools/plugins/woow-dev/skills/k8s-dev-sandbox`，checkout `4948b7519d1a146256dac17c50b8743f8e830e1c`。既有入口為 `scripts/create.sh`、`test.sh`、`upgrade.sh`、`destroy.sh`，chart 為 `charts/odoo-dev-sandbox`。所有實際呼叫必須明給 `--context woow-k3s` 與對應 `--repo`。

不能直接對本 repo 執行預設 create：`.sandbox/post-create.sh` 會探索 operator 及寫入 AI/ECPay 測試設定；ci-local overlay 也不是隔離網路預設。沙盒 runner 應沿用該工具的 chart/UID 解析及測試命令，使用新的 staging source bundle 與無 hook 的執行描述，先 render 並驗證以下界線，再由 Main 綁定 lease：

- 唯一 `dev-sandbox-mcp95-*` namespace；新合成 DB/密碼，禁止還原正式或 staging 資料。
- default-deny，僅同 namespace DB 與 DNS；不接 operator、AI gateway、SMTP、支付或租戶服務；無 ingress、pgadmin、hostPath、特權或跨 namespace RBAC。
- Odoo 一次性 install/upgrade/test process，沒有常駐 cron；來源 tar 先核 hash 再寫入 owned volume，addons precedence 必須讀回確認沒有載到 image 內的舊副本。
- 可用已發布 official280 digest `941b92091cbf851fdca22cd5d2e67f06235ccfdaad5b2af044682823fa5011a3` 作依賴 runtime，但必須先驗 addons 路徑與版本。這只是測試 runtime，不能當本候選的發布映像。
- PostgreSQL 與輔助 image 必須先解出並核對 immutable digest；未完成前不能啟動。使用 `longhorn-delete` 的實際2 replicas/Delete 契約，不能假設一般 `longhorn` 的 reclaim policy。
- 先申請總量上限3 CPU、6Gi RAM、PG5Gi及source/filestore3Gi（共8Gi PVC）、local ephemeral/image預算8Gi。Main 指定 node/UID 與實際容量後才生效。測試75分鐘 stop，加15分鐘 cleanup；沒有自動延長。

## 實際 Odoo 驗證順序

1. 凍結候選 commit/source bundle、工具 hash、render 與 image digest；記錄新 namespace/PVC/PV/Longhorn volume/node UID。啟動前再核 lease、容量與無舊 intent，避免重播。
2. **升級路徑**：在全新 DB 安裝基底 c42850a 的 addon，讀回27筆模板與 XMLID/noupdate/plan topology。換成候選 addon source，明確執行 `-u woow_paas_platform --stop-after-init --max-cron-threads=0`。逐欄比較：僅八個 image tags改變；其餘19筆與八筆非 image 資料不變，無 EMQX。
3. **新安裝路徑**：同一隔離 PG 的另一個新 DB 以候選 source `-i woow_paas_platform --stop-after-init --without-demo=all --no-http --max-cron-threads=0` 先安裝且不跑測試；確認已安裝後，以獨立的 `-u` test process 執行 seed 類別及相關 MCP credentials/launch/plan tests。前一個 Odoo process 已退出後才開始，避免並行修改。
4. Odoo 測試使用 `/woow_paas_platform:ClassName` 的實際已存在類別選擇器；測試 process 帶 `--test-enable --stop-after-init --http-port=18069 --gevent-port=18072 --log-level=test`。先核 module installed，再核每個選中 class/method 的實際執行與非零測試數；不把 exit0或泛用 healthz 當通過。不以純 Python mock 取代 ORM。
5. 保存原始 log hash、實際 counts/failures/skips、imageID/source map、全27筆前後非密資料與 schema/plan 比較。若來源還要走 normal full-suite CI，由 release owner 綁定 exact head；此 lane 不 dispatch CI 或 artifact、不簽核。
6. 完成或失敗都以 UID precondition 清除 owned namespace；確認 PVC/PV/Longhorn volume及replica物件消失、owned port-forward退出與private credentials移除。留下 cleanup receipt，不改 finalizer、不刪他人資源、不宣稱物理 byte 擦除。

## 回復與採用

來源回復是把八個 tag 改回 JSON 的 `previous_tag`，走正常平台來源/release流程；無 schema migration。模板更新不會自動重啟既存租戶服務，PVC 的 seed-if-absent 也不會覆寫既有 config。後續如要 rollout，必須另綁當時服務狀態與可回復點；本份證據不宣稱既有服務或正式環境已升級。

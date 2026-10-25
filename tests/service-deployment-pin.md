# 管理員指定單一服務部署版本

## 介面

`woow_paas_platform.cloud_service` 新增三個 `base.group_system` 欄位：

| 欄位 | 預設 | 用途 |
| --- | --- | --- |
| `deployment_policy` | `template` | `template` 跟隨商品；`pinned` 使用管理員指定版本 |
| `deployment_chart_version` | 空 | 精確 SemVer，允許 prerelease／build metadata |
| `deployment_image_overrides` | `{}` | 指定既有商品 image 路徑及不可變映像參照 |

管理員一次 ORM write 設定，例如（digest 請換成已驗證的實際產物）：

```python
service.write({
    'deployment_policy': 'pinned',
    'deployment_chart_version': '0.5.8',
    'deployment_image_overrides': {
        'image': {
            'repository': 'registry.example/open-design',
            'tag': 'candidate@sha256:' + 'a' * 64,
        },
    },
})
```

映像 entry 只接受 `repository`、`tag`、選用的 `registry`；`tag` 必須含
`@sha256:<64 lowercase hex>`。路徑須是商品現有 image dict，且不能是租戶可設定鍵。
不接受任意 Helm values 或更換 chart repository／name。未指定的 image 繼續跟隨商品。

目前正式 operator 尚未部署 metadata 版本解析修正 `069c3758`，舊版以 `rsplit`
拆 chart 名稱會誤記 prerelease 版本。因此在該修正部署前，正式 pin 只使用 stable
chart 版本（例如 `0.5.8`）。Resolver 的 SemVer 能力與 prerelease regression tests 保留；
本 feature 不包含 operator 發版。

切回預設時同一次 write 清除兩個 override：

```python
service.write({
    'deployment_policy': 'template',
    'deployment_chart_version': False,
    'deployment_image_overrides': {},
})
```

既有管理員表單切換至 `template` 時，onchange 會自動清除兩個 override，
避免欄位隱藏後無法儲存。RPC／ORM 仍須提供上述完整一致的更新。
JSON 欄位使用本模組的 textarea widget，可編輯 JSON object；空白視為 `{}`，
無效 JSON／非 object 會阻擋表單儲存，entry 欄位仍由後端驗證。

首次建立服務使用可信 context defaults 中和 `default_*` 與 `ir.default` 的 pin 注入，
不把三個受限欄位塞入 regular user 的顯式 create vals，也不提升整個 create 的權限。
管理員明確傳入的 pin 仍會驗證並採用。

## 生效範圍與整合

- 私有 `_resolve_deployment_target()` 統一回傳 `policy/chart/repo_url/version/image_values`。
- 管理員 Redeploy、排隊後的 Redeploy、release 不存在後的首次安裝均採同一目標。
- image 刷新仍使用既有 `_refresh_image_refs`；非 image 的服務設定、資源與 `enabled: false` 保留。
- 租戶儲存設定／重設密碼維持既有行為，使用已部署版本與持久化 image；租戶 values 無法選擇 pin。
- `helm_chart_version` 仍為 operator 觀測帳本；不拿它作為管理員期望版本。
- 商品移除被 pin 的 image path 時停止部署並回報錯誤，避免靜默忽略 pin。
- 此 feature 不改商品 seed、不綁特定 service ID；未 pin 的服務保留原 template-follow 語意。

發布需正式 addon image 與 Odoo module upgrade 建立三欄位。一般 feature PR 不自行 bump
manifest；release 由 parent 整合最新主線、人工 merge，再走既有 tag／human `/approve`
流程。確認產物存在後，只對指定服務設定 pin；其他服務不需改資料。

## 驗證

離線 resolver 測試：

```sh
python -m pytest tests/test_deployment_target.py -q
```

Odoo 測試使用獨立合成資料庫與隔離 sandbox，不使用 stg／prod DB。新增測試為
`TestServiceDeploymentPin`（15 項，含管理員表單解除 pin、regular-user create 與 view 過濾）
與 `TestServiceDeploymentPinRpc`（1 項），
operator HTTP 邊界使用 mock；驗證真實 ORM、constraints、field groups、交易回捲與 RPC。
這些測試不等於真實 Helm／Kubernetes Redeploy 驗收。

建議 focused tags（供既有 sandbox 的 `odoo --test-enable --stop-after-init` 使用）：

```text
/woow_paas_platform:TestServiceDeploymentPin,/woow_paas_platform:TestServiceDeploymentPinRpc,/woow_paas_platform:TestImageRefHelpers,/woow_paas_platform:TestRedeployRefreshesImages,/woow_paas_platform:TestRedeployVerification,/woow_paas_platform:TestExecuteDeployRecordsActualChartVersion,/woow_paas_platform:TestRedeployRecordsActualChartVersion,/woow_paas_platform:TestExecuteDeployBackstop,/woow_paas_platform:TestRedeployGate,/woow_paas_platform:TestReconfigureRejectsTenantChartVersion1525,/woow_paas_platform:TestTenantChartVersionOverRpc1525,/woow_paas_platform:TestRedeployKeepsSuspendedServiceSuspended
```

測試 class 已登記 `src/tests/__init__.py` 與 collection baseline。PR 靜態 CI 綠燈不代表
上述 Odoo 測試已執行；以 sandbox 實際 log 與 tested SHA 為準。

### 2026-10-25 驗證紀錄

- 離線 resolver：39 passed。
- 真實 Odoo 18／PostgreSQL focused：63 tests，0 failed、0 errors，process exit 0。
  新增 16 項（含真實 `Form` onchange/save、regular-user create 與 HTTP RPC），既有合約 47 項。
- regular user 的 `_internal_provision(...).create()` 使用原 uid，`env.su=False`、
  `env.is_system()=False`；分別驗證 context 與確實落庫的 `ir.default` pin 注入無效。
  修正前同一正向案例也通過，未重現所擔心的 AccessError；此修改明確避免新增受限 raw vals。
- JSON parser：`node --test tests/frontend/service-deployment-pin.test.mjs`，8 passed。
- 真實 Chromium browser：8 項檢查通過，管理員 pin／JSON 儲存與 reload、無效 JSON 阻擋、
  切回 template 清空並儲存，租戶可開啟自己的服務且三欄位均不出現在 DOM。
  同時修正既有 `redeploy_check_message` 容器缺少 system group、造成租戶表單 OwlError 的問題。
- 環境：`dev-sandbox-paseo-pin-20261025/paseo-pin-tests`，official `.259` base image
  載入此分支完整 `.260` addon source；全新合成 DB `paseo_pin_test`，Pod 內 localhost PG，
  deny-all NetworkPolicy、無 ServiceAccount token；未使用 stg／prod DB。
- 執行參數：`-c /dev/null -d paseo_pin_test -r odoo --db_host=127.0.0.1 --db_port=5432
  --addons-path=/tmp/paseo-addons,/opt/woow/addons,/usr/lib/python3/dist-packages/odoo/addons
  --data-dir=/tmp/paseo-odoo-data --limit-memory-hard=0 --limit-memory-soft=0 --max-cron-threads=0
  --http-interface=127.0.0.1 --http-port=18069 --gevent-port=18072
  -u woow_paas_platform --test-enable --stop-after-init`，加上上述 focused tags。
- 本機紀錄：`/tmp/paseo-pin-browser-evidence/odoo-focused-final.log`、`browser-result.json`，
  截圖 `01-admin-pinned.png`、`02-admin-template.png`、`03-tenant-no-pin-fields.png`。
  瀏覽器在無 display／Xvfb 的環境使用既有 Chromium **headless**；未宣稱 headed 驗收。
  此結果不包含完整 Odoo suite 或實際 Helm 部署；正式平台 image 候選 build／部署由 parent 處理。

瀏覽器回歸腳本僅接受 localhost sandbox；合成帳號 JSON 含 `admin_login`、`admin_password`、
`tenant_login`、`tenant_password`，須存放私有檔案，不提交或輸出內容。先在合成 DB
`paseo_pin_test` 建立名為 `Synthetic deployment pin browser` 的服務並由合成租戶持有，
以管理員／租戶分開登入。重跑命令（路徑與 service id 換成自己的 sandbox）：

```sh
PLAYWRIGHT_MODULE=/path/to/node_modules/playwright \
CHROMIUM_EXECUTABLE=/path/to/chrome \
PIN_BROWSER_ACCOUNTS=/tmp/private-synthetic-accounts.json \
PIN_BROWSER_SERVICE_ID=111 PIN_BROWSER_HEADLESS=1 \
node tests/e2e/service-deployment-pin/service-deployment-pin.cjs
```

測試只編輯合成服務的部署政策，不點 Redeploy。Odoo 以 `--max-cron-threads=0`、
`--http-interface=127.0.0.1 --http-port=18069` 啟動；port-forward 僅綁 `127.0.0.1:28069`。
執行完畢停止測試 server 與 port-forward，保留 sandbox DB 由建立者管理。

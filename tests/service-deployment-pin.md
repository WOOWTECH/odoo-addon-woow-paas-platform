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
    'deployment_chart_version': '0.5.8-paseo.2',
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

切回預設時同一次 write 清除兩個 override：

```python
service.write({
    'deployment_policy': 'template',
    'deployment_chart_version': False,
    'deployment_image_overrides': {},
})
```

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
`TestServiceDeploymentPin`（12 項）與 `TestServiceDeploymentPinRpc`（1 項），
operator HTTP 邊界使用 mock；驗證真實 ORM、constraints、field groups、交易回捲與 RPC。
這些測試不等於真實 Helm／Kubernetes Redeploy 驗收。

建議 focused tags（供既有 sandbox 的 `odoo --test-enable --stop-after-init` 使用）：

```text
/woow_paas_platform:TestServiceDeploymentPin,/woow_paas_platform:TestServiceDeploymentPinRpc,/woow_paas_platform:TestImageRefHelpers,/woow_paas_platform:TestRedeployRefreshesImages,/woow_paas_platform:TestRedeployVerification,/woow_paas_platform:TestExecuteDeployRecordsActualChartVersion,/woow_paas_platform:TestRedeployRecordsActualChartVersion,/woow_paas_platform:TestExecuteDeployBackstop,/woow_paas_platform:TestRedeployGate,/woow_paas_platform:TestReconfigureRejectsTenantChartVersion1525,/woow_paas_platform:TestTenantChartVersionOverRpc1525,/woow_paas_platform:TestRedeployKeepsSuspendedServiceSuspended
```

測試 class 已登記 `src/tests/__init__.py` 與 collection baseline。PR 靜態 CI 綠燈不代表
上述 Odoo 測試已執行；以 sandbox 實際 log 與 tested SHA 為準。

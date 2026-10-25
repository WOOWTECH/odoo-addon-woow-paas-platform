# Pi Agent Omnigent 新開通預設 pin（WIP，待正式 digest）

## 狀態與修改範圍

本 PR 基於 `origin/develop` `f6f40011`。只準備 Pi Agent 商品的 chart `0.1.10`、既有 seed 測試及本文件；manifest、其他商品、方案、資源與價格都不改。

**尚不可合併／部署**：image 仍是原 seed 的有效 pin `20261018.0104@sha256:0614e0260679b83876cb54b19cda118dea8fb8e236c822f2068afbd3f9707d09`。這不是本次新映像。不能把目前的 chart/image 組合當成已驗證的最終組合。

| 發布層 | 目標／前置條件 |
| --- | --- |
| Plugin | 已驗證 `0.3.11` |
| Root image | `0.3.1` multiarch 正式產物，由 parent 核對 |
| PaaS image | [paas-odoo-ci #407](https://git-prod.woowtech.io/woow-paas/paas-odoo-ci/pulls/407)，正式 tag 預計 `20261025.1200`；等待 parent 提供經 registry 讀回驗證的 digest |
| PaaS chart | [woow-paas-charts #278](https://git-prod.woowtech.io/woow-paas/woow-paas-charts/pulls/278)，需確認 OCI `0.1.10` 已發布 |

保留既有 `has_admin_gui=True`、`admin_password_carrier=helm_secret` 與 `authProxy.basicAuthUsername=admin`：Pi Web 登入密碼仍由平台供給。Omnigent 的 server URL／帳號／密碼由首次設定對話要求使用者填入，不寫入商品 seed。`piWeb.omnigentSetup` 使用 chart 預設 true，無須在模板重複宣告。

## 正確的 default 與既有服務邊界

目前新開通仍讀 `cloud_app_pi_agent.helm_chart_version` 與 `helm_default_values`；沒有另一份新服務 image 預設需要更新。

[平台 #1996](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/pulls/1996) 與 [release #1998](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/pulls/1998) 的單一服務 pin 尚待覆核。該設計新增管理員專用的 `deployment_policy`、`deployment_chart_version`、`deployment_image_overrides`，**沒有取代商品 default**。本 PR 不以該 feature 分支為 base，也不修改或回填這三欄位。

更新商品本身不觸發既有服務 rollout、不改既有 `helm_values`。需要區分後續的操作：

- `template-follow` 服務若將來由管理員執行 Redeploy，仍依原設計更新到當時的商品版本。
- 已完成 #1996 部署且明確設為 `pinned` 的服務，完整 pin 由 resolver 保留；未指定的 image 路徑仍跟商品。
- 本 PR 不執行 Redeploy，也不將所有既有服務改成 pinned。

[舊 Pi image PR #1866](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/pulls/1866) 也在修改同一個 seed image，目標是較舊的 `20261019.0810`。人工整併時須確認本次正式映像已含其 context-window 修正，避免之後合入舊 PR 導致 default 倒退；本 PR 不關閉或改寫他人的 PR。

## 收到 verified digest 後的精確補入點

1. 在 `src/data/cloud_app_templates.xml` 的 `record id="cloud_app_pi_agent"`，只將 `helm_default_values` JSON 的 `image.tag` 換成 **正式 tag 加 parent 提供的 digest**。保留 repository 與其餘所有 JSON key/value，不能借用 root digest、candidate digest、全零值或 `pending-approval` tag。
2. 確認 chart `0.1.10` 已發布，刪除該 record 旁的 WIP 註解，改記實際來源／驗證證據。`src/tests/test_pi_agent_seed.py::test_chart_oci_pin` 已對齊 `0.1.10`。
3. 同步最新 `origin/develop`，重跑下方範圍檢查與 Odoo seed 測試。一般 PR 不 bump `src/__manifest__.py`，不新增 migration。
4. 更新本文件與 PR 的實際驗證結果，再交真人覆核。**不自動 merge、不打平台 release tag、不簽署 image approval、不 dispatch 全平台 production build。**

## 最小驗證

在 repo 根目錄執行以下只讀 source 檢查。它驗 XML/JSON、修改範圍與 pin 格式，**不代表 Odoo 實機測試或 registry 發布已成功**。

```bash
python3 - <<'PY'
import ast, json, re, subprocess
from pathlib import Path
from xml.etree import ElementTree as ET

path = 'src/data/cloud_app_templates.xml'
base = ET.fromstring(subprocess.check_output(['git', 'show', 'origin/develop:' + path]))
head = ET.parse(path).getroot()
def records(root):
    return {r.attrib['id']: ET.tostring(r) for r in root.findall('.//record')}
before, after = records(base), records(head)
assert before.keys() == after.keys()
assert {k for k in before if before[k] != after[k]} == {'cloud_app_pi_agent'}
def pi(root):
    return root.find(".//record[@id='cloud_app_pi_agent']")
def fields(root):
    return {f.attrib['name']: ET.tostring(f) for f in pi(root).findall('field')}
a, b = fields(base), fields(head)
assert a.keys() == b.keys()
assert {k for k in a if a[k] != b[k]} <= {'helm_chart_version', 'helm_default_values'}
assert pi(head).find("field[@name='helm_chart_version']").text == '0.1.10'
def values(root):
    return json.loads(pi(root).find("field[@name='helm_default_values']").text)
old, new = values(base), values(head)
old_tag, new_tag = old['image'].pop('tag'), new['image'].pop('tag')
assert old == new, 'Only image.tag may change in Helm defaults'
assert re.fullmatch(r'\d{8}\.\d{4}@sha256:[0-9a-f]{64}', new_tag)
assert head.find('data').attrib['noupdate'] == '0'
manifest = 'src/__manifest__.py'
assert Path(manifest).read_bytes() == subprocess.check_output(['git', 'show', 'origin/develop:' + manifest])
ast.parse(Path('src/tests/test_pi_agent_seed.py').read_text())
print('PASS: Pi-only XML scope, values/resources, immutable-pin syntax, noupdate, unchanged manifest, test syntax')
print('Final release image inserted:', new_tag.startswith('20261025.1200@sha256:'))
PY
python3 scripts/ci/manifest-version-gate.py --base-ref origin/develop
python3 scripts/audit/test_collection_audit.py --check-baseline
python3.12 scripts/audit/repo-lint.py

git diff --check
```

完整 pin 補齊後，在獨立 K8s Odoo 沙盒對既有合成 DB 做 `-u woow_paas_platform`，focused tags：

```text
/woow_paas_platform:TestPiAgentSeed
```

實際 pod 命令依 [Testing Guide](../docs/guides/testing/TESTING.md#run-in-k8s-dev-sandbox) 使用 `-c /etc/odoo/odoo.conf`、`--http-port=18069 --gevent-port=18072`、`--stop-after-init`，確認至少原有 10 項 seed tests 被執行且 0 failed/0 errors。既有 service pin feature 的測試由 #1996 保持；不為本次 seed 更新擴充全平台行為。

## 既有平台套用方式（本次只交接，不執行）

這次只改現有 model 的資料，不需要新增欄位／全平台重建。parent 可在正式 image 與 chart 可拉取且完成既有核准流程後，透過管理員 ORM 只更新 `env.ref('woow_paas_platform.cloud_app_pi_agent')` 的 `helm_chart_version` 與 `helm_default_values.image.tag`。以當時 DB 的 JSON 為起點，只換 tag，不用整份 source defaults 覆蓋 DB 客製值；禁止對 cloud service 呼叫 write/redeploy。

套用前後核對商品兩個目標值，並在受保護記憶體內比較既有 Pi services 的 `helm_values`、`helm_chart_version`，若已部署 #1996 再比較三個 pin 欄位；只輸出是否相同，不輸出完整 values 或憑證。`noupdate=0` 意味日後 module upgrade 仍會重載 source seed，故 source PR 也必須經正常覆核整併，不能只改 DB 後永久留舊來源。這不是授權此次子任務修改 production。

## 本次備妥階段的實際結果

- XML/JSON source 範圍檢查通過；只改 Pi chart，原 image pin 保留，正式新映像檢查顯示 false。
- digest 斷言正向案例通過，五種無效格式均被拒絕（非 hex、長度少一／多一、結尾換行、錯誤算法）。
- `python3.12 scripts/audit/repo-lint.py`：全部通過；系統 Python 3.11 不符合此工具要求，改用既有 Python 3.12，未安裝額外依賴。
- `test_collection_audit.py --check-baseline`：C2 PASS（1289 個 baseline class），Pi seed 原有 10 個 test methods，未新增／改名 class。
- Odoo focused 測試與最終 registry pin 驗證仍待正式 digest；本次未執行完整 Odoo suite 或任何 production 操作。

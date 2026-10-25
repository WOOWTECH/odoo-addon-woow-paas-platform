# Pi Agent Omnigent 新開通預設 pin（WIP，待 canonical promotion）

## 狀態與範圍

本 PR 已同步 `develop` `a00d2a5075efba3d1bf51e5f942f935a49580ce0`（#2000）。manifest 沿用 `18.0.1.0.261`，不另行 bump。商品變更只含 Pi Agent chart `0.1.10` 與 `helm_default_values.image.tag`，其餘商品、資源、方案、價格與憑證設定均不變。

**不可合併／部署：canonical 尚未 promotion，chart 0.1.10 尚待正式發布。** 使用者已授權先準備下列 canonical pin；它是預備值，不是 canonical 已可拉取的證據：

```text
jcr-prod.woowtech.io/woow-paas-docker-local/pi-agent:20261025.1200@sha256:7152969a4ffa3f05f46b28f49d8899448e426d85014d5f05875f7ede563c120d
```

| 發布層 | 已知證據與待辦 |
| --- | --- |
| Root | 本任務已發布的 `v0.3.2`，plugin `0.3.11`；root main 另有 `0.3.3`／plugin `0.3.12` 並行工作，本 PR 不自行切換目標 |
| PaaS 候選 | [run27837](https://git-prod.woowtech.io/woow-paas/paas-odoo-ci/actions/runs/27837) SUCCESS；source `1edfb29be196454a11dfebf2688bc9abd45f2677`；實際候選 tag 為 `20261025.1200-pending-approval` |
| 候選 digest | `sha256:7152969a4ffa3f05f46b28f49d8899448e426d85014d5f05875f7ede563c120d`；parent 提供的 `pi-deployment-release-20261025/paas-candidate-receipt.json` 記錄 CI push digest、成功檢查及 `canonicalPublished=false`。這是 PaaS digest，非 root digest |
| Promotion | 真正人工核准為 [paas-odoo-ci issue409](https://git-prod.woowtech.io/woow-paas/paas-odoo-ci/issues/409)。等待授權人核准、canonical promotion 成功，再從 registry 讀回 canonical digest 比對；agent 不代簽 |
| PaaS chart | [woow-paas-charts PR278](https://git-prod.woowtech.io/woow-paas/woow-paas-charts/pulls/278)，head `6d5412e9423841bd549cb4b9d3be38541f1f6f46`；parent 回報六項 tests/lint 通過，待 canonical 才 merge／發布 OCI `0.1.10` |

候選 receipt 證明候選建置成功，不能冒充獨立 registry canonical 讀回；Odoo seed tests 也不會拉取 image 或 chart。

## 並行更新與衝突（2026-10-25 查核）

- PR1999 原 head `3d72c99a`、base `f6f40011`；Gitea 舊 8/8 checks 早於 develop #1997／#2000，不能當作新 head 的驗證。
- 原 head manifest `.260` 相對 develop `.261` 的 gate 實跑失敗；最新 develop 的 `merge-tree` 計算無內容衝突，已將本分支 rebase 到 `a00d2a50`。沒有合併任何 PR。
- [#1866](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/pulls/1866) 仍 open，同改 Pi seed 註解與較舊 image `20261019.0810`；後續人工整合不得把新 pin 倒退。新版 PaaS 是否完整包含該 PR 的 context-window 修正，由發布方核對。
- [#1996](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/pulls/1996) 單一服務 pin 與 [#1998](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/pulls/1998) release `.262` 仍 open，未改本 PR 的 seed/test/handoff 檔；其 `deployment_policy`／`deployment_chart_version`／`deployment_image_overrides` 不取代商品 default。
- 查到 #1999 之後只有已合併的 #2000（AI 額度零時提示），未找到取代 #1999 的新版 Pi seed PR。此結論不涵蓋未開 PR 的並行分支。

## Default 與既有服務邊界

新開通讀 `cloud_app_pi_agent.helm_chart_version` 與 `helm_default_values`。更新商品本身不觸發既有服務 rollout，不改其 `helm_values`，不回填單一服務 pin 欄位。

保留 `has_admin_gui=True`、`admin_password_carrier=helm_secret`、`authProxy.basicAuthUsername=admin`：Pi Web 密碼仍由平台供給。Omnigent server URL／帳號／密碼由首次設定對話要求租戶填寫，不寫入 seed。`piWeb.omnigentSetup` 沿用 chart 預設 true。

`template-follow` 服務若日後由管理員 Redeploy，會使用當時商品版本；已部署 #1996 且明確設為 `pinned` 的服務依 resolver 保留完整 pin。本次不 Redeploy，也不改服務的 policy。

## 實際隔離 Odoo 環境與跑法

沿用既有 `woow-k3s` 的 `dev-sandbox-pi-seed-20261025/pi-seed-tests`：namespace owner `codex-pi-pin-20261025`、purpose `pi-seed-pr1999`。Odoo／PostgreSQL 15 同 Pod，資料與 source 都為 `emptyDir`；無 PVC、Service、Deployment，deny-all ingress／egress，`automountServiceAccountToken=false`。未新增或修改任何基礎架構。

Odoo 測試映像：`paas-odoo-official:20261025.0938@sha256:713e8093d1b092df845f8e9a76eba43799c816bb99c316a70c34d332bd08c65f`。這是測試用 Odoo runtime，與待 promotion 的 Pi Agent image 是不同產物。

原合成 DB `pi_seed_pr1999` 已安裝 addon `.260`，含舊正式 Pi pin、0 筆 cloud services；本輪建立其專用副本 `pi_seed_pr1999_canonical`，保留原 DB。將本 worktree `src/` 的 tracked files 打包到 `/tmp/pi-pr1999-canonical-addons/woow_paas_platform`，必須讓此路徑優先於映像內建的舊 addon。

```bash
kubectl --context woow-k3s exec \
  -n dev-sandbox-pi-seed-20261025 pi-seed-tests -c odoo -- \
  env OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
  odoo -c /etc/odoo/odoo.conf \
  --addons-path=/tmp/pi-pr1999-canonical-addons,/opt/woow/addons,/usr/lib/python3/dist-packages/odoo/addons \
  --data-dir=/tmp/pi-pr1999-canonical-data \
  -d pi_seed_pr1999_canonical \
  --db_host=127.0.0.1 --db_port=5432 --db_user=odoo \
  -u woow_paas_platform --test-enable \
  --test-tags=/woow_paas_platform:TestPiAgentSeed \
  --stop-after-init --http-port=18069 --gevent-port=18072 \
  --workers=0 --max-cron-threads=0 --log-level=test
```

只接受 log 中原有 10 個不同 test methods 全部啟動、`0 failed, 0 error(s) of 10 tests` 且 exit 0；不能以「0 tests」當通過。`-u` 用來驗證舊合成 DB 升級後 source seed 確實重載，不需正式部署。

環境排障：本輪第一次無 `-u` 的舊 pin 重跑，在任何測試前出現 `RuntimeError: can't start new thread`。Odoo 匯入建立 64 個 BLAS threads，VMS 約 2.75 GiB，大於內建 hard limit 2.5 GiB。上述三個程序環境參數把 threads 限為 1，VMS 降至約 228 MiB，原 memory limit 下 thread probe 通過；沒有改 Pod、config 或平台程式碼。`--no-http` 無法解決，因 Odoo test mode 仍會啟動 HTTP thread。

## 本輪驗證結果

- 2026-10-25 14:15:34 UTC：新 canonical pin 的 `-u woow_paas_platform` 與 focused 測試 exit **0**；10 個不同 methods 全數執行，`0 failed, 0 error(s) of 10 tests`。這是本輪新 pin 的實測，沒有沿用舊 pin 的結果。
- Odoo `stats` 另列 12；本報告以 10 個實際 test methods、`10 post-tests` 與最終 result 的 **10** 為準。
- 原始 log：`/tmp/pi-agent-pr1999-canonical-20261025T141518Z/focused.log`；SHA-256 `0412aa2b0a26f78b7d11816c03a07daad69bb0c1986c4b298d69688d9bbc71e0`。可提交的節錄見 [測試證據](pi-agent-seed-20261025-evidence.txt)。
- 1,253 個 tracked source files 與 pod snapshot 逐檔相同。DB 讀回確認新副本為 canonical tag／指定 digest、chart `0.1.10`、addon `.261`、`noupdate=false`、0 筆 cloud services；原 DB 保留舊 pin 與 addon `.260`。
- Pi-only XML/JSON/source 檢查、manifest gate、C2 collection audit（1289 classes）均通過；repo-lint 全部通過，`git diff --check` 通過。
- 此結果只證明 Odoo 升級／seed 契約；不代表 canonical promotion、chart 發布或實際 Pi pod 啟動已完成。

## 局部來源驗證

```bash
python3 - <<'PY'
import ast, json, subprocess
from pathlib import Path
from xml.etree import ElementTree as ET

path = 'src/data/cloud_app_templates.xml'
base = ET.fromstring(subprocess.check_output(['git', 'show', 'origin/develop:' + path]))
head = ET.parse(path).getroot()
def records(root):
    return {r.attrib['id']: ET.tostring(r) for r in root.findall('.//record')}
a, b = records(base), records(head)
assert a.keys() == b.keys()
assert {k for k in a if a[k] != b[k]} == {'cloud_app_pi_agent'}
def pi(root):
    return root.find(".//record[@id='cloud_app_pi_agent']")
def fields(root):
    return {f.attrib['name']: ET.tostring(f) for f in pi(root).findall('field')}
a, b = fields(base), fields(head)
assert a.keys() == b.keys()
assert {k for k in a if a[k] != b[k]} == {'helm_chart_version', 'helm_default_values'}
assert pi(head).find("field[@name='helm_chart_version']").text == '0.1.10'
def values(root):
    return json.loads(pi(root).find("field[@name='helm_default_values']").text)
old, new = values(base), values(head)
old_tag = old['image'].pop('tag')
new_tag = new['image'].pop('tag')
assert old == new, 'Only image.tag may change in Helm defaults'
assert new_tag == '20261025.1200@sha256:7152969a4ffa3f05f46b28f49d8899448e426d85014d5f05875f7ede563c120d'
assert head.find('data').attrib['noupdate'] == '0'
manifest = 'src/__manifest__.py'
assert Path(manifest).read_bytes() == subprocess.check_output(['git', 'show', 'origin/develop:' + manifest])
tree = ast.parse(Path('src/tests/test_pi_agent_seed.py').read_text())
case = next(c for c in tree.body if isinstance(c, ast.ClassDef) and c.name == 'TestPiAgentSeed')
assert len([m for m in case.body if isinstance(m, ast.FunctionDef) and m.name.startswith('test_')]) == 10
print('PASS: Pi-only scope, canonical pin matches receipt, other values unchanged, noupdate=0, manifest matches develop, 10 test methods')
PY
python3 scripts/ci/manifest-version-gate.py --base-ref origin/develop
python3 scripts/audit/test_collection_audit.py --check-baseline
python3.12 scripts/audit/repo-lint.py
git diff --check
```

## Promotion 後的交接（本次不執行）

1. 授權人處理 issue409；確認 canonical `20261025.1200` promotion 成功，registry 讀回 digest 與上列值一致。
2. chart PR278 走正常合併流程，確認 OCI `0.1.10` 已發布。
3. 重新核對 develop／#1866 等並行變更；確認正式映像與 chart 存在、required checks 通過後走正常合併流程。本次依使用者範圍維持 WIP、不 merge、不簽 approve、不發布全 Odoo；不新增平台 approval 要求。
4. 正式套用若走管理員 ORM，只更新 `env.ref('woow_paas_platform.cloud_app_pi_agent')` 的 chart 與 image tag；以當時 DB 的 JSON 為起點，保留客製值。不得對 cloud services 呼叫 write/redeploy。`noupdate=0` 會在日後 module upgrade 重載 source seed，source PR 仍須正常覆核整併。

本輪保留沙盒、原 DB 與新合成副本供覆查，不刪除他人或先前建立的資源。

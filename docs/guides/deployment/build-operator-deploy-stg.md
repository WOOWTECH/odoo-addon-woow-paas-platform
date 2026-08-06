# reference build operator deploy stg

> 自 Claude memory 遷移（2026-08-05）。原 slug `reference_build_operator_deploy_stg`。盤點依據見 [memory-inventory-2026-08-05](../../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description：paas-operator 建置+部署 stg 全流程 gotcha — operator-v* tag 不觸發 CI（改 workflow_dispatch）、Fleet 落點、驗證法
>
> ⚠ **上面這行 description 的「tag 不觸發 CI（改 workflow_dispatch）」已於 2026-08-06 被實證推翻**，正確結論見下方 ①。保留原文只為留下這條錯誤結論的出處。

**paas-operator 建置 → 部署 stg 全流程**（2026-07-05 #279/#280 部署實證）：

**① `operator-v*` tag push 會觸發 CI —— `build-operator` skill 的 Step 5 是對的**（2026-08-06 全量複驗，**推翻本段 2026-07-05 起的舊結論**）。

本段先前寫「tag 機制在此 Gitea 上壞掉、`paths: extra/paas-operator/**` 把 tag push 吃掉、必須改用 `workflow_dispatch`」——**那是錯的**。實查 Gitea Actions API，遠端**每一個** `operator-v*` tag 都有一筆對應的 `event=push`、`path=build-paas-operator.yml@refs/tags/<tag>` 的 run，逐一對照零缺漏：

| tag | run | `path`（唯一有鑑別力的欄位） | event | job | 產出（log 實印） |
| --- | --- | --- | --- | --- | --- |
| `operator-v20260801.1305` | **1349** | `build-paas-operator.yml@refs/tags/operator-v20260801.1305` | `push` | success | `20260801.1305: digest: sha256:0f87ac73…` |
| `operator-v20260806.0718` | **1688** | `build-paas-operator.yml@refs/tags/operator-v20260806.0718` | `push` | success | `20260806.0718: digest: sha256:6a4f573c…` |

**舊段落引為「2026-08-01 再次實證不觸發」的那一次（run 1349），恰恰是它自己的反證。**

連帶推翻「`paths` filter 吃掉 tag push」這個推測：上表兩個 tag 指向的都是**沒有動到任何 `extra/paas-operator/` 檔案**的 merge commit，照樣觸發並完整 build + push。

**代價**：照舊結論做的人會在 tag 已經觸發之後再 dispatch 一次——run **1350**（2026-08-01）與 run **1689**（2026-08-06）都是這樣來的**多餘第二次 build**（全 `CACHED`、數秒）。同一個錯誤已害人做了兩次白工。

**複驗方式**（別信這張表的凍結結果，要查就重跑）：

```bash
TOKEN=$(python3 -c "import re;print(re.search(r'^\s*token:\s*(\S+)',open('$HOME/Library/Application Support/tea/config.yml').read(),re.M).group(1))")
# 逐頁抓完 /actions/runs，過濾 path 含 refs/tags/operator-v，與 git ls-remote --tags 的 operator-v* 對照
curl -sS -H "Authorization: bearer $TOKEN" \
  "https://git-prod.woowtech.io/api/v1/repos/odoo-addons/woow_paas_platform/actions/runs?limit=50&page=1"
```

**🔴 這條錯誤結論是怎麼來的——兩個假陰性探針（比結論本身更值得記）**

兩者都屬「**壞掉的樣子和正常的樣子一模一樣**」家族：查詢正常回應、格式正確、沒有任何錯誤訊號，只是答案是錯的。

- **(a) 用 `display_title`／`tea actions runs list` 的 WORKFLOW 欄辨識 run 來源 ⇒ 零鑑別力。** tag 指向的就是那顆 merge commit，所以 tag run 與同一顆 commit 的 branch run 標題**必然逐字相同**。`tea` 的表格更糟：WORKFLOW 欄印的是 **commit 訊息不是 workflow 檔名**，BRANCH 欄對 tag run 是**空的**，整列沒有任何字樣提到 `operator-v…`——

  ```
  │ 1689 │ completed │ Merge pull request 'chore: 自我測試步驟…' (#608) … │ develop │ workflow_dispatch │ 15:21 │ 7s  │
  │ 1688 │ completed │ Merge pull request 'chore: 自我測試步驟…' (#608) … │         │ push              │ 15:18 │ 53s │
  ```

  1688 就是那次 tag build，但看起來完全像「別人 merge 進 develop 觸發的」。**只有 API 的 `path` 欄位分得出來。**

- **(b) 用 `runs?limit=N`（或 `tea actions runs list`）的第一筆判斷「有沒有新 run」⇒ 假陰性。** 該列表按 **run id 遞減**排序、**不是**按執行時間（實測相鄰三筆 STARTED 為 22:59 / 23:16 / 22:57，完全不遞減），且 `N` 是**全 repo 所有 workflow 共用的視窗**——本 repo 每個 PR 都會觸發數條 lint/gate workflow，小視窗很快就把 build run 擠到看不見（實測 1688 在 `tea list` 預設輸出落在第 24 列）。
  順帶一個會再錯一次的點：**API 印 UTC（`07:18:46Z`）、`tea list` 印本地時間（`15:18`）**，拿兩者直接比對時間會得到錯誤結論。

⇒ **正解：按 `path` 欄位過濾，或直接按 run id 查 `/actions/runs/{id}`（單筆端點可靠、含 in-progress）。判成敗再往下查 job 層 `/actions/runs/{id}/jobs` 的 `conclusion`**（run 層 `conclusion` 在列表回應中常為 `null`）。

**`workflow_dispatch` 仍然有用，但它是補救手段、不是必要路徑**：想對同一顆 commit 重 build 又不想再打一個 tag 時用——

```bash
tea actions workflows dispatch build-paas-operator.yml --repo odoo-addons/woow_paas_platform \
  --login eugene --ref develop --input tag=<YYYYMMDD.HHMM>
```

（workflow 的 `Resolve image tag` 對 `inputs.tag` 優先，冪等。）想把 dispatch 釘在 tag 上時 `--ref` 用 tag 名會被拒（ref doesn't exist），必須寫全名 **`--ref refs/tags/operator-v<版>`**（2026-08-01 實測；此點與「tag push 是否觸發」無關，仍成立）。

另：operator 的 values-prod.yaml image 段**無 digest pin 欄位**（只有 repository/tag/pullPolicy，digest 僅記註解）——「digest pin 優先於 tag、只改 tag 是 no-op」的教訓屬 paas-odoo values，operator 檔改 tag 即生效，勿捏造 digest 欄位。

**② operator docker build ~10-18s 是正常的**（act-runner-runner-0 layer cache 熱）；`#N CACHED` 全 CACHED＝「同一 develop HEAD 之前建過」非「舊碼」。驗證真的建對：`tea actions runs view <id>`（Conclusion: success）+ `tea actions runs logs <id> | grep 'digest:'`（docker push 成功印 `<tag>: digest: sha256:… size:`）+ 比對 registry digest ≠ 現況 pod imageID digest（排除 cache 供舊碼）。

**③ Fleet 落點**：單一 GitRepo `paas-app-bundle`（ns `fleet-local`）watch `woow-paas/paas-app-bundle-cd` 的 **`main` branch**（repo 預設分支 `master` 是空的、別被騙）。operator image pin：
- stg → `paas-operator-stg/values-stg.yaml`（`image.tag`）
- prod → `paas-operator/values-prod.yaml`
改 stg 只動 stg 檔、commit+push `main` → Fleet ~1-2 分鐘 reconcile → deployment rolling。**`git push` 到此 CD repo 會撞 auto-mode 權限**（需使用者授權 / `Bash(git push:*)` 規則 / 使用者 `!` 前綴自跑）。

**④ 驗證 rollout**：`kubectl -n paas-stg-system get deploy paas-operator-stg -o jsonpath image` == 新 tag + `readyReplicas=1` + 新 pod 0 restart；F6 guard 過看 pod log `F6 env guard OK — PAAS_ENV=stg, namespace_prefix=paas-ws-stg-`（PAAS_ENV≠prefix env 會 lifespan fail-fast CrashLoop）；新 endpoints 存在 `kubectl exec <pod> -- python3 -c '…openapi.json…'` 撈 paths。stg operator env（PAAS_ENV=stg / NAMESPACE_PREFIX=paas-ws-stg-）在 values-stg.yaml `config.paasEnv`+`config.namespacePrefix`。

相關：[[deploy-stg-gotchas]]（odoo image 走 tea tag，與 operator 不同路）；[[agent-runtime-respawn-mechanism]]（release chain）；[[netpol-279-280-landing]]。

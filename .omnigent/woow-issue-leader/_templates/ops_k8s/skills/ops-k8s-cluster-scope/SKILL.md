---
name: ops-k8s-cluster-scope
description: woow-k3s 共用叢集的操作範圍守則：namespace 白名單、唯讀盤點分流、三層守線與繞過方式。
---

# ops-k8s-cluster-scope

## 什麼時候用

任何要下 `kubectl --context woow-k3s` 的時候，先過一次這條。

## 1. 先分類：唯讀還是寫入

- **唯讀**（`get` `describe` `logs` `top` `wait` `events` `explain` `diff`
  `api-resources` `api-versions` `cluster-info` `auth`）：跨 namespace 允許，僅供歸屬分
  流。非白名單 namespace 的資源**只列不動**。
- **寫入／exec**（其餘一切動詞）：namespace 必須在白名單內，而且**要明確帶 `-n`**
  ——P-kubectl 是 fail-closed，沒帶 `-n` 會被 DENY（不要靠 current namespace）。

## 2. namespace 白名單

`paas-ws-*`、`paas-ws-stg-*`、`paas-system`、`paas-stg-system`、
`paas-cloudflare-system`、`paas-stg-cloudflare-system`、`dev-sandbox-*`、
`fleet-local`（**僅限 `paas-*` 前綴資源**）。

⚠ `fleet-local` 的資源名這一層**沒有機器守線**——只有你自己記得。`paas-*` 以外的
GitRepo／Bundle／secret 一律不碰。

⚠ 這份白名單在本 bundle 是第 4／5 份副本（權威三份由 repo-lint `kubectl-ns-whitelist-sync`
對帳，本 bundle 的兩份不在對帳範圍）。詳見 `AGENTS.md`。

## 3. `--context` 一定要寫字面值

守線靠解析指令字串判斷 context，寫在 shell 變數裡會解析不到而被攔。

## 4. 撞到守線怎麼辦

- 撞到 `.claude/hooks/kubectl-ns-guard.py`：先確認你真的該碰那個 namespace。真的需要
  （已與叢集運維協調過）才用一次性前綴 `KUBECTL_NS_GUARD=off `。
- 撞到 P-kubectl（policy 層）：多半是**沒帶 `-n`** 或 namespace 拼錯。⛔ 不要改寫指令去
  繞過守線——那是在騙自己。

## 5. 非白名單 namespace

叢集全域維運屬 `woow-machines-operator` 職責。回報給上層轉該管道，⛔ 不要自己動手。
叢集級設定（default StorageClass、Longhorn settings）影響全叢集，動之前必須協調。

## 6. deploy-stg 兩段式（repo 的 `deploy-stg` skill 你看不到，這裡是精華）

script 在 `<repo root>/.claude/skills/deploy-stg/scripts/deploy-stg.sh`。⛔ **不要整支直接跑**
（不帶 `--image`，讓它自己建 tag ＋輪詢 CI）：輪詢迴圈裡反覆執行的 `sleep` 會被 agent
harness 砍掉，script 以 `exit 5` 死在 `Main branch SHA: …` 之後、極難診斷（單發 `sleep` 探針
正常，測不出來）。而且 **`exit 5` ≠ 無副作用**——tag 與 CI build 已留下，重跑整支會 triple
build。`--skip-build` 只是「tag 已在、只等 CI」的變體，一樣進輪詢迴圈，⛔ 也不要用。

正解三步，每一步都是獨立 tool call：

1. **建 tag 觸發 CI**（`paas-odoo-ci` repo，tag 名 `stg-<YYYYMMDD.HHMM>`）：
   ```bash
   MAIN_SHA=$(tea api -r woow-paas/paas-odoo-ci "repos/{owner}/{repo}/branches/main" | jq -r '.commit.id')
   tea api -X POST -r woow-paas/paas-odoo-ci "repos/{owner}/{repo}/tags" -f "tag_name=stg-<YYYYMMDD.HHMM>" -f "target=${MAIN_SHA}"
   ```
   前置：`tea login list` 綠**不代表** API 打得通（token 過期／CF Access 攔截照樣綠），第一
   條 `tea api` 就是真探針。若 `blast_radius` 對建 tag 跳 ASK，回報 blocked，⛔ 不要繞。
2. **自己查 CI 狀態**（每次查都是一個新 tool call，⛔ 不要寫 `sleep` 迴圈）：
   ```bash
   tea api -r woow-paas/paas-odoo-ci "repos/{owner}/{repo}/actions/tasks?limit=10"
   ```
   看 `head_branch` 等於你的 tag 那筆的 `status`：`success` 才進第 3 步；`failure`／
   `cancelled` 回報。⚠ **tag-push 觸發的 run 可能完全不出現在這個 API**（實證兩次，build
   其實 2 分鐘內就完成）——**API 查無 ≠ 未觸發，嚴禁重推 tag**。多次查空就看 ground truth：
   `kubectl --context woow-k3s -n gitea logs act-runner-runner-0 -c runner --tail=400 > /tmp/runner.log`
   （唯讀跨 ns 盤點，允許；⛔ 不要接 `| grep`，P-kubectl 會擋——存檔後另一個 tool call 再 grep
   `paas-odoo-stg:<YYYYMMDD.HHMM>`）。runner 全叢集只有 1 個 slot，prod build 卡 approval gate
   會佔住它，這時同樣**別重推**。
3. **用 `--image` 收尾**（這條路徑不經過輪詢迴圈）：
   ```bash
   bash <repo root>/.claude/skills/deploy-stg/scripts/deploy-stg.sh \
     --image jcr-prod.woowtech.io/woow-paas-docker-local/paas-odoo-stg:<YYYYMMDD.HHMM> \
     --slug <slug> --context woow-k3s
   ```
   image tag ＝ git tag 去掉 `stg-` 前綴。部署到哪個 sandbox（`dev-sandbox-<slug>`）、
   addon 版號 post-check 的結果，連同你實際跑的三段指令與輸出一起寫進回報。

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

---
title: Hermes Agent chart 升級 recipe（既有租戶）
category: guides
status: active
created: 2026-08-16
updated: 2026-08-16
related:
  - docs/decisions/hermes-agent-installer-landing-env.md
  - docs/decisions/hermes-agent-command-execution-planes.md
  - docs/guides/deployment/hermes-agent-console-support-runbook.md
---

# Hermes Agent chart 升級 recipe（既有租戶）

## 適用場景

**既有 hermes-agent 租戶要把 chart 升級到新版本**（例如 0.1.3 → 0.2.0）。新部署不適用本文——
新租戶由平台 seed（`cloud_app_template.helm_chart_version`）自動帶最新版，不需要走這套 recipe。

> 現況（叢集裡有哪些租戶在哪個版本、prod 目前有幾個租戶）以叢集實查為準，本文不記錄快照——
> 查法見下方「升級後驗收」的判別法 A/B。

---

## 步驟

### 1. 保全 rollback 材料

升級前先留一份「回得去」的底：

```sh
helm -n <ns> get values <release> -o yaml > <release>-values-before.yaml
helm history <release> -n <ns> > <release>-helm-history-before.txt
```

沒有這兩份，升級後想 rollback 只能憑記憶重建 values，容易漏欄位。

### 2. 先量負向對照，再升級

升級前先跑一次下方「判別法 A/B/C」，確認**現在還沒有**這次要升的修正，再動手升級。

**為什麼要有這個順序**：如果只在升級後量一次，沒辦法區分「這次升級生效」與「本來就這樣」。
尤其判別法 C（容器內目錄存在性）租戶自己 `mkdir` 或裝過工具也會讓它變綠——必須有 before/after
兩個時間點的落差對照，判準才有鑑別力。

### 3. ⛔ 禁用 `--reuse-values`

```sh
# ⛔ 不要這樣升級
helm upgrade <release> <chart> --version <new-version> -n <ns> --reuse-values

# ✅ 改用保全下來的 values 明確帶入
helm upgrade <release> <chart> --version <new-version> -n <ns> -f <release>-values-before.yaml
```

`--reuse-values` 會丟掉新 chart 版本新增的預設值（例如 0.2.0 新增的十個安裝器落點覆寫 env），
但 `helm` 照樣 `exit 0`——**症狀與「根本沒升級」不可區分**，是最容易踩的假綠陷阱。

### 4. 🔴 helm 大版本要配 operator（3.13.3）

平台 operator 目前用 helm **3.13.3**，release secret 是 Helm 3 格式
（`type: helm.sh/release.v1`）。若用本機裝的 helm 4 執行升級，有把新 revision 寫成 operator
讀不到格式的風險。

- 操作前備妥與 operator 同版本（3.13.3）的 helm 執行升級。
- 升級後複驗新 revision 的 release secret type 仍正確：

  ```sh
  kubectl -n <ns> get secret sh.helm.release.v1.<release>.v<新 revision 號> -o jsonpath='{.type}'
  # 預期輸出：helm.sh/release.v1
  ```

### 5. 升級後驗收

⛔ 不要只看 `helm upgrade` 的 exit code 是 0 就當作升級生效，逐項驗：

**判別法 A/B/C** — 「這個部署有沒有含某次 chart 修正」的判別方法，權威定義見
[hermes-agent-installer-landing-env 決策文件](../../decisions/hermes-agent-installer-landing-env.md)
§6（本文不複寫細節，避免雙寫漂移）。摘要：

- **A**（container spec env 名單，唯讀、不進 pod）：
  ```sh
  kubectl -n <ns> get deploy <release>-hermes-agent \
    -o jsonpath='{.spec.template.spec.containers[?(@.name=="hermes-agent")].env[*].name}'
  ```
- **B**（initContainer 名單）：
  ```sh
  kubectl -n <ns> get deploy <release>-hermes-agent \
    -o jsonpath='{.spec.template.spec.initContainers[*].name}'
  ```
- **C**（容器內目錄存在性，輔助用、不可單獨當判準）：
  ```sh
  kubectl -n <ns> exec <pod> -c hermes-agent -- stat -c '%n %U:%G %a' <目錄>
  ```
  ⚠ **判別力只在 owner**：group／mode 每次 pod 掛載都會被 kubelet fsGroup 重寫成固定值，
  對「誰建的」恆綠、驗不出東西。

**第三態 log 判讀**（相容處理類 initContainer，如 `cli-landing-init`）：

升級前先用唯讀探針查一次目標相容路徑（例如 `~/.local/bin`／`~/bin`）現況，據此**預測**這次
升級的 init log 會走哪一支分支（路徑不存在 → 建立；已是正確 symlink → no-op；已是租戶真目錄
→ loud error、不覆寫不刪除）。

⚠ **log 沒出現預期字樣＝沒走到那個分支，不是「通過」**——三態各有各的預期輸出，不能只 grep
一個關鍵字（例如 `ERROR`）就判定整個 init 正常；也不能反過來看到一個 `ERROR` 就當整個 init
失敗，要逐條核對是哪一條相容路徑、是否符合升級前探針預測的分支。

### 6. 停機面（如實記錄，不美化）

hermes-agent 是單 replica 的 Deployment（非 DaemonSet），`helm upgrade` 會觸發 pod 重建：

- agent session 中斷約數十秒。
- 若 image digest 沒變（這類升級通常只改 chart spec、不換 image），重啟時間主要花在 pod
  重建與 initContainer 執行本身，不是拉取新 image。

---

## 相關

- [判別法 A/B/C 與六家族安裝器落點覆寫變數的權威定義](../../decisions/hermes-agent-installer-landing-env.md)
- [四個指令執行面分析](../../decisions/hermes-agent-command-execution-planes.md)
- [`/console` 支援除錯 runbook](hermes-agent-console-support-runbook.md)

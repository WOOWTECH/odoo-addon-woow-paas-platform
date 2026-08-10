---
name: ci-local-stale-pod-green-tests-on-old-code
description: ci-local sandbox 的 code 是 image 快照，pod 一被重建就回到 image 烤進去的舊版、前一輪 kubectl cp 的修補全消失——而且失效是靜默的：pod 1/1 Running 無警告、test.sh 照跑照綠，那個綠燈驗的是舊碼；沒先讀 pod 內版號就跑測試，會產出一份有測試數字佐證但錨點錯掉的假驗收報告
metadata:
  type: pitfall
---

# Pitfall: ci-local sandbox 靜默退回舊碼——測試全綠，但綠的是 image 烤進去的那版

> 來源：2026-08-10 驗收 PR #650 merge 後的 develop（`68342517`）。sandbox
> `dev-sandbox-hermes013`（`docker-desktop`、ci-local 模式）。
>
> **姊妹篇**：[[ci-local-kubectl-cp-root-owned-addons]] —— 同一個 ci-local 設計的另一半。
> 本篇是「**你不知道自己在測哪個版本**」，姊妹篇是「**你想用 `kubectl cp` 補上去卻補不進去**」。
> 兩者咬合：發現退回舊碼 → 想熱蓋回來 → 撞權限。**兩篇都讀，才是完整的一條路。**
>
> **證據來源分兩層**：現象、實值表、機制敘述由當次操作者提供（第一手）；**本檔作者於同一天、
> 同一顆 sandbox 上以唯讀方式逐項覆驗**，並補查了 namespace 標記（結論與原素材的推論有一處
> 需要加限制，見 §機制 的「⚠ 旁證不可靠」）。逐項標示於下。

## Symptom：**沒有症狀**——這正是本篇的重點

pod 重建之後：

- `kubectl get pod` 顯示 `1/1 Running`、`RESTARTS 0`，**沒有任何錯誤或警告**。
- 直接跑 `test.sh` 完整 suite，測試**照樣執行、照樣可能全綠**——image 裡烤的是目標 commit
  之前的碼，那份碼在它自己的世界裡是自洽的（舊 seed 值配舊測試基準，該過的照樣過）。
- 唯一可能露餡的地方，是「這次剛好改的欄位有具名判準測試」。但**若新舊值都能通過同一組
  判準**（例如兩個值都「夠用」，只有「是否最小」的判準能分辨），連測試結果都不會告訴你
  測到的是舊碼。

⇒ 沒有「先讀 pod 內版號再跑測試」這道前置查核，會發生的事是：
**跑 suite → 拿到綠燈 → 回報「驗證通過」，而那個結論驗的是舊碼。**
整條驗收鏈看起來完整、還有測試數字佐證，只是錨點錯了。

（這次抓到純粹是因為派工單明寫「先驗 pod 內現值，不是就先補」。少了那一步，本輪會直接
產出一份「develop 驗證通過、suite N passed」的假報告。）

## 實證：pod 內實值 vs 目標 commit 應有值

pod `hermes013-odoo-dev-sandbox-odoo-7c4dc98597-94l8b`，`startTime 2026-08-10T05:42:33Z`、
`restartCount 0`——**不是同一顆 pod 重啟，是 rollout 換上來的新 pod**。

| 欄位 | pod 內現值 | develop@`68342517` 應有值 | 鑑別力 |
| --- | --- | --- | --- |
| `src/__manifest__.py` 的 `version` | `18.0.1.0.120` | `18.0.1.0.121` | ✅ 兩版不同 |
| `min_vcpu`（`cloud_app_hermes_agent`） | `2` | `1` | ✅ 兩版不同 |
| `min_ram_gb`（同上） | `3.0` | `2.5` | ✅ 兩版不同 |
| `helm_default_values` 是否含 `webui` | 不含 | 不含 | ❌ **零鑑別力**（兩版一致） |

> 上表四列由本檔作者在 pod 內（唯讀 `exec`）與 worktree 的 `origin/develop` 兩邊各讀一次
> 覆驗；前三列一字不差。第四列沿用原素材，它自己就標明了那是非鑑別欄位。

**這張表最該學的是最後一列**：驗「pod 裡是哪個版本」要挑**兩版之間確定不同**的欄位當探針。
拿兩版一樣的欄位去驗，不論結果如何都推不出結論——而它看起來跟一次成功的驗證一模一樣。

## 機制：ci-local 的 code 是 image 快照，容器可寫層的修補不隨 pod 走

`k8s-dev-sandbox` 的兩種掛載模式：

- **local（hostPath）模式**：addon 目錄 bind-mount 宿主機路徑，改本機檔案 pod 內立刻看得到
  ⇒ **沒有本篇這個坑**。但 docker-desktop 上這個模式已不可用（k8s node 是容器化的
  `desktop-control-plane`，看不到 macOS 的 `/Users`），見
  [docker-desktop-ci-local-sandbox-image-import](../../guides/testing/docker-desktop-ci-local-sandbox-image-import.md)。
- **ci-local 模式**：addon code **烤進 image**（`docker build` 時 `COPY` 進去），容器啟動後這份
  code 就是「build image 那一刻」的快照。`kubectl cp`、`kubectl exec -i … 'cat > file'` 這類操作
  寫的是**容器可寫層**，只疊在 image 快照之上——**不回寫 image、也不跟著 Deployment 的 pod
  template 走**。

任何讓 pod 重新排程的事件——`helm upgrade` / rollout、OOM、node 驅逐、甚至只是
`kubectl delete pod` 讓 controller 重建——都會從**同一份 image** 長出乾淨的容器層，
先前疊上去的修補不會被帶過去。跑一次 `test.sh` 就會發生一次：它在測試成功與失敗**兩條路徑
的最後都會** `kubectl rollout restart`（測試用 `--stop-after-init`，跑完要把 odoo 拉回來），
見 `.claude/skills/k8s-dev-sandbox/scripts/test.sh`。

本次研判是 sandbox 在驗收窗口內經歷了一次 pod 重建：namespace 的
`sandbox.woow.tw/expires = 2026-08-11T05:42:32Z`（24h TTL ⇒ 標記寫於 `05:42:32Z`）與 pod 的
`startTime 05:42:33Z` **只差一秒**。

> ⚠ **旁證不可靠（本檔作者補查，原素材沒有這一段）**：同一顆 sandbox 的 namespace 上還有
> 另一組標記 `dev-sandbox.io/created = 2026-08-10T12:03:47Z`、`dev-sandbox.io/ttl = 48h`、
> `dev-sandbox.io/expires = 2026-08-12T12:42:14Z`（兩組標記出自不同世代的工具）。
> **`created` 比 pod 的 `startTime` 晚了六個多小時，而 pod 仍是同一顆（`restartCount 0`）**
> ⇒ namespace 標記會被後續操作重新蓋寫，**與 pod 世代脫鉤**。
> 上一段的「差一秒」可以支持本次的判斷，但**不要把「看標記推 pod 世代」當成通用手法**——
> 唯一可靠的是直接讀 pod 內的檔案實值。

## 為什麼 `kubectl cp` 不是解法（兩個獨立原因，各自都足以否決）

1. **不持久**：如上，補丁只活在容器可寫層，下一次 pod 重建就沒了。就算這次補成功、suite 也
   真的測到新碼，**那個「測到新碼」的狀態本身是脆弱的**——它隨時會因為下一次 rollout 消失
   （而 `test.sh` 自己每跑一次就製造一次 rollout），不是穩定可重現的驗收環境。
2. **不可寫**：目標檔／目錄是 `root:root` `0644`/`0755`，容器執行身分是 `uid=100(odoo)`
   ⇒ 連「趁 pod 還沒重建先補上去」都走不通。當次實測兩種寫法都被擋：

   ```
   kubectl cp …                       → tar: cloud_app_templates.xml: Cannot open: File exists   (exit 2)
   kubectl exec -i … sh -c 'cat > f'  → sh: 1: cannot create …: Permission denied                (exit 2)
   ```

   （注意兩者的訊息**不一致**：走 tar 的那條會說謊成「File exists」。完整解釋與可用的繞法
   見姊妹篇 [[ci-local-kubectl-cp-root-owned-addons]]。）

⇒ 當次連「補丁能不能撐到測完」都還沒機會驗，第一關「補不補得進去」就先被擋下。
**姊妹篇提供的 `kubectl debug --target` + 數字 chown 繞法只解決第 2 點，解決不了第 1 點**——
補進去之後仍然活不過下一次 rollout。

## 什麼才是解法（成本與前提）

| 方案 | 前提 | 成本 | 適用時機 |
| --- | --- | --- | --- |
| **重建 image 帶入目標 commit** | 有磁碟空間、build 管線可用 | 高（build 時間＋磁碟；當次 host 磁碟 8.4Gi／100%，明令禁止 build） | 需要**穩定、可重複**驗收環境時的正規解 |
| **local／hostPath 模式** | k8s node 看得到宿主機路徑 | 低（改檔即時生效，不用 build） | 能掛 hostPath 的叢集上最省；**docker-desktop 上結構性不可行** |
| **stg 模式 sandbox** | 走真實 image pull，不 bind-mount | 中（仍是 image 快照，但不撞 hostPath 那個坑） | 需要真 HTTP session／真 ACL 的 e2e。⚠ **待補：當次未驗證 stg 模式是否同樣有 root-owned 寫入限制** |
| **chown 後再 cp** | 需要 root 執行路徑（見姊妹篇的 `kubectl debug --target`） | 中；且只買到「這一顆 pod 的壽命」 | 只有在**確定接下來不會再有 pod 重建**、且能接受權限模式與正式環境不同時才值得——一般不建議 |
| **不重跑，改用內容比對 ＋ 獨立證據鏈** | 已有其他獨立證據（見下節） | 低 | 目標 commit 與已驗證版本內容可證明一致時 |

## 判斷指引：什麼時候可以「不重跑」，什麼時候一定要重跑

**可以接受不重跑**，需**同時**滿足：

1. 目標 commit（如 develop 的 merge commit）與**已被獨立驗證過**的版本（如 PR head）之間，
   用 `git diff` 或等效手段確認**內容逐位元組相同**——不是「看起來差不多」，是 diff 真的空。
2. 兩者之間**沒有夾帶其他人的改動**（`git log A..B` 逐個 commit 確認來源）。
3. 已驗證過的那版，其「綠燈」有**比『suite 跑完沒紅字』更強的證據**——例如用 SQL 直接查 DB
   實值 ＋ `write_date` 時間戳對帳，證明 `-u` 真的把新值寫進 DB、轉綠不是巧合或殘留快取。
   **單純「跑過一次 0 failed」不夠格**：本篇 §Symptom 已經說明那個綠燈本身可能來自舊碼。
4. 把「這次沒有重跑，推論鏈由 X／Y／Z 組成」**明確寫進報告**，別讓讀者以為是重新驗過的。

**一定要重跑**，只要以下任一成立：

- 目標 commit 與已驗證版本**內容有差異**（哪怕只是空白或註解——沒逐位元組比對過就不能假設沒差）。
- 找不到「內容相同」以外的獨立證據鏈（例如這是第一次驗這條改動，沒有前一輪的 DB 層佐證可承接）。
- 判準測試涉及**環境相依的數值**（例如 quota 的兩向算式會隨 chart 版本、容器 resources 改變）
  ——這類測試的「過」高度依賴當下環境實況，內容比對取代不了重新執行。
- 要交付的結論是「**這個環境現在是健康的**」而不是「這段程式碼邏輯上是對的」——前者本質上就是
  在斷言一個當下狀態，離開「現在重新測過」就沒有意義。

## 前置查核：跑測試前先讀 pod 內的實值（可直接複製）

```bash
NS=dev-sandbox-hermes013
POD=$(kubectl --context=docker-desktop -n $NS get pod \
  -l app.kubernetes.io/component=odoo -o jsonpath='{.items[0].metadata.name}')

# pod 內版號
kubectl --context=docker-desktop -n $NS exec "$POD" -- \
  grep -n version /mnt/extra-addons/woow_paas_platform/__manifest__.py

# 本機目標 commit 的版號（在對應的 worktree 內）
grep -n version src/__manifest__.py
```

兩邊不一致 ⇒ **pod 裡不是你要驗的那份碼，先處理環境再跑測試**。

⚠ **探針必須先證明自己會說話**：這道查核要求兩邊**都吐出非空結果**才算有效。本次寫這份 doc 時
就踩過一次——用 `grep "'version'"`（單引號）去撈，而 manifest 寫的是 `"version"`（雙引號），
於是**兩邊都是空的、exit 1**，畫面看起來就像「沒有差異」。
拿到空結果時先假設探針壞了，不要先假設「兩邊一樣」。

## 機械化狀態：`—`（已評估、落點明確，但本次是純 doc PR 不動 code）

與姊妹篇不同，**這條有明確可掛載點**：`.claude/skills/k8s-dev-sandbox/scripts/test.sh` 在跑測試
**之前**對帳「pod 內的 code」與「期望的 commit」，不一致就 fail-loud（它已經有一道
「0 tests 也報綠」的 false-green guard，同一個位置再加一道同型的）。兩種判準與各自的弱點：

- **比版號**（pod 內 `__manifest__.py` 的 `version` vs 本機 worktree 的）：實作成本低，但
  **版號不是每次改動都會 bump** ⇒ 抓得到本次這種（`.120` vs `.121`），抓不到「同版號不同內容」。
- **比內容指紋**（兩邊對 addon 檔案集算同一種 hash 再比）：不漏抓，但要處理 `.pyc`／建置時
  被改寫的檔案（例如 image 建置時 `sed` 動過 `requirements.txt`）造成的天然差異。

⇒ 誰要落這道 gate，**第一個測試請注入紅燈**（拿一顆版號故意不同的 pod 驗它真的會擋），
不要只跑一遍看乾不乾淨——「跑起來沒紅」對「這道檢查是否真的有效」零鑑別力。

## 相關

- 索引：[docs/tracking/pitfalls/README.md](README.md)
- **姊妹篇**：[[ci-local-kubectl-cp-root-owned-addons]] — 想熱蓋回去時撞到的權限牆（含可用的繞法）
- ci-local 建置與跑測試：
  [docker-desktop-ci-local-sandbox-image-import](../../guides/testing/docker-desktop-ci-local-sandbox-image-import.md)、
  [odoo-sandbox-ci-local-two-track-test](../../guides/testing/odoo-sandbox-ci-local-two-track-test.md)
- 同 family（**綠燈本身可能是假的**）：
  - [[sandbox-module-test-false-green]] — `test.sh` 只看 exit code ⇒ 0 tests 也報綠
  - [[tag-scoped-test-misses-full-u-failures]] — 驗綠的 scope 不對 ⇒ 漏抓
  - [[provision-e2e-green-on-chart-defaults]] — e2e 綠，但驗證跑在 chart 預設值上

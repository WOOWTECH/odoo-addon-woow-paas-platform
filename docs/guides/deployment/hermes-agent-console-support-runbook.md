---
title: Hermes Agent /console 支援除錯 runbook（L3 不是信任邊界）
category: guides
status: active
created: 2026-08-16
updated: 2026-08-16
related:
  - docs/decisions/hermes-agent-command-execution-planes.md
  - docs/decisions/hermes-agent-installer-landing-env.md
  - docs/guides/tutorials/hermes-agent-installing-cli-tools.md
  - docs/guides/deployment/shared-cluster-paas-scope-guard.md
---

# Hermes Agent `/console` 支援除錯 runbook

> 對象：進租戶 Hermes Agent 容器除錯的平台支援工程師 / 維運。
> 對應 issue：`#717`（與 chart `hermes-agent` 0.2.0 同批發）、`#718`。
> 租戶側的對應文件是[安裝 CLI 工具指南](../tutorials/hermes-agent-installing-cli-tools.md)。

## 0. 一句話

**`/console` 是 root shell，而它的 `PATH` 前段有一個租戶可寫的目錄。
在那裡打任何裸指令名，都可能是在以 root 執行租戶的碼。**

除錯**只用絕對路徑**，或用 `PATH=<已知安全路徑> <cmd>` 前綴。

---

## 1. L3（`/console`）不是信任邊界——這是明文宣告，不是提醒

事實（皆實測）：

- `/console/` 的網頁終端最終是 `kubectl exec -it <pod> -c hermes-agent -- /bin/bash`，
  拿到的是 **uid 0**。agent container 刻意以 root 起（s6-overlay 自己 drop 到 uid 10000），
  這一點不能改（改了映像開機即失敗）。
- 該 shell 的 `PATH` ＝ 映像烘進去的那條：

  ```
  /opt/hermes/bin:/opt/hermes/.venv/bin:/opt/data/.local/bin:/usr/local/sbin:/usr/local/bin:…
  ```

  **第 3 格 `/opt/data/.local/bin` 在 `/usr/local/bin`（第 5）、`/usr/bin`（第 7）之前**，
  而它是租戶的持久磁碟、租戶可寫。
- ⇒ 租戶放一個叫 `ls` / `git` / `kubectl` 的檔案，下一位在 `/console` 打那個指令的支援工程師
  就以 uid 0 執行租戶的碼。

⚠ **這條通道與 chart 0.2.0 無關，映像本來就是這個 PATH 排序。** 0.2.0 的
`cli-landing-init` 把那個目錄**建了出來**，等於把潛伏態轉成現行態——所以圍堵必須跟它同批生效。

### 禁止事項（實測會出事，不是理論）

| ⛔ 不要做 | 會怎樣 |
|---|---|
| 在 `/console` 打裸指令名除錯 | PATH shadowing：可能執行租戶放的同名檔 |
| `bash -l`、`su - hermes`、`HOME=<租戶home> bash` | login shell 會 source 租戶的 `.profile`。實測輸出過 `[.profile EXECUTED as uid=0]` ——**以 root 執行租戶的 rc** |
| 在 `/console` 跑任何安裝指令（見 §3） | 以 root 把檔案寫進租戶目錄，租戶自己刪不掉 |
| 整包 dump `environ` / `env` | daemon environ 內含 `API_SERVER_KEY`、`HERMES_DASHBOARD_BASIC_AUTH_PASSWORD`／`_SECRET`；dump 會把它們印進對話並落地到 PVC 上的 `state.db`（任何 console session 都讀得到）。一律用白名單 `grep` |

### 該怎麼做

```sh
# ✅ 絕對路徑
/usr/bin/ls -la /opt/data/
/usr/bin/git --version

# ✅ 或前綴一條乾淨的 PATH
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin ls -la /opt/data/

# ✅ 要看 env 只看指定的鍵，不要整包 dump
env | grep -E '^(API_SERVER_KEY|MINIMAX_API_KEY|OPENROUTER_API_KEY)=' | awk -F= '{print $1" len:"length($2)}'

# ✅ 要以租戶身分做事：用 su 但不要帶 -（不觸發 login shell、不 source 租戶 rc）
su hermes -c '/opt/data/.local/bin/<tool> --version'
```

⚠ `su hermes -c 'echo $PATH'` 這種寫法沒有鑑別力——它回顯的是你自己的輸入，不是觀測到的環境。

---

## 2. 🔴 chart 的落點 env 對 `/console` 同樣生效

`hermes-agent` 0.2.0 起，container spec 上有十個安裝器落點覆寫 env
（`UV_TOOL_BIN_DIR`、`npm_config_prefix`、`GOBIN`、`CARGO_INSTALL_ROOT`…，完整清單見
[支撐 doc](../../decisions/hermes-agent-installer-landing-env.md) §0）。

`kubectl exec` 進去的行程**同樣繼承這些 env**——它們是 pod spec 的一部分，不是租戶那一面
專屬的設定。

**後果**：你在 `/console` 以 root 跑 `npm install -g <pkg>`（或 `uv tool install`、
`cargo install`、`go install`），檔案會落進**租戶的** canonical 目錄
`/opt/data/.local/bin`，而且 **owner 是 root**。租戶是 uid 10000，**刪不掉也覆寫不了**
那些檔案——你留下了一個租戶自己收不回的殘留，而且它就坐在你自己的 root PATH 前段。

### 原則：`/console` 不做任何安裝

- 除錯只用**映像自帶**的工具（`ls`、`cat`、`grep`、`stat`、`curl`、`git`、`python3`…），
  一律走絕對路徑。
- 需要某個工具才能診斷 → 開 issue 讓它進映像，或請租戶自己在自己那一面裝。
- 真的不得已必須在 console 裝東西（例外，需留紀錄）：裝完把 owner 交還租戶並記錄在工單裡。

```sh
# 事後清理／交還（例外情況才會用到）
/usr/bin/chown -R 10000:10000 /opt/data/.local
```

### 怎麼發現這種殘留

```sh
# canonical 目錄裡不該有 root 擁有的檔案
/usr/bin/find /opt/data/.local -maxdepth 3 ! -user 10000 -printf '%p %u:%g\n' 2>/dev/null
```

⚠ 判別力只在 **owner**。`fsGroup` 會在每次掛載時把 group 與 mode 重寫成固定值，
所以 group／mode 對這件事恆綠、驗不出東西。

---

## 3. 🔴 第四面的自動安裝在 0.2.0 之後會真的成功

`_run_setup_command`（memory provider setup，issue `#718`）是容器內第四個指令執行面。
兩個既有事實：

- 它用 `subprocess.run(..., shell=True, executable="/bin/bash")`，而且
  **沒有接 approval gate**（L6 的 `!` 面有接；這一面沒有）。
- 它的 `check_cmd` 會隨 Plugins 頁載入**自動觸發**，不需要點任何按鈕。

0.2.0 帶來的變化：這一面跑的安裝指令（例如 byterover provider 的
`npm install -g byterover-cli`）在**之前必然失敗**（預設 prefix `/usr/local` 對 uid 10000
不可寫，`EACCES` / rc=243）；**現在會成功**，落點 canonical、在 PVC 上、撐得過 pod 重建。

- 這**不是**新開的洞：同一個 uid、同一個目錄、同一條既有通道，權限沒有升級。
- 但風險形狀變了：從「跑了也裝不起來」變成「跑了會真的落地一個可執行檔到 root PATH 前段的
  目錄」。⇒ §1 的圍堵從「建議」升級成「必須」。

**排查時**：在 canonical 目錄看到你和租戶都不認得的可執行檔，先想到這一面，
不要預設是租戶手動裝的。

```sh
/usr/bin/ls -la /opt/data/.local/bin/
/usr/bin/ls -la /opt/data/.local/lib/node_modules/ 2>/dev/null
```

---

## 4. 常見診斷動作（都已改成絕對路徑形式）

### 4.1 這個部署有沒有含 #717 的修正

不進 pod、唯讀：

```sh
kubectl --context woow-k3s -n <paas-ws-*> get deploy <release>-hermes-agent \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="hermes-agent")].env[*].name}'
kubectl --context woow-k3s -n <paas-ws-*> get deploy <release>-hermes-agent \
  -o jsonpath='{.spec.template.spec.initContainers[*].name}'
```

出現 `UV_TOOL_BIN_DIR` 等落點鍵、或出現 `cli-landing-init` ⇒ 已含修正。
⛔ 不要用 chart 版號判（`appVersion` 追上游、`version` 會因無關改動 bump、真 image 釘在平台的
`helm_default_values`——三個數字都不與「有沒有這個修正」單射）。

### 4.2 `cli-landing-init` 有沒有正常做事

```sh
kubectl --context woow-k3s -n <ns> logs <pod> -c cli-landing-init
```

正常長這樣：

```
[cli-landing] canonical bin dir ready: /opt/data/.local/bin (created by uid 10000, gid 10000)
[cli-landing] compat link created: /opt/data/home/.local/bin -> /opt/data/.local/bin
```

看到 `[cli-landing][ERROR]` 表示某條相容連結的目標**已經是租戶的真目錄或指向別處**——
這是**預期行為**，不是故障：init 刻意不覆寫、不刪除租戶既有內容，並且**一律 exit 0**
（非零退出會讓既有租戶一升級就 CrashLoop）。處置＝告訴租戶把該目錄下的工具搬到
`/opt/data/.local/bin`，不要自己動租戶的檔案。

⚠ 兩條相容連結的狀態可能**不同**（一條 ERROR、一條正常建立）。看 log 要逐條看，
不要 grep 到一個 ERROR 就當成整個 init 失敗。

### 4.3 租戶說「裝了但叫不到」

先分清是**落點**問題還是 **PATH** 問題：

```sh
# 1) 檔案在不在（絕對路徑，不依賴 PATH）
/usr/bin/ls -l /opt/data/.local/bin/ /opt/data/home/.local/bin/ 2>/dev/null
# 2) 直接用絕對路徑執行，證明檔案本身沒問題
/opt/data/.local/bin/<tool> --version
```

- 檔案在 canonical、絕對路徑跑得動 ⇒ 是 PATH 問題。若租戶是在 `/chat` 面（L5）叫不到，
  那是**設計行為**：L5 的 login shell 會被 `/etc/profile` 整條換掉 `PATH`，平台不代寫租戶
  的 rc。請他照[租戶文件](../tutorials/hermes-agent-installing-cli-tools.md) §5 自己加一行。
- 檔案在 `/opt/data/home/.local/bin` 而不在 canonical ⇒ 是 0.2.0 之前裝的，請租戶搬。
- 兩處都沒有 ⇒ 才是真的沒裝成功。

### 4.4 判斷某段輸出來自哪一面

錯誤訊息形狀就分得出來：

```
bash: tea: command not found      ← bash 面（/console、/chat，或第四面）
/bin/sh: 1: tea: not found        ← dash 面 ⇒ 必為 `!` 面那條路
```

要更精確就貼這條探針（四面都能跑）：

```sh
printf 'uid=%s exe=%s VE=[%s] VEP=[%s] HRH=[%s] HOME=[%s]\n' \
  "$(id -u)" "$(readlink /proc/$$/exe)" \
  "$VIRTUAL_ENV" "$VIRTUAL_ENV_PROMPT" "$HERMES_REAL_HOME" "$HOME"
```

| 面 | `uid` | `exe` | `VIRTUAL_ENV` | `VIRTUAL_ENV_PROMPT` | `HERMES_REAL_HOME` |
|---|:---:|---|:---:|:---:|:---:|
| `/console` | **0** | bash | 空 | 空 | 空 |
| `/chat`（L5） | 10000 | bash | **空** | `hermes-agent` | **`/opt/data`** |
| `!`（L6） | 10000 | **dash** | `/opt/hermes/.venv` | `hermes-agent` | 空 |
| 第四面 | 10000 | bash | `/opt/hermes/.venv` | `hermes-agent` | 空 |

🔴 **陰性對照那一格**：`uid=0` **且** `HERMES_REAL_HOME` 非空 ⇒ **這個組合不該出現**。
看到它的結論是「探針或前提錯了」，不是「有第五面」。四欄全空且 `uid` 也讀不到 ⇒ 探針壞了
（`/proc` 不可讀），不是「這一面什麼都沒設」。

---

## 5. 升級既有租戶到 0.2.0

⛔ **不可用 `--reuse-values`**——它會丟掉 0.2.0 新增的預設值，`helm` 照樣 exit 0，
但十個 env 與 `cli-landing-init` 一個都不會出現（症狀與「根本沒升」不可區分）。

```sh
helm -n <ns> get values <release> -o yaml > /tmp/vals.yaml
helm upgrade <release> <chart> -n <ns> -f /tmp/vals.yaml
```

升完**不要只看 helm exit 0**，用 §4.1 的兩條判別法確認 env 名單與 initContainer 名單真的變了。

---

## 相關

- [四個指令執行面的完整分析](../../decisions/hermes-agent-command-execution-planes.md)
- [安裝器落點覆寫變數清單](../../decisions/hermes-agent-installer-landing-env.md)
- [租戶側：安裝 CLI 工具指南](../tutorials/hermes-agent-installing-cli-tools.md)
- [共用叢集 namespace scope 鐵律](shared-cluster-paas-scope-guard.md)

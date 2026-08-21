---
title: Hermes Agent 裡安裝自己的 CLI 工具（租戶指南）
category: guides
status: active
created: 2026-08-16
updated: 2026-08-16
related:
  - docs/decisions/hermes-agent-command-execution-planes.md
  - docs/decisions/hermes-agent-installer-landing-env.md
  - docs/guides/deployment/hermes-agent-console-support-runbook.md
---

# Hermes Agent 裡安裝自己的 CLI 工具

> 適用：Hermes Agent Cloud Service 的租戶。chart `hermes-agent` **0.2.0 起**。
> 對應 issue：`#717`。

## TL;DR

**只有一條規則：把工具裝進 `/opt/data/.local/bin`。**

平台已經幫你把 uv / pipx / npm -g / go / cargo 的預設落點都改成這個目錄，所以你用這些工具
的標準指令安裝，**不需要做任何事**就會落在對的地方。只有手動下載（`curl -o`）需要你自己
指定目的地。

```sh
uv tool install <pkg>        # → /opt/data/.local/bin/<cmd>
npm install -g <pkg>         # → /opt/data/.local/bin/<cmd>
cargo install <crate>        # → /opt/data/.local/bin/<cmd>
go install <pkg>@latest      # → /opt/data/.local/bin/<cmd>

curl -fsSL <url> -o /opt/data/.local/bin/<cmd> && chmod +x /opt/data/.local/bin/<cmd>
```

---

## 1. 為什麼需要這條規則：同一個容器裡有四個不同的「地方」

你的 Hermes Agent 容器裡有**四個互不相通的指令執行面**。它們的 `$HOME` 與 `PATH` 都不一樣，
而畫面上不會告訴你現在站在哪一面：

| 面 | 你從哪裡碰到它 | 身分 | `$HOME` | 會讀 shell rc 嗎 |
|---|---|---|---|---|
| **L3** | `/console/` 網頁終端 | **root** | `/root` | 讀 `/root/.bashrc` |
| **L5** | 在 `/chat` 請 agent 幫你執行指令 | 一般使用者（uid 10000） | `/opt/data/home` | 讀 `~/.profile` |
| **L6** | `/chat` 輸入框打 `! <指令>` | 一般使用者 | `/opt/data` | **不讀任何 rc** |
| 第四面 | Plugins 頁的 memory provider setup（自動觸發） | 一般使用者 | `/opt/data` | **不讀任何 rc** |

三個直接後果：

1. **同一句 `~/.local/bin` 在不同面指的是不同的實體目錄。** 在 `/chat` 裝的東西掉進
   `/opt/data/home/.local/bin`，在 `!` 面裝的掉進 `/opt/data/.local/bin`——彼此看不到對方。
2. **改 shell rc 救不了全部。** L6 與第四面**物理上不讀任何 rc**（它們用的是非互動 shell），
   所以「把 PATH 加進 `.bashrc`」這種常見建議對它們無效。
3. **`/usr/local/bin` 這類系統目錄不會活過重啟。** 只有 `/opt/data` 底下是你的持久磁碟；
   其他路徑都在容器映像層，pod 一重建就消失。

**平台的解法**：不去統一那四個 `$HOME`（做不到），而是把**安裝器的落點**統一——不管你站在
哪一面下指令，工具都掉進同一個持久目錄。

---

## 2. 哪裡是持久的

| 路徑 | 持久？ | 說明 |
|---|---|---|
| `/opt/data/**` | ✅ | 你的資料磁碟（PVC）。**唯一**撐得過 pod 重建的地方 |
| `/opt/data/.local/bin` | ✅ | **canonical 工具目錄**——請把 CLI 都放這裡 |
| `/usr/local/bin`、`/usr/bin`、`/bin` | ❌ | 映像層，pod 重建即失（而且一般身分也寫不進去） |
| `/root/**` | ❌ | 映像層。`/console` 的家目錄，重建即失 |
| `/tmp/**` | ❌ | 暫存 |

⚠ `/tmp/tea` 這種東西可能是**前人留下的假檔**（10 bytes、內容只有 `Not found`、卻有執行權限）。
不要把 `/tmp` 加進 `PATH`。

---

## 3. 各家工具怎麼裝

### 3.1 直接可用、零設定（平台已收斂落點）

```sh
uv tool install httpie          # Python CLI；容器內已有 uv
uv tool install pipx            # 想用 pipx 的話，先用 uv 裝出來
pipx install <pkg>              # pipx 在這裡用 uv 當後端，不需要 pip
npm install -g <pkg>            # 容器內已有 node 22 / npm 10
```

驗證落點：

```sh
ls -l /opt/data/.local/bin/
```

### 3.2 需要你先自備工具鏈的（go / cargo）

容器內**沒有** go 與 rust。你自己把工具鏈帶進來之後，`go install` 與 `cargo install`
的落點平台已經先設好了，會直接落在 canonical 目錄。

⚠ 工具鏈**本身**不受這個設定管：

- `go` 這支 binary：你解到哪就在哪 → 請解到 `/opt/data` 底下，並把它的 `bin` 自己加進 PATH。
- `rustup` 裝的 `cargo` / `rustc` **shim**：落在 `$CARGO_HOME/bin`（平台已指到
  `/opt/data/.local/share/cargo/bin`，是持久的，但**不是** canonical 目錄）。
  用 `cargo install` 裝的**工具**才會落在 canonical。

### 3.3 pip：可以用，但有前提（請先讀完）

這顆容器**沒有 pip**，而且 agent 自己那個 Python 虛擬環境**關閉了 user-site**，所以
`pip install --user` 在裡面會被直接拒絕。平台**刻意沒有**替 pip 設落點變數——那個變數
（`PYTHONUSERBASE`）會連帶改變容器內所有 Python 行程的 import 行為，副作用超出「裝個 CLI」
的範圍。

**建議**：Python CLI 一律用 `uv tool install`（或 `pipx`），那是這顆容器上最順的路。

真的需要 pip 時，自己指定落點，不要靠預設：

```sh
/usr/bin/python3 -m ensurepip --user 2>/dev/null || true   # 先把 pip 弄出來
/usr/bin/python3 -m pip install --prefix /opt/data/.local <pkg>
```

裝完確認可執行檔真的在 `/opt/data/.local/bin/`；沒有的話就手動搬過去。

### 3.4 手動下載（`curl -o`）

沒有任何旋鈕可以自動收斂，**你必須自己指定目的地**：

```sh
curl -fsSL <url> -o /opt/data/.local/bin/mytool
chmod +x /opt/data/.local/bin/mytool
```

---

## 4. 明確不支援（這是設計，不是缺陷）

| 對象 | 為什麼 |
|---|---|
| **`apt` / `apt-get`** | 系統層、需要 root、落點在映像層 ⇒ **設計上就不該持久**。需要系統套件請開 support ticket |
| **`nvm` 及同類 rc-function 版本管理器** | 它的本質是「要 shell rc 被 source」，而 L6 與第四面物理上不讀 rc。替代方案：用已收斂 prefix 的 `npm install -g` |
| **`npm install -g` 之前的舊行為** | 0.2.0 之前它會 `EACCES` 直接失敗（預設 prefix `/usr/local` 你寫不進去）。0.2.0 起可用 |

---

## 5. 讓 `/chat` 面的 `PATH` 也看到 canonical 目錄（選用，需要你自己做一次）

「工具**落在哪**」與「哪一面的 `PATH` **看得到它**」是兩件事。0.2.0 之後：

| 面 | `PATH` 看得到 `/opt/data/.local/bin` 嗎 |
|---|---|
| L3（`/console`） | ✅ 本來就在 PATH 上 |
| L6（`!` 面） | ✅ 本來就在 PATH 上 |
| 第四面 | ✅（目錄一存在就會被加進去） |
| **L5（`/chat` 請 agent 執行）** | ❌ **需要你自己加一行** |

原因：L5 走的是 login shell，系統的 `/etc/profile` 會把 `PATH` **整條換掉**，之後只有你自己的
`~/.profile` 有機會把目錄加回去。**平台不會替你寫 shell rc**——那是你的檔案，平台代寫就成了
你升級時的隱形相依。

要讓 `/chat` 面也能用短指令名叫到工具，在 `/opt/data/home/.profile` 加一行：

```sh
# 在 /console 或 ! 面執行一次即可
printf '%s\n' 'PATH="/opt/data/.local/bin:$PATH"; export PATH' >> /opt/data/home/.profile
```

平台已經在 `/opt/data/home/.local/bin` 與 `/opt/data/home/bin` 建好指向 canonical 目錄的
**符號連結**，所以如果你的 `.profile` 是 Debian 標準版本（會自動把 `$HOME/.local/bin` 前插），
上面那一行也可以不用加。

⚠ **只用 append（`>>`），不要整條覆寫 `PATH`。** 寫成 `export PATH=/my/bin` 會把 agent 自己
需要的路徑擠掉，症狀是 agent 開始出現沒有錯誤訊息的怪異失敗。

⚠ 這兩條符號連結若在你的環境**沒有**建出來（例如 `/opt/data/home/.local/bin` 早就是一個
真的目錄、裡面已經有你以前裝的東西），平台**不會覆寫也不會刪除**它——你既有的檔案是安全的。
想收斂的話，自己把裡面的東西搬到 `/opt/data/.local/bin` 即可。

---

## 6. 常見問題

**Q：我在 `/chat` 裝好了，`!` 面卻說 `not found`。**
確認落點：`! ls -l /opt/data/.local/bin/`。如果工具在 `/opt/data/home/.local/bin` 而不在
canonical 目錄，代表它是 0.2.0 之前裝的——搬過去即可。

**Q：`command -v <tool>` 找不到，但檔案明明在。**
那是 `PATH` 的問題不是安裝的問題。四個面的 `PATH` 不同（見 §5）；先用**絕對路徑**
`/opt/data/.local/bin/<tool>` 確認工具本身沒問題。

**Q：pod 重建之後工具不見了。**
它當初落在映像層而不是 `/opt/data`。`/usr/local/bin`、`/root` 都會在重建時清空。

**Q：`/bin/sh: 1: xxx: not found` 和 `bash: xxx: command not found` 有差嗎？**
有。前者來自 `!` 面（用的是 dash），後者來自 `/console` 或 `/chat` 面。這個差別可以幫你
判斷自己剛剛站在哪一面。

---

## 相關

- [四個指令執行面的完整分析](../../decisions/hermes-agent-command-execution-planes.md)
- [安裝器落點覆寫變數清單（含第一手查證）](../../decisions/hermes-agent-installer-landing-env.md)
- [support 端 runbook](../deployment/hermes-agent-console-support-runbook.md)（給平台支援工程師，不是租戶）

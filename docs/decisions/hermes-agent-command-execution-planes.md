---
name: decision-hermes-agent-command-execution-planes
status: accepted
created: 2026-08-14
updated: 2026-08-14
phase: 租戶自裝 CLI 可用性調查收斂（#717 實作前的架構事實基準）
team: repo-leader（woow-paas-platform）
deciders:
  - 使用者（拍板「rc 由租戶自理」與 L3 信任邊界圍堵方式）
  - leader4（五輪唯讀調查 ＋ 對抗式覆核收斂）
related:
  - docs/reference/features/hermes-agent-web-dist-patch-constraints.md
  - src/data/cloud_app_templates.xml
  - src/controllers/paas.py
issues:
  - "#717（實作：chart 落點覆寫 env ＋ initContainer，須與 L3 信任邊界圍堵同批發）"
  - "#718（上游 `web_server.py` 未接 approval gate — 回報上游 ＋ 內部記錄）"
---

# Decision：hermes-agent container 內有**四個互不相通的指令執行面**——工具可用性由執行面決定，rc 不是可用的槓桿

> 適用：任何要處理「租戶在 Hermes Agent 裡自裝 CLI 卻叫不到」、要動 hermes-agent chart 的
> env／initContainer、要判斷容器內某條指令是從哪一面跑出來的、或要評估 `/console/`
> 這個 root 面之信任邊界的人。

**觀測基準**：ns `paas-ws-stg-7b967520` / Cloud Service `my-stg-hermes-1` / pod
`svc-03224fdb-hermes-agent-…` / container `hermes-agent` / chart `hermes-agent-0.1.3` /
Hermes Agent v0.19.0 / 上游 image `nousresearch/hermes-agent@sha256:a2a0a2da…`。
runtime 觀測與 build-time 檢查皆於 **2026-08-13** 完成，全程唯讀（pod 內零寫入）。
另有四項 pod 內唯讀觀測（`stat` / `test -w` / `id` 與 `/proc/<daemon>/status` / daemon 執行期
`environ` 白名單 grep）由 leader4 於本檔撰寫前補齊——它們把 §8 原本三條「尚未直接觀測」
的邊界收掉了，出處見 §9。

**本檔記的是已查證的架構事實與已拍板的約束；待辦在 #717 / #718，不在此複述。**

---

## TL;DR

1. **同一個 container 裡有四個指令執行面**（L3 / L5 / L6 / 第四面），各自的 `$HOME`、PATH
   語意、shell 種類、是否讀 rc 全都不同；使用者無從得知自己站在哪一面。**同名的
   `~/.local/bin` 在不同面上指向不同的實體目錄**（inode 實測）。
2. **根因不是「rc 沒有持久化」。** rc 路線物理上到不了 L6（dash，non-interactive 不讀
   任何 rc）與第四面（`bash -c`，非 login 非互動）；三面 PATH 的交集全在 image layer，
   pod 重建即失。**持久**與**三面可見**是兩根獨立的軸，只修一軸的方案都不完整。
3. **`/opt/data/.local/bin` 是上游刻意留的空位**（五條交叉證據支持的推導；上游無明文，見
   §3）——它在 image 烘進的 PATH 上、在 PVC 上，但從沒有任何 build 或 boot 流程建立它。
4. **兩條安全事實並列**（不是一主一附）：L3 的 root PATH shadowing 通道（今日**潛伏**——
   目錄尚未建出，但**租戶對其父目錄的寫入權已實測**，任何首次建出該目錄的行動者都會把它
   轉現行）＋ 第四面的 `shell=True` **沒有接 approval gate**（L6 有）。
5. **已拍板**：rc 由租戶自理；平台可在租戶空間留「不會被執行的命名結構」，不得留「會被
   執行的內容」。apt 與 nvm 類明文不支援——這是設計決策，不是缺陷。

---

## 1. 四個指令執行面

> 以下 `/opt/hermes/…` 路徑指的是**容器內上游 hermes 的原始碼**（本 repo 不含這些檔）；
> `src/…` 才是本 repo 的相對路徑。

### 1.1 對照表（全部第一手實測）

| | **L3** console exec | **L5** agent terminal tool | **L6** TUI bang-escape | **第四面** `_run_setup_command` |
|---|---|---|---|---|
| 入口 | `/console/`（ttyd → `connect.sh` → `kubectl exec -it <daemon-pod> -c hermes-agent -- /bin/bash`） | agent（LLM）呼叫 terminal / file / code tool | TUI 內 `!cmd`，**以及訊息內插 `{!cmd}`** | dashboard API `/api/memory/providers/{name}/setup` 系列，**含被動觸發** |
| 身分 | **root** uid=0（gid 0、附加 group 10000） | hermes uid=10000 | hermes uid=10000 | 繼承 dashboard daemon ＝ hermes uid=10000 |
| `$HOME` | `/root` | `/opt/data/home` | `/opt/data` | `/opt/data` |
| 在 PVC？ | ❌ overlay，**重建即失** | ✅ | ✅ | ✅ |
| shell | non-login interactive bash | `bash -l`（做一次 snapshot） | `/bin/sh` ＝ **dash** | `bash -c`（僅 `install_cmd` 一處）／**其餘四個呼叫點連 shell 都沒有** |
| 讀 rc？ | `/etc/bash.bashrc` + `/root/.bashrc`（overlay） | **是**——`bash -l` 的原生 login 語意 source `~/.profile` | **否** | **否** |
| 有效 PATH | `/opt/hermes/bin:/opt/hermes/.venv/bin:/opt/data/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin` | `/opt/data/home/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/local/games:/usr/games` | dashboard 血統：`/opt/hermes/.venv/bin:/command:/opt/hermes/bin:/opt/hermes/.venv/bin:/opt/data/.local/bin:…`<br>CLI 血統：同 L3 那條 | `os.environ.copy()`（daemon 執行期環境）＋ 4 個 candidate 逐一 `exists()` 才前插 |
| `/opt/data/.local/bin` 在 PATH？ | ✅ | ❌ | ✅ | 名列 candidate，但**今天被 `exists()` 過濾掉** |
| `/opt/data/home/.local/bin` 在 PATH？ | ❌ | ✅ | ❌ | ❌ |

**三面 PATH 的交集只有 `/usr/local/bin`、`/usr/bin`、`/bin`——全部在 image layer。**
`/proc/mounts` 實測：整個容器只有 `/opt/data` 是 Longhorn PVC，其餘皆 overlay。

**身分與 `$HOME` 的直接證據**（leader4 補充觀測）：`id hermes` → `uid=10000(hermes)`，且
dashboard daemon 的 `/proc/<pid>/status` 的 `Uid` 也是 **10000** ⇒ **租戶面與 daemon 是同一個
uid**，不是兩個湊巧都叫 hermes 的身分。daemon 的執行期環境亦已直接讀到：
`HERMES_HOME=/opt/data`、`HOME=/opt/data`（與 s6 run script、`getent passwd hermes` 三路一致）。

第四面的四個 candidate 與現況：`/usr/local/bin` ✅ 存在（**今天唯一真被前插的一格**）、
`/opt/data/.local/bin` ❌、`.brv-cli` bin ❌、`.npm-global` bin ❌
（`/opt/data/.local` 底下今天只有 `share`、`state`）。組法定義在
`hermes_cli/web_server.py:5784-5797` 的 `_memory_provider_setup_env()`；
5 個呼叫點為 5859 / 5891 / 5925 / 5967 / 5997，其中 **唯一 `shell=True` 在 5970**
（`install_cmd`，`executable="/bin/bash"`、無 `-l`）。

**第四面是獨立的一面而非既有三面的變形**，直接證據是環境三方對比：L5 走
`_make_run_env` ＋ `bash -l` snapshot、L6 走裸繼承承載行程的 `os.environ`、第四面走
`os.environ.copy()` ＋ 條件式前插——**三種組法兩兩不同**。

### 1.2 L6 的執行主體：承載該 TUI 的 gateway 行程（三態）

`!` 與 `{!cmd}` 都是**純 RPC** `shell.exec`：node TUI 從不 fork shell（實測 node 行程
零子行程；`ui-tui/dist/entry.js` 內不存在任何 bang→`child_process` 路徑）。真正跑
`subprocess.run(shell=True)` 的是**承載該 TUI 的 gateway 行程**，共三態：

| 態 | 情境 | 承載者 |
|---|---|---|
| (a) | dashboard chat 預設 profile（本 pod 現況） | **attach 模式**——在 dashboard 行程（`hermes dashboard --host 0.0.0.0 --port 9119`）的 event loop 內。證據：node 的 `HERMES_TUI_GATEWAY_URL=ws://127.0.0.1:9119/api/ws`、PPID ＝ dashboard、零子行程、`web_server.py:18544-18560` 直接 in-process `handle_ws` |
| (b) | `/api/pty?profile=<非 current>` | node 自己 spawn `python -m tui_gateway.entry`（`web_server.py:17577-17583` 刻意不注入 attach URL） |
| (c) | CLI `hermes --tui`（含在 L3 手打） | 無 `HERMES_TUI_GATEWAY_URL` ⇒ 同樣 spawn（`ui-tui/…/gatewayClient.ts:38-42`：attach URL 唯一來源就是這個變數） |

🔴 **執行主體不是 `hermes gateway run --replace` 那支常駐 daemon。** 那支只 LISTEN
`127.0.0.1:8642`、服務 authproxy `/gateway/` 的機器對機器 API，不承載任何 TUI 的
`shell.exec`（socket inode 歸屬實測：8642 與 9119 分屬兩個不同行程）。

在 (a) 態下，**L5 與 L6 是同一顆行程的兩條 fork 分支**，不是兩套系統、不是兩個
container——差別只在「組環境變數的程式路徑」。

⚠ **引用時不要寫死 PID**：TUI 行程會換（同日兩次觀測分別是 20634 與 34318），血統結論
穩定但 PID 不穩定。

### 1.3 三個看起來像、但不是執行面的東西

| 東西 | 判定 | 依據 |
|---|---|---|
| `/api/pty` | **不是**執行面，是 L6 的發射台 | spawn 的是同一支 TUI binary（`_make_tui_argv` 三分支全回 `[node, --expose-gc, entry.js]`，node 缺失即 `sys.exit(1)`）；`pty_bridge.py:146-155` 無 `shell=True`、無 `executable`；spawn 失敗一律 `ws.close(1011)` 不 fallback |
| `/api/console`（`web_server.py:18007` → `HermesConsoleEngine`） | 第三個「打字入口」，但**不是** shell 執行面 | `console_engine.py:5` 明文 "without becoming a raw shell"；`:502-504` 遇 shell 語法直接拒絕 |
| `hermes gateway run --replace`（8642） | 常駐行程，但不承載 TUI | 見 §1.2 |

---

## 2. 為什麼會有這個問題

### 2.1 `$HOME` 有四個互不知情的權威，同一面內部也可能不一致

1. **L3**：container runtime 依 exec-uid 查容器內 `/etc/passwd` 補 HOME ⇒ `/root`。
2. **L4 / L6 / 第四面**：shell 腳本**硬編字面量** `export HOME=/opt/data`——上游 image
   內兩處（`docker/main-wrapper.sh:56`、`docker/s6-rc.d/dashboard/run`），runtime 另有
   動態生成的 `gateway-default/run` 同樣硬編。理由是 s6 的 `with-contenv` 會先把 `HOME`
   灌成 `/root`，必須在 drop privilege **之前**改回來。
3. **L5**：Python `apply_subprocess_home_env()` 依 `HERMES_HOME` **算出** `/opt/data/home`，
   完全不讀 `$HOME`（`hermes_constants.py:761-786`、`tools/environments/local.py:392-399,479-482`；
   由 `terminal.home_mode` 控制，預設 `auto`）。這是上游**刻意設計**的 per-profile
   tool-config isolation，不是意外。
4. **`/etc/passwd` 本身**：`getent passwd hermes` → `/opt/data`，與 L5 的 `$HOME` 不一致
   ⇒ 任何走 `getpwnam()`／`getpwuid()` 的解析（`~hermes` 展開、`su - hermes`、部分
   runtime 的 UserHomeDir fallback）在 **L5 面內部**就與 `$HOME` 打架。

⇒ **chart 改 `env: HOME=` 只動得到 L3**；「單一旋鈕統一 HOME」不存在。

第 2、4 條先前只有靜態旁證（s6 run script 逐字、`getent`）。daemon 的**執行期**環境已由
leader4 直接讀到——`HERMES_HOME=/opt/data`、`HOME=/opt/data`——三路一致，所以「`HERMES_HOME`
可能落 `Path.home()/".hermes"` fallback」這個分支已排除。

### 2.2 同名的 `~/.local/bin` 是兩個不同的實體目錄

- `/opt/data/.local` ＝ **inode 23**；`/opt/data/home/.local` ＝ **inode 480**。
  同 device、**不同 inode、皆非 symlink** ⇒ 兩個獨立的真實目錄，不是一個指向另一個。
- 今天 `/opt/data/home/.local/bin` 已存在（租戶裝 `tea` 當下建出的）；
  `/opt/data/.local/bin` **尚未建出**。
- 觸發個案的完整形狀：租戶把 `tea` 裝進 `/opt/data/home/.local/bin`（L5 靠 `~/.profile`
  前插看得到），而 `!tea` 走的是 L6，那條 PATH 含的是**空的** `/opt/data/.local/bin`、
  不含 tea 的實際位置 ⇒ `/bin/sh: 1: tea: not found`、`exit 127`。

### 2.3 「持久」與「三面可見」是兩根軸

只有 `/opt/data` 撐得過 pod 重建；三面 PATH 的交集卻全在 image layer。反過來，在 PVC 上
**不等於**三面看得到——L5 與 L6 互相看不到對方的落點。任何只修一軸的方案都不完整。

### 2.4 L5 的 PATH 是「清空 → 重建」，不是「多加一段」

`/etc/profile:4-9` 對非 root **無條件整條覆寫** PATH（不是 append）：

```sh
if [ "$(id -u)" -eq 0 ]; then
  PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
else
  PATH="/usr/local/bin:/usr/bin:/bin:/usr/local/games:/usr/games"
fi
export PATH
```

該檔逐位元組是 Debian 13 (trixie) 的原廠檔案（828 bytes、`/etc/profile.d/` 為空目錄），
上游與本平台都沒有改過。所以 `_make_run_env` 精心組出來的那條 PATH，在使用者的指令跑起來
**之前**就被丟掉了；`~/.profile` 是在清空**之後**才前插 `$HOME/.local/bin`。

🔴 **方法論後果**：逐欄位重演 `_make_run_env` 的解析規則必然漏掉這一步——最終決定者是
shell，不在 Python 的呼叫鏈上，組裝端 grep 遍所有 `env["PATH"]=` 也看不到它。

兩個連帶事實：

- L5 的 snapshot 由 `init_session` 那一次 `bash -l` 的 `export -p` 落地成
  `/tmp/hermes-snap-<sid>.sh`，之後每道指令 source 它；而 snapshot 在**每道指令結束後
  重新 dump**（`base.py:706`）⇒ 某次指令裡的 `export PATH=` 會外溢到同 session 的後續指令。
- L6 PATH 裡 `/command` 來自 s6-overlay——只有經過 `/init` supervision context 的行程才有；
  L3 是裸 `kubectl exec`、不經 `/init`，所以沒有。`.venv/bin` 出現兩次則是
  `main-wrapper.sh:65` 的 `. activate` 疊在已含該路徑的 base PATH 上。

---

## 3. `/opt/data/.local/bin` 是上游刻意留的空位

**PATH 那一格的來源**：上游 base image 的 `Config.Env`。上游自帶的建置定義副本
`/opt/hermes/Dockerfile:395` 寫著
`ENV PATH="/opt/hermes/bin:/opt/hermes/.venv/bin:/opt/data/.local/bin:${PATH}"`，而該行的
註解只解釋了前兩格，**完全沒有解釋 `/opt/data/.local/bin` 為什麼在這裡**。本平台的
`paas-odoo-ci` `hermes-agent/Dockerfile` 只有 FROM / LABEL / COPY / RUN 四種指令、**沒有
任何 `ENV`** ⇒ 發佈 image 的 PATH 與上游逐字元相同。

**五條交叉證據**（皆實測）：

| # | 證據 | 說明 |
|---|---|---|
| a | 全新、未經任何開機流程的上游 image 裡 `/opt/data/.local` **整個不存在** | `docker run --entrypoint sh …` 蓋掉 `/init`，看到的是原始 layer 狀態——連 `.local` 這一層都沒有 |
| b | 上游 build 只 `mkdir -p /opt/data`，沒有更深的路徑 | `/opt/hermes/Dockerfile:396`，緊接在 ENV PATH 那行之後 |
| c | 開機時**唯一**建目錄的腳本，seed 清單沒有 `.local` | `docker/stage2-hook.sh:357-372` 的 15 個 seed 目錄與 `:245` 的 chown 清單皆無；逐字讀完整個 `hermes-docker/`（entrypoint、main-wrapper、cont-init.d、s6-rc.d…）**沒有任何一處提到 `.local`** ⇒ 不是「該建的那一步漏做 mkdir」，是**根本沒有那一步** |
| d | 上游自己的工具鏈**刻意避開** `~/.local/bin` | `hermes_cli/managed_uv.py:1097-1112` 用 `UV_UNMANAGED_INSTALL` / `UV_INSTALL_DIR` 把 uv 的落點從 `~/.local/bin/` 改成 `$HERMES_HOME/bin/`（註解原文：“so the astral installer writes the binary directly into `$HERMES_HOME/bin/` instead of `~/.local/bin/`”）。另 `hermes_cli/doctor.py:1620-1629` 證明 `~/.local/bin` 在這個 codebase 裡是**有名字的慣例路徑**（host 安裝的 command-link 落點），只是容器不跑 `doctor --fix` 故不會建 |
| e | runtime 觀測吻合 | `/opt/data/.local/` 只有 `share`、`state`——這兩者對應 XDG 的 `XDG_DATA_HOME`／`XDG_STATE_HOME`，程式庫第一次寫入狀態時會自己 `mkdir -p`；`bin` 沒被建，正符合「還沒有人在這個 `$HOME` 底下跑過 `pip install --user`／`uv tool install` 這類會自動建它的安裝流程」 |

⚠ **證據邊界（不可省略）**：上游**沒有任何文件／commit message／PR 明講**「`.local/bin`
是留給使用者的」。以上是五條獨立證據交叉支持的**推導**，不是上游明文承認。要 100% 確定
得去問 NousResearch。

---

## 4. 安全事實（兩條並列，不是一主一附）

### 4.1 L3 的 root PATH shadowing 通道（今日潛伏）

- `/opt/data/.local/bin` 位於 L3（**uid=0**）PATH 的**第 3 格**，排在 `/usr/local/bin`
  （第 5）、`/usr/bin`（第 7）**之前**。
- 父目錄 `/opt/data/.local` ＝ `hermes:hermes 755`，且**以 hermes 身分實測 `test -w` 回
  WRITABLE** ⇒ 租戶確實可在其下 `mkdir`。（同批 `stat` 實測：`/opt/data` 為
  `hermes:hermes 700`、`/opt/data/plugins` 為 `hermes:hermes 755`，三者皆 `test -w` 通過。）
- ⇒ 租戶放一個叫 `ls` / `git` 的檔案，**下一位在 `/console/` 打那個指令的支援工程師就以
  uid 0 執行租戶的碼**。
- **這條通道與任何改動無關，今天就存在**（image 的 PATH 排序造成）；它處於**潛伏態**
  只是因為那個目錄尚未被建出——**不是因為租戶建不出來**。
- **第四面共用同一格**：`_memory_provider_setup_env()` 的 candidate 就是同一個實體目錄
  （inode 23）。兩面同 uid（`id hermes` 與 daemon 的 `/proc/<pid>/status` 皆為 10000，
  §1.1），故這**不構成新的權限升級**——它繼承的是同一條既有通道，不是新的洞。
- 🔴 **潛伏→現行的觸發器**：任何**首次**在 `/opt/data/.local/` 建出 `bin/` 的行動者，
  包含 initContainer、也包含第四面自己跑 `pip / uv install --user`。這是「建目錄」與
  「安全圍堵」必須同批發的原因（#717）。
- **已拍板的圍堵**：明文宣告 **L3 不是信任邊界**，support runbook 規定除錯時只用絕對路徑
  或 `PATH=/opt/data/home/.local/bin:$PATH <cmd>` 前綴（零程式改動、零爆炸半徑）。
- 連帶的 support 禁區（docker 實測）：root 帶租戶 HOME 跑 login shell **會以 uid=0 執行
  租戶的 `.profile`**（實測輸出 `[.profile EXECUTED as uid=0]`）⇒ **明文禁止**
  `bash -l`、`su - hermes`、`HOME=<租戶home> bash`。
- 一條今日不觸發的條件式同族風險：`hermes_cli/main.py:1774-1817` 的 `_ensure_tui_node`
  ——npm 一旦從 image 消失，它會把租戶可寫的 `/opt/data/node/bin` 前插進 dashboard 行程
  PATH 的**最前面**。今天 node/npm 都在，該分支早退。

### 4.2 第四執行面的 `shell=True` 沒有接 approval gate

- L6 的 `shell.exec` 在真的 `subprocess.run` 之前，**先過** `tools/approval.py` 的
  `detect_hardline_command` / `detect_dangerous_command`。
- `hermes_cli/web_server.py` **全檔沒有 import 也沒有呼叫**這組模組：
  `grep -rln "detect_hardline_command\|detect_dangerous_command" /opt/hermes --include="*.py"`
  全樹只命中 `tui_gateway/server.py`（用它）、`approvals_suggest.py`、`tools/approval.py`
  三個檔。
- ✅ **陽性對照成立**：同一支 grep 在 `tui_gateway/server.py` 拿到 3 ⇒ 那個 0 不是「探針
  沒作用」的零資訊，是真的沒接。
- **可達性**：authproxy nginx 不擋（134 行逐 block 檢視、`grep -i memory` 零命中，
  `/api/memory*` 落 catch-all 且 `auth_basic off`）；app-level middleware 三層擋的是
  **未登入者**（`/api/memory` 不在 `dashboard_auth/public_paths.py` 的 6 條白名單 ⇒ 無
  session 一律 401）⇒ **已登入的租戶自己可達**。
- **被動觸發**：`check_cmd` 隨 Plugins 頁載入自動執行（`PluginsPage.tsx:322-341` 的
  `useEffect` → … → `web_server.py:5859`），**不需點任何按鈕**；Install 按鈕只守得住
  `install_cmd`（5970 那格）。措辭邊界：8 個 bundled provider 中**只有 byterover** 宣告
  `external_dependencies`，故被動觸發只在「現行 provider ＝ byterover，或租戶自裝的
  provider 宣告了 `external_dependencies`」時才會走到 `shell=True`；`check_cmd`
  （`shell=False`）則任何 provider 皆會被動跑。
- **歸屬**：這是**上游行為**，不是本平台改動造成的——本 repo 對上游 image 的客製只有
  一處（patch `web_dist` 的 MCP OAuth 按鈕顯示條件，見
  [hermes-agent-web-dist-patch-constraints](../reference/features/hermes-agent-web-dist-patch-constraints.md)），
  未觸及 Python 端。處置見 #718。
- **結構性弱點**：`_install_memory_provider_pip_dependencies` 把缺件直接展開進 argv，無
  `-` 開頭／VCS spec／`--index-url` 白名單。**經 bundled provider 這條路今日不可利用**
  （bundled manifest 內容乾淨、`/opt/hermes/plugins/memory/*` 為 `root:root 755`、租戶無
  寫入權）。
  ⚠ 但 **user-installed provider 這條路的兩道前置關卡都已實測通過**：掃描路徑由
  `HERMES_HOME=/opt/data` 確定為 `/opt/data/plugins/<name>/`，而該目錄實測
  `hermes:hermes 755` 且 `test -w` 為 WRITABLE ⇒ **manifest 確實是租戶可控的**。
  仍未驗的只剩最後一段：user-installed 的 `pip_dependencies` 是否真的流進上述無白名單的
  argv 展開（函式端已驗、租戶輸入端已驗，**相接與否未驗**）。這一段仍是未解疑問、不是
  結論（§8-2）。

---

## 5. 已拍板的約束與界線判準

> **rc 由租戶自理，平台不代寫、不把租戶 rc 納入平台依賴。**

**界線判準**：平台可以在租戶空間留下**不會被執行的命名結構**（空目錄、指向租戶自己目錄
的 symlink）；**不得**留下**會被執行的內容**（rc、script、binary、指向平台可執行檔的
symlink）。理由：rc 是會被執行的碼，平台代寫即成平台依賴；目錄與 symlink 不把平台的碼引入
執行路徑，租戶刪掉最壞只是回到「找不到工具」。

**唯一例外**：當 symlink 讓租戶可寫的目錄出現在 **root 面 PATH 前段**時，它就從命名決策
升級成執行路徑決策，必須走 §4.1 的安全裁決。

⚠ **「rc 自理」不能是唯一機制**——rc 路線物理上到不了 L6 與第四面（都不讀 rc）。

三條相關的既有事實：

- **L5 早就在自動 source 一份租戶可寫的 rc**（`/opt/data/home/.profile`，mode 600，
  2026-08-12 才被建立）。所以「租戶 rc 會被自動執行」不是新增出來的東西；會被新增的是把它
  **擴大到 L3（root 面）**。
- ⚠ rc 只能 **append** 不能整條覆寫 PATH——snapshot 是 `export -p` 固化的，租戶寫
  `export PATH=/my/bin` 會把 `/opt/hermes/.venv/bin` 擠掉，agent 自己壞在沒有錯誤訊息的
  地方。
- **租戶目前無法自助注入 `extraEnv`**：`helm_value_specs` 是可開的 allowlist，hermes-agent
  template 現在只開 2 個 optional key（`auth.minimaxApiKey`、`auth.openrouterApiKey`；
  `src/data/cloud_app_templates.xml`），過濾在 `src/controllers/paas.py:2682`
  `_filter_allowed_helm_values`。

### 明確否決的兩條路（連理由，避免被重新提案）

**❌ 改 L3 的 HOME（chart `env: HOME=/opt/data/home`）**

1. 解不了根因——只把 3 個 HOME 併成 2 個：L4/L6 的字面量 `export HOME=/opt/data` chart
   動不到，`/etc/passwd` 也仍寫 `/opt/data`。付全額代價買部分收斂。
2. `kubectl exec -it … bash` 是**互動式 non-login bash**，會讀 `$HOME/.bashrc`——HOME 一改，
   租戶可寫的 rc 立刻進入 **root 執行路徑**；附帶把 `~/.ssh`、`~/.netrc`、
   `git config --global` 全部轉向租戶可寫區，並在租戶 home 撒 root-owned 檔案。
3. 它要解的問題有零成本替代（絕對路徑或 `PATH=… <cmd>` 前綴）。
   ⚠ **不要用「L3 現在是安全的」當理由**——§4.1 已證 L3 今天就可被 shadowing 拿到 root。
   正確說法是：L3 本來就不是信任邊界，該做的是承認並圍堵，不是再開第二個洞。

**❌ 投設計成本讓 L6 變好用**

L6 的定位是**受限逃生口**，文件講清楚即可。它繼承承載 gateway 行程的
`VIRTUAL_ENV=/opt/hermes/.venv`（L5 有 pop 守護，L6 完全繞過），而該 venv 是
`root:root` 唯讀 ⇒ 在 L6 跑 pip / uv pip 必然 EACCES。

---

## 6. 不支援的邊界（設計決策，不是缺陷）

| 對象 | 判定與理由 |
|---|---|
| **apt** | 明文不支援。系統層、root-only（`dpkg`／apt 快取皆 root-only，實測 L5/L6 物理裝不成），落點在 `/usr/bin`、`/var/lib/dpkg` 等 image layer ⇒ **設計上就不該持久**。需要系統套件請開 issue |
| **nvm 及同類 rc-function 版本管理器** | 明文不支援。本質要 rc 被 source，而 L6／第四面物理上不讀 rc；替代方案＝走已收斂 prefix 的 `npm i -g`。（⚠「nvm 預設寫 `.bashrc`、而 L5 讀 `.profile`」是文件層級推論，未實測——見 §8-6） |
| **`curl -o` 手動下載** | 無旋鈕可自動收斂，租戶需遵守「裝到 canonical 目錄」這**一條**規則——但已從「每個面每個工具各一條規則」降為唯一一條、且在任何面都成立 |
| **`npm install -g` 在 L5/L6** | 預設 global prefix `/usr/local` 對 uid 10000 不可寫（`root:root drwxr-xr-x`，`test -w` 實測）⇒ EACCES |
| **pip / pipx** | 容器內**不存在**（連 `.venv` 的 python 都 `No module named pip`）；`ensurepip` 模組在，故 pip 可 bootstrap。`uv` / `uvx` 在 `/usr/local/bin` |

---

## 7. 可複用的判別式

### 7.1 錯誤訊息形狀（不需任何探針）

```
bash: tea: command not found      ← bash 面
/bin/sh: 1: tea: not found        ← dash 面
```

- **邊界一**：只能區分 bash vs dash，**反向不成立**——bash 格式可能是 L3、L5，或第四面的
  `install_cmd`。
- **邊界二**：dash 格式 ⇒ **必為 `shell.exec` 那條路**，而它有**兩個入口**——`!` 前綴，
  以及訊息內插 `{!cmd}`（不需 `!` 開頭）。已知 `{!}` 失敗時**不印** `exit N`，但**它是否也
  印出這一行 stderr 尚未確認**；在確認之前，不要把「看到 dash 格式」直接等同於「使用者
  打了 `!`」。

### 7.2 MARKERS 三元組（判斷站在哪一面）

| `VIRTUAL_ENV` | `VIRTUAL_ENV_PROMPT` | `HERMES_REAL_HOME` | 判定 |
|:---:|:---:|:---:|---|
| ❌ | ✅ | ✅ | **L5**（terminal tool） |
| ✅ | ✅ | ❌ | **L6**（gateway 血統，未經 `_make_run_env` 的 pop） |
| ❌ | ❌ | ❌ | **L3** 血統 |

依據：`tools/environments/local.py:344`
`_ACTIVE_VENV_MARKER_VARS = ("VIRTUAL_ENV","CONDA_PREFIX")` ＋ `local.py:1195`
`run_env.pop(_marker)`——**只 pop `VIRTUAL_ENV`，`VIRTUAL_ENV_PROMPT` 不在清單裡會留下**；
`HERMES_REAL_HOME` 只由 `hermes_constants.py:797-801` 的 `apply_subprocess_home_env` 寫入。
這個不對稱是乾淨的分隔線。

⛔ **不要用 PATH 判血統**（不管是「有沒有 `/command`」還是尾端指紋）——L5 的 PATH 在指令
跑起來之前就被 `/etc/profile` 換掉了，會給假陰性。PATH 只能用來看 L5 的 snapshot 健不健康
（出現 `/command` 或 `/opt/homebrew` ⇒ snapshot 走了 degraded 分支）。

### 7.3 觀測陷阱（都撞過，且症狀與「真的沒問題」不可區分）

- 🔴 **「environ 相同」永遠不可當血統證據。** 三個行程的 PATH/HOME 值相同，是「都繼承自
  同一套 s6/image ENV」的產物——對「A 是否 fork 自 B」**零鑑別力**（兩個假說都預測得出
  一樣的觀測）。而且「逐字相同」本身還是錯的：key 計數實測 68 / 69 / 120，只有 PATH 與
  HOME 的**值**相同。真正能分辨的是**控制流**（`resolveGatewayAttachUrl()` 只讀
  `HERMES_TUI_GATEWAY_URL` 一個來源，有→attach、無→spawn）與 **socket inode 歸屬**。
- **`/proc/<pid>/environ` 在容器內即使 root 也讀不到**（缺 `CAP_SYS_PTRACE`）；空輸出
  ＝**探針壞了**，不是「這個面沒設這些變數」。可用 `su hermes -c '…'`（**不帶** `-`，
  非 login shell，不觸發讀 profile）借同 uid 的讀取權限；界線是——`su hermes -c 'echo $PATH'`
  就變成回顯自己的輸入，沒有鑑別力。
  🔴 **本案就吃過這個虧**：五條調查線全用 root 讀而撞 `Permission denied`，於是把
  `HERMES_HOME` 的實際值當成「查不到的事實」寫進殘留未知；換成 `su hermes -c` 之後一次
  就讀到（`HERMES_HOME=/opt/data`）。**探針權限不足留下的空白，會被寫成「事實未知」而不是
  「探針該換」——後者才是真相。**
- `tr "\0" "\n" < /proc/self/environ` 會**靜默回 0 bytes**，`cat … | tr` 回正常內容（同一個
  sh、可重現，機制未查明）⇒ 讀 environ 一律用 `cat … | tr` 形式。
- **整包 dump environ 會外洩機密**：daemon environ 內含 `API_SERVER_KEY`、
  `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD` / `_SECRET`，dump 會把它們印進對話並落地到 PVC 上
  的 `state.db`（console 任何 session 都讀得到）⇒ 一律用白名單 `grep`。
- **mtime 不可當 src/dist 同版證據**（dist 早於 src，是 layer copy 時序造成的）。
- **PID 不穩定**；TUI 畫面左側顯示的 `/opt/data` 是 `HERMES_CWD`／`HERMES_HOME`，**不是**
  行程的實際 cwd；畫面上的 `Session: <id>` 與行程的 `HERMES_SESSION_ID` 是兩個層級的 ID，
  對不起來。
- `/tmp/tea` 是**假的**（10 bytes、內容只有 `Not found`、卻掛著 `+x`）——任何把 `/tmp` 帶進
  PATH 的面會撞到它，得到「執行成功但什麼都沒做」。目前三面 PATH 都不含 `/tmp`。

---

## 8. 本檔的證據邊界（尚未直接觀測的項目，明列以免被當成已驗）

1. **`_run_setup_command` 另外三個呼叫點（5891 / 5925 / 5997）的觸發條件無人回報** ⇒
   被動觸發面可能不只 5859。
2. **user-installed provider 的 `pip_dependencies` 是否流進 §4.2 那條無白名單 argv 展開**
   ——函式端已驗、租戶輸入端已驗（掃描落點與可寫性都已實測，見 §4.2），**只剩相接與否
   未驗**。這是疑問，不是主張。
3. **Cloudflare tunnel / ingress 外層**是否對 `/api/memory*` 另有限制——pod 內查不到，
   需叢集側確認。
4. **上游對 `.local/bin` 留白的「意圖」沒有明文**（§3 的證據邊界）。
5. **`tools/approval.py` 是否涵蓋 L5 的 session-bootstrap 靜默 source 路徑，未追。**
   ⚠ 提問方式要先修正：`_prepend_shell_init` 在本 pod 是**死碼**（只在 `login=True` 跑，
   snapshot 健康時該值恆 False），真正 source `.profile` 的是 `bash -l` 的原生 login 語意。
6. **nvm 實際寫哪個 rc 是文件層級推論**，未實測。

**已從本節移除（原 1–3 項，現皆為實測）**：`HERMES_HOME` 的實際值（＝`/opt/data`）、
`hermes` 的數值 uid（＝10000，且與 daemon 同 uid）、`/opt/data`／`/opt/data/.local`／
`/opt/data/plugins` 三者的 owner・mode 與 `test -w` 可寫性。出處見 §9；促成它們被讀到的
探針換法見 §7.3 第 2 條。

---

## 9. 材料出處

本檔由六份唯讀分析＋使用者第一手實證收斂而成（皆為 2026-08-13、leader4 的 scratchpad
產物，**不入版控**；權威順序為後輪覆蓋前輪）：

| 輪次 | 檔名 | 承擔的部分 |
|---|---|---|
| 1 | `console-shellrc-persistence-analysis.md` | 問題框架、L3 落地身分、觸發個案根因 |
| 2 | `console-shellrc-L5-addendum.md` | L3/L5/L6 的 PATH/HOME 機制（§2.4 的 `/etc/profile` 覆寫） |
| 3 | `console-generic-cli-availability.md` | 安裝器落點矩陣、四權威 HOME、安全發現、不支援邊界 |
| 4 | `console-execution-plane-lineage.md` | 執行面血統（§1.2）、`{!cmd}` 第二入口、方法論警告 |
| 5 | `console-run-setup-command-fourth-plane.md` | 第四執行面確認、inode 判別式、approval gate 缺口 |
| — | `hermes-image-build-investigation.md` | build-time 側：PATH 來源、§3 的留白證據鏈、`/etc/profile` 原廠性 |
| — | maestri note `hermes-exec-planes-evidence` | 使用者第一手三面實測與 §7.1 的判別式邊界 |
| — | leader4 的 pod 內唯讀補充觀測 | `stat -c "%n %U:%G %a"` 三目錄、`su hermes -c 'test -w …'` 三目錄、`id hermes` ＋ daemon `/proc/<pid>/status` 的 `Uid`、`su hermes -c 'cat /proc/<pid>/environ \| tr …'` 白名單 grep ⇒ 收掉 §8 原 1–3 項 |

> 這些材料是**五輪演進**，早期結論已被後期推翻（例如第一輪的「把 tea 搬到
> `/opt/data/.local/bin`」已撤回、第二輪的「`/proc/…/environ` 與 daemon 逐字相同」與
> 第四輪的「L6 執行主體是某支常駐 gateway daemon」已推翻）。**本檔只收錄收斂後的結論**；
> 被撤回的建議與逐輪推翻過程刻意不寫進來。

**相關 issue**：

- [#717](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/717) — 實作單：
  chart 落點覆寫 env ＋ initContainer，必須與 §4.1 的信任邊界圍堵**同一次 release**。
- [#718](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/718) — §4.2 的
  上游回報與內部風險記錄。

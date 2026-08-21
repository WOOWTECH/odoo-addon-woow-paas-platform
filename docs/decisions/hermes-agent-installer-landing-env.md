---
name: decision-hermes-agent-installer-landing-env
status: accepted
created: 2026-08-16
updated: 2026-08-16
phase: "#717 實作（chart hermes-agent 0.2.0）"
team: leader3（woow-paas-platform repo agent team）
deciders:
  - 使用者（2026-08-16 核准 #717 開工與 stg 驗收機）
  - leader3（D1 pip 家族不設變數／D2 Tier A+B 全收／D3 本檔落點與時機）
related:
  - docs/decisions/hermes-agent-command-execution-planes.md
  - docs/guides/tutorials/hermes-agent-installing-cli-tools.md
  - docs/guides/deployment/hermes-agent-console-support-runbook.md
  - src/data/cloud_app_templates.xml
issues:
  - "#717（chart 落點覆寫 env ＋ initContainer，須與 L3 信任邊界圍堵同批發）"
  - "#718（上游 `web_server.py` 未接 approval gate）"
---

# Decision：hermes-agent chart 注入哪些安裝器落點覆寫 env（六家族逐一，附第一手查證）

> **本檔是 #717 issue 內文「具體變數清單與值見支撐 doc」所指的那份 doc。**
> 適用：要改 `woow-paas-charts` 的 `charts/hermes-agent` env 區塊、要判斷某個執行中的
> 部署含不含這個修正、或要回答「租戶用某個安裝器裝的東西會掉到哪」的人。
>
> 前置事實（本檔**不複述**）：四個執行面的血統、canonical 目錄為何是
> `/opt/data/.local/bin`、L3 不是信任邊界 —— 見
> [hermes-agent-command-execution-planes](hermes-agent-command-execution-planes.md)。

---

## 0. 結論表（chart 端直接對應 `cliLanding.*`）

**canonical 目錄 ＝ `/opt/data/.local/bin`；canonical 前綴 ＝ `/opt/data/.local`。**

### Tier A — 落點收斂（必要，缺一即該家族不收斂）

| # | 變數 | 值 | 家族 | 決定什麼 |
|---|---|---|---|---|
| A1 | `UV_TOOL_BIN_DIR` | `/opt/data/.local/bin` | uv | `uv tool install` 放**可執行檔**的目錄 |
| A2 | `UV_PYTHON_BIN_DIR` | `/opt/data/.local/bin` | uv | `uv python install` 放 `python3.x` 可執行檔的目錄 |
| A3 | `PIPX_BIN_DIR` | `/opt/data/.local/bin` | pipx | pipx 放 app 可執行檔的目錄 |
| A4 | `npm_config_prefix` | `/opt/data/.local` | npm -g | `npm i -g` 的 prefix；binary 落 `<prefix>/bin`、模組落 `<prefix>/lib/node_modules` |
| A5 | `GOBIN` | `/opt/data/.local/bin` | go | `go install` 放可執行檔的目錄 |
| A6 | `CARGO_INSTALL_ROOT` | `/opt/data/.local` | cargo | `cargo install` 的 installation root；可執行檔落 `<root>/bin` |

### Tier B — 跨面狀態一致（一併採用）

| # | 變數 | 值 | 不設的後果 |
|---|---|---|---|
| B1 | `UV_TOOL_DIR` | `/opt/data/.local/share/uv/tools` | **已實測**：只設 A1 時 tool venv 仍落 `$HOME/.local/share/uv/tools` ⇒ 同一個 tool 在 L5 `uv tool list` 看得到、在 L6 回 `No tools installed`；從另一面重裝會撞 `error: Executable already exists` |
| B2 | `PIPX_HOME` | `/opt/data/.local/share/pipx` | 同上（pipx venv / log / trash 全部跟著 `$HOME` 走） |
| B3 | `GOPATH` | `/opt/data/.local/share/go` | module cache 依面分裂（重複下載、佔兩份空間）。**不影響正確性** |
| B4 | `CARGO_HOME` | `/opt/data/.local/share/cargo` | registry cache 依面分裂；另外 rustup 的 shim 落點是 `$CARGO_HOME/bin`，不設就依面分裂。**不影響 `cargo install` 落點**（A6 優先序更高） |

### 明確不設（列出來是為了擋掉下一輪重新提案）

| 不設 | 理由 |
|---|---|
| `PATH` | K8s 的 `$(VAR)` 展開看不到 image `Config.Env`，注入 `PATH=$(PATH):…` 會拿到未展開字面值而 CrashLoop |
| `XDG_*`（`XDG_BIN_HOME` / `XDG_DATA_HOME` …） | 廣義變數會把 daemon 自己的狀態目錄一起搬走。**且不需要**——A1/A2 對 uv 的優先序高於 `XDG_BIN_HOME` |
| `PYTHONUSERBASE`／`PIP_*`／`PIP_USER` | 見 §3.2 與 §5。**這是對 issue「六家族」字面的 deviation，已由 leader3 於 2026-08-16 裁決採用** |

---

## 1. 查證基準與探針健康度

### 1.1 材料

| 材料 | 取得方式 | 版本 / digest |
|---|---|---|
| 上游 image | 本機 `docker`（本輪複驗 digest） | `nousresearch/hermes-agent@sha256:a2a0a2da2ce482ec0f67561d9b894a711086b15ad5d4b85ea073421b66a7d3b8`，`Architecture=amd64`、`Created=2026-07-27T03:59:33Z` |
| `managed_uv.py` | `docker cp`（唯讀） | 同上 image，41,444 B |
| go 對照 | `golang:1.25-alpine`（官方 image，`--platform linux/amd64`） | `go1.25.13 linux/amd64` |
| cargo 對照 | `rust:1-alpine`（同上） | `cargo 1.97.1 (c980f4866 2026-06-30)`、`rustup 1.29.0` |

**執行方式**：全部是 `docker run --rm` 的即拋容器，主機端零寫入；未觸碰任何叢集。

### 1.2 image 內六家族的存在性（第一手，逐字）

```
uv       /usr/local/bin/uv           uv 0.11.6 (x86_64-unknown-linux-musl)
node     /usr/local/bin/node         v22.22.3
npm      /usr/local/bin/npm          10.9.8       npm config get prefix → /usr/local
python3  /opt/hermes/.venv/bin/python3   Python 3.13.5
pip      MISSING       pip3   MISSING       pipx   MISSING
go       MISSING       cargo  MISSING       rustup MISSING
```

補充（決定 pip 家族邊界用）：

```
venv python:      ENABLE_USER_SITE = False   prefix=/opt/hermes/.venv  base_prefix=/usr
/usr/bin/python3: ENABLE_USER_SITE = True    （3.13.5）
ensurepip 兩邊都在：25.1.1
```

⇒ **image 內今天實際跑得起來的只有 uv 與 npm 兩個家族**；pipx 可用 `uv tool install pipx`
裝出來（實測成功，見 §3.3）；go / cargo 要租戶自己帶工具鏈進來。A5 / A6 / B3 / B4 屬於
**預先上膛**：現在設了沒有作用對象，租戶哪天把工具鏈帶進來就直接收斂。

### 1.3 兩次探針故障（記錄下來，避免下一位照抄壞探針）

| 探針 | 症狀 | 真相 | 改法 |
|---|---|---|---|
| `strings /usr/local/bin/uv \| grep -x -E 'UV_TOOL_BIN_DIR\|…'` | **零命中** | 不是「這版不支援」。`strings` 存在（`/usr/bin/strings`），但 Rust 二進位的字串表是**接在一起的**，`grep -x`（整行相等）永遠不會命中 | 改 `grep -a -q "<name>" <binary>`，並**加一個必定不存在的名字當陰性對照** |
| `npm install -g … \| tail -4; echo rc=$?` | `rc=0`，看起來成功 | `$?` 取到的是 `tail` 的退出碼，不是 npm 的；`--silent` 又把錯誤吞掉 | 先 `> /tmp/out 2>&1; rc=$?` 再看檔案。改法後真值是 **`rc=243` / `EACCES`** |

第二個尤其該記：它會讓「今日 `npm i -g` 在 L5/L6 會 EACCES」這條既有事實被誤判為已修好。

---

## 2. 四個面都收得到嗎？（前提複驗，不是引述）

`#717` 主張「落點覆寫變數不經過 `$HOME`，從 container spec 注入後四個面全部到達」。本檔複驗了
其中**兩條會靜默吃掉變數的關卡**：

**(a) s6 `with-contenv` 這一關**——container spec env 有沒有真的傳到服務層：

```sh
docker run --rm -e WO717_PROBE=canonical-test -e UV_TOOL_BIN_DIR=/opt/data/.local/bin \
  --entrypoint /init <image> /command/with-contenv sh -c '…'
→ WO717_PROBE=[canonical-test] UV_TOOL_BIN_DIR=[/opt/data/.local/bin] HOME=[/root]
→ NOT_INJECTED=[]        ← 陰性對照：沒注入的變數是空的（證明上一行不是回顯我自己的輸入）
```

（`HOME=/root` 是 `/init` context 的值，`dashboard/run:22` 之後會 `export HOME=/opt/data` 蓋掉。）

**(b) L5 的 `_make_run_env` blocklist 這一關**——用 image 內的 python 直接做成員測試：

```
blocklist size = 318
UV_TOOL_BIN_DIR / UV_TOOL_DIR / UV_PYTHON_BIN_DIR / PIPX_BIN_DIR / PIPX_HOME /
npm_config_prefix / NPM_CONFIG_PREFIX / GOBIN / GOPATH / CARGO_INSTALL_ROOT /
CARGO_HOME / PYTHONUSERBASE            → 全部 blocked=False
OPENROUTER_API_KEY / MINIMAX_API_KEY   → blocked=True   ← 陽性對照
```

`_is_hermes_internal_secret()` 對上列 12 個候選也全部回 `False`。`_make_run_env`
（`tools/environments/local.py:1151-1202`）逐字讀過，**沒有任何前綴式過濾**會掃到
`UV_*` / `PIPX_*` / `npm_config_*` / `GO*` / `CARGO_*`。

⚠ **本檔沒有驗到的那一格**：L3（`kubectl exec`）繼承 pod spec env——這是 CRI 通用行為，
未在 K8s 上實證（execution-planes ADR §6-1 也把它列為待實測）。→ stg 驗收會直接證到。

---

## 3. 六家族逐一

### 3.1 uv（image 內版本 0.11.6）

| 變數 | 值 | 誰在哪一步讀 |
|---|---|---|
| `UV_TOOL_BIN_DIR` | `/opt/data/.local/bin` | `uv tool install` 建 entry point 連結那一步；預設 ＝ `XDG_BIN_HOME` → `$HOME/.local/bin` |
| `UV_PYTHON_BIN_DIR` | `/opt/data/.local/bin` | `uv python install` 建 `python3.x` 可執行檔那一步（`--no-bin` 才會關掉 ⇒ **建 bin 是預設行為**） |
| `UV_TOOL_DIR`（Tier B） | `/opt/data/.local/share/uv/tools` | tool venv 的位置；`uv tool list/upgrade/uninstall` 也讀它 |

**查證 1 — 變數存在性（含陰性對照）**：`grep -a` 對 `/usr/local/bin/uv` 二進位

```
UV_TOOL_BIN_DIR: PRESENT      UV_TOOL_DIR: PRESENT      UV_PYTHON_BIN_DIR: PRESENT
XDG_BIN_HOME:    PRESENT
UV_INSTALL_DIR:  absent       UV_UNMANAGED_INSTALL: absent      ← 見 §4.1，重要
UV_THIS_STRING_SHOULD_NOT_EXIST: absent                          ← 陰性對照
```

**查證 2 — 鑑別式（不是回顯自己的輸入）**：讓 uv 自己算落點，**把 `HOME` 換成 L5 的值**：

```
HOME=/opt/data       uv tool dir --bin   → /opt/data/.local/bin          （預設就已 canonical）
HOME=/opt/data/home  uv tool dir --bin   → /opt/data/home/.local/bin     （L5 預設，分歧點）
HOME=/opt/data/home  UV_TOOL_BIN_DIR=…   → /opt/data/.local/bin          ✅ 覆寫贏

HOME=/opt/data/home  uv python dir --bin → /opt/data/home/.local/bin
HOME=/opt/data/home  UV_PYTHON_BIN_DIR=… → /opt/data/.local/bin          ✅ 覆寫贏
```

**查證 3 — 端到端真裝一個（最強證據）**：uid 10000、`HOME=/opt/data/home`（L5 的 HOME）、
canonical 目錄**刻意事先不存在**：

```
$ ls -d /opt/data/.local/bin
ls: cannot access '/opt/data/.local/bin': No such file or directory

$ uv tool install cowsay            # UV_TOOL_BIN_DIR / UV_TOOL_DIR 已設
 + cowsay==6.1
Installed 1 executable: cowsay

$ ls -l /opt/data/.local/bin/
lrwxrwxrwx 1 hermes hermes 49 … cowsay -> /opt/data/.local/share/uv/tools/cowsay/bin/cowsay

$ ls -la /opt/data/home/.local/bin
ls: cannot access '/opt/data/home/.local/bin': No such file or directory     ← 沒有污染 L5 HOME
```

⇒ 三件事同時成立：落點 canonical、**uv 自己會 `mkdir -p` 出 bin 目錄**、L5 的 HOME 沒被碰。

**查證 4 — Tier B 為什麼不是可有可無**（只設 `UV_TOOL_BIN_DIR`、不設 `UV_TOOL_DIR`）：

```
$ ls -l /opt/data/.local/bin/cowsay
… cowsay -> /opt/data/home/.local/share/uv/tools/cowsay/bin/cowsay    ← venv 跑到 L5 HOME 底下

$ HOME=/opt/data/home uv tool list   → cowsay v6.1 - cowsay
$ HOME=/opt/data      uv tool list   → No tools installed              ← 同一顆 pod、同一個 tool
```

可執行檔照樣叫得到（symlink 也在 PVC 上），但**管理面依執行面分裂**。實際撞到的症狀是從另一面
重裝時的 `error: Executable already exists: cowsay (use --force to overwrite)`——這行是本輪測試
真的撞到的，不是推測。

### 3.2 pip（**本家族不設變數**，見 §5 D1）

| 事實 | 第一手證據 |
|---|---|
| 容器內沒有 pip | `command -v pip` / `pip3` 皆 MISSING（§1.2） |
| venv 拒絕 `--user` | `/opt/hermes/.venv/bin/python -c "import site; site.ENABLE_USER_SITE"` → **False** |
| 但 `/usr/bin/python3` 的 user-site 是開的 | `/usr/bin/python3` → `ENABLE_USER_SITE = True`；`ensurepip 25.1.1` 兩邊都在 |

⇒ **實效邊界**：租戶要用 pip，必須（a）用 `/usr/bin/python3 -m ensurepip` 之類的方式 bootstrap
出 pip，**且**（b）不在 `/opt/hermes/.venv` 裡跑。走到這一步之後，唯一能收斂落點的旋鈕是
`PYTHONUSERBASE`（`site.getuserbase()` 的來源，scripts 落 `<userbase>/bin`）。

**本檔決定不設 `PYTHONUSERBASE`**，兩條理由都是第一手：

1. **上游把它列在「會改變下一個 subprocess 怎麼執行」的 denylist 裡**，與 `LD_PRELOAD`、
   `PYTHONPATH` 同級。`/opt/hermes/hermes_cli/config.py:169` 的註解逐字：

   > `PYTHONPATH` / `PYTHONHOME` / `PYTHONSTARTUP` / `PYTHONUSERBASE` — Python interpreter
   > init. Hermes itself starts from one of these on every restart.

   清單本體在 `config.py:204`（`_ENV_VAR_NAME_DENYLIST`）。該 denylist 擋的是 dashboard 寫入
   `.env` 的路徑，不是 process env；但它對這個變數的**風險分類**是上游自己下的。

2. 🔴 **它會開一條繞得過既有圍堵的 root 通道。** 已拍板的 L3 圍堵是「除錯只用絕對路徑或
   `PATH=… <cmd>` 前綴」——那條規則防的是 PATH shadowing。但 `PYTHONUSERBASE=/opt/data/.local`
   會讓 **`/usr/bin/python3`（絕對路徑！）** 把 `/opt/data/.local/lib/python3.13/site-packages`
   加進 `sys.path`，而那是租戶可寫目錄 ⇒ 支援工程師在 `/console` 用**絕對路徑**跑任何 python，
   都會 import 租戶的碼（含 `sitecustomize`）。**用絕對路徑防不了它**，圍堵規則要改寫才蓋得住。

   對照：`PATH` 已因「不可用 chart env 設」被否決；`PYTHONUSERBASE` 的爆炸半徑與它同級。

⇒ pip 家族在文件層面用**唯一那條規則**收（「裝到 canonical 目錄」＝
`pip install --prefix /opt/data/.local`，或裝完把 binary 放進 canonical），不進 chart env。
租戶側說明見[租戶文件](../guides/tutorials/hermes-agent-installing-cli-tools.md)。

### 3.3 pipx（用 `uv tool install pipx` 裝出 1.16.7 實測）

| 變數 | 值 | 誰在哪一步讀 |
|---|---|---|
| `PIPX_BIN_DIR` | `/opt/data/.local/bin` | pipx 建 app entry point 連結那一步 |
| `PIPX_HOME`（Tier B） | `/opt/data/.local/share/pipx` | venvs / shared libs / logs / trash 的根 |

**查證（pipx 自己印的 derived values，鑑別式：兩組只差覆寫變數）**：

```
預設（HOME=/opt/data/home）                    覆寫後
PIPX_HOME=/opt/data/home/.local/share/pipx     PIPX_HOME=/opt/data/.local/share/pipx
PIPX_BIN_DIR=/opt/data/home/.local/bin         PIPX_BIN_DIR=/opt/data/.local/bin       ✅
PIPX_LOCAL_VENVS=/opt/data/home/.local/…       PIPX_LOCAL_VENVS=/opt/data/.local/share/pipx/venvs
```

兩個附帶事實：

- `PIPX_MAN_DIR` / `PIPX_COMPLETION_DIR` 仍跟 `$HOME` 走。**刻意不收**——man page 與 completion
  不是可執行檔，收它們只是多兩個變數。
- pipx 1.16.7 在這顆 image 上 `PIPX_RESOLVED_BACKEND=uv`、`PIPX_UV_BINARY=/usr/local/bin/uv`
  ⇒ **沒有 pip 也能用**。所以 pipx 這一列不是空談，是租戶今天就走得到的路。

### 3.4 npm -g（image 內 npm 10.9.8）

| 變數 | 值 | 誰在哪一步讀 |
|---|---|---|
| `npm_config_prefix` | `/opt/data/.local` | npm 讀 config 時（`npm_config_<key>` 是 npm 的 env config 慣例）；`npm i -g` 用它決定 `<prefix>/bin` 與 `<prefix>/lib/node_modules` |

**查證 1 — 大小寫兩種形式與 CLI 優先序**：

```
default                            → /usr/local
npm_config_prefix=/opt/data/.local → /opt/data/.local      ✅
NPM_CONFIG_PREFIX=/opt/data/.local → /opt/data/.local      ✅（兩種都認）
env + CLI --prefix=/tmp/cli-wins   → /tmp/cli-wins         ← CLI 贏過 env（§4.2 要用到）
HOME 換成 /opt/data/home 後：default 仍 /usr/local、env 仍 /opt/data/.local
```

⇒ npm 的預設 prefix **不隨 `$HOME` 變**（跟著 node 安裝位置），所以 npm 的病不是「四個面分裂」，
是「唯一那個 prefix 對 uid 10000 不可寫」。

**查證 2 — 端到端（負向對照 → 正向）**，uid 10000、`HOME=/opt/data/home`：

```
# 今日行為（無覆寫）
$ npm install -g --no-fund --no-audit cowsay   → rc=243
npm error code EACCES
npm error syscall mkdir
npm error path /usr/local/lib/node_modules/cowsay
npm error Error: EACCES: permission denied, mkdir '/usr/local/lib/node_modules/cowsay'

# 加上覆寫
$ npm_config_prefix=/opt/data/.local npm install -g … cowsay   → rc=0  (added 41 packages in 2s)
$ ls -l /opt/data/.local/bin/
cowsay    -> ../lib/node_modules/cowsay/cli.js
cowthink  -> ../lib/node_modules/cowsay/cli.js
```

⇒ 這一格是**六家族裡效果最大的一個**：從「物理上裝不成」變成「裝得成且落點 canonical
且在 PVC 上」。

**採用小寫 `npm_config_prefix` 的理由**：判別法 A（§6）用這個字面當「已含修正」的判準，
用小寫可以讓驗收指令逐字沿用。大寫也有效，但沒有理由製造兩個字面。

### 3.5 go（image 內無 go；用官方 `golang:1.25-alpine` 驗機制）

| 變數 | 值 | 誰在哪一步讀 |
|---|---|---|
| `GOBIN` | `/opt/data/.local/bin` | `go install` 決定可執行檔落點 |
| `GOPATH`（Tier B） | `/opt/data/.local/share/go` | module cache（`$GOPATH/pkg/mod`）與 `GOBIN` 未設時的 fallback |

**查證 1 — 安裝器自己的說明（來自 binary 而非網頁）**：`go help install`

> Executables are installed in the directory named by the **GOBIN** environment variable,
> which defaults to `$GOPATH/bin` or `$HOME/go/bin` if the GOPATH environment variable is not set.

⇒ 預設鏈就是 `$HOME` ⇒ 不覆寫必定依面分裂（L5 `/opt/data/home/go/bin`、L6 `/opt/data/go/bin`）。

**查證 2 — 鑑別式**：

```
default   GOBIN=[]                      GOPATH=[/go]
override  GOBIN=[/opt/data/.local/bin]  GOPATH=[/opt/data/.local/share/go]
```

⚠ **邊界**：go 工具鏈本身（`go` 這支 binary）不由 `GOBIN` 決定落點——租戶自己解 tarball 放哪
就在哪。那由「唯一一條規則：裝到 canonical 目錄」承接，不是變數能解的。

### 3.6 cargo（image 內無 cargo；用官方 `rust:1-alpine` 驗機制）

| 變數 | 值 | 誰在哪一步讀 |
|---|---|---|
| `CARGO_INSTALL_ROOT` | `/opt/data/.local` | `cargo install` 決定 installation root，可執行檔落 `<root>/bin` |
| `CARGO_HOME`（Tier B） | `/opt/data/.local/share/cargo` | registry cache、config 探索起點；也是 rustup shim 的落點 |

**查證 — `cargo help install`（cargo 1.97.1 自己的 man，逐字）**：

> all executables are installed into the installation root's **bin** folder.
>
> The installation root is determined, in order of precedence:
> `--root` option → **`CARGO_INSTALL_ROOT` environment variable** → `install.root` Cargo
> config value → `CARGO_HOME` environment variable → `$HOME/.cargo`

⇒ 兩件事同時得證：(1) `CARGO_INSTALL_ROOT` 是**正確的那個旋鈕**；(2) 它的優先序**高於**
`CARGO_HOME` ⇒ Tier B 的 B4 不會把 A6 蓋掉。

⚠ **邊界（與 nvm 同類，已寫進租戶文件）**：rustup 裝的 `cargo` / `rustc` **shim** 落點是
`$CARGO_HOME/bin`，不受 `CARGO_INSTALL_ROOT` 管（官方 rust image 的實際佈局即
`CARGO_HOME=/usr/local/cargo` → `/usr/local/cargo/bin/{cargo,rustc,…}`，已實測列出）。
⇒ 「用 rustup 裝工具鏈」這條路工具鏈本身不會落在 canonical；用 `cargo install` 裝的**工具**會。

---

## 4. 與上游自有機制的相容性

### 4.1 🔴 `hermes_cli/managed_uv.py` 的 `UV_UNMANAGED_INSTALL` / `UV_INSTALL_DIR` — **不打架**

上游 `_install_uv()`（`managed_uv.py:1098-1116`）逐字：

```python
def _install_uv(target: Path) -> None:
    """Bootstrap uv into *target* using the official standalone installer.

    Uses ``UV_UNMANAGED_INSTALL`` (POSIX) or ``UV_INSTALL_DIR`` (Windows)
    so the astral installer writes the binary directly into
    ``$HERMES_HOME/bin/`` instead of ``~/.local/bin/``.
    """
    env = {
        **os.environ,
        "UV_UNMANAGED_INSTALL": str(target.parent),
        "UV_INSTALL_DIR": str(target.parent),
    }
```

三條互不相交的證據：

1. **消費者不同**：這兩個變數是 **astral 的 `install.sh` 安裝腳本**讀的，不是 `uv` 這支 binary
   讀的——`grep -a` 對 `/usr/local/bin/uv` 的結果是 **absent / absent**（而 `UV_TOOL_BIN_DIR`
   等三個是 PRESENT）。同一支 grep 的陽性對照成立 ⇒ 這個 absent 不是探針壞掉。
2. **管的東西不同**：它們管「**uv 這支 binary 自己**裝到哪」（`$HERMES_HOME/bin`）；本檔的
   A1/A2/B1 管「**uv 幫你裝的東西**放哪」。
3. **覆寫方向安全**：上游是 `{**os.environ, "UV_UNMANAGED_INSTALL": …}` ⇒ 上游**自己覆寫**，
   我們從 container spec 注入的值也影響不到它（我們根本不注入這兩個名字）。

**另一處也查了**（`managed_uv.py:81-114` 的 `managed_python_env()`）：上游組 env 時 pop 掉
`UV_PROJECT_ENVIRONMENT / UV_NO_MANAGED_PYTHON / UV_PYTHON / UV_PYTHON_DOWNLOADS /
UV_SYSTEM_PYTHON / VIRTUAL_ENV / PYTHONHOME / PYTHONPATH`，再 `update` 進
`UV_PYTHON_INSTALL_BIN="0"`、`UV_PYTHON_INSTALL_DIR=<target>`。

- `UV_PYTHON_BIN_DIR` **不在 pop 清單裡**，但上游把 `UV_PYTHON_INSTALL_BIN=0`（＝不建 bin 連結）
  **顯式設死** ⇒ 我們的變數對上游那條路徑**零作用**。
- 反向也安全：上游沒有把 `UV_TOOL_*` 帶進任何 tool 安裝流程（全樹沒有 `uv tool install` 型
  argv，grep 零命中）。

### 4.2 上游 LSP 安裝器 — **不打架，且已有第一手證據**

`agent/lsp/install.py`：

- npm 策略：`[npm, "install", "--prefix", str(staging), …]`（`:267`）——用 **CLI `--prefix`**。
  §3.4 查證 1 已實測 **CLI 贏過 env** ⇒ `npm_config_prefix` 不會把 LSP server 搬走。
- go 策略：`env = dict(os.environ); env["GOBIN"] = str(staging)`（`:311-312`）——上游**自己覆寫**
  `GOBIN` 到 staging ⇒ 我們的 `GOBIN` 被它蓋掉，正是要的行為。

### 4.3 `_ensure_tui_node`（execution-planes ADR §4.1 提到的條件式風險）

`hermes_cli/main.py:1761`（`def _ensure_tui_node`）／`:1774`：
`if shutil.which("node") and shutil.which("npm"): return` ——今天 node/npm 都在 ⇒ **早退，整段不跑**。
本檔的變數不改變這個判斷。

⚠ 但要記一筆給後人：該分支若真的跑起來，會把 `Path.home()/".local"/"bin"` 前插到 **dashboard
行程 PATH 的最前面**，而它有 `extra.is_dir()` 過濾——**initContainer 建出 canonical 目錄之後，
這個過濾就會通過**。今天仍不可達（node/npm 都在），屬於「建目錄把潛伏轉現行」的第二個實例，
與 execution-planes ADR §4.1 已拍板的圍堵同一類，不需額外處置。

### 4.4 🔴 第四面的 `install_cmd` 會因為 A4 從「必失敗」變成「會成功」

`plugins/memory/byterover` 的安裝提示是 `npm install -g byterover-cli`，而第四面
（`_run_setup_command`，`shell=True`、**無 approval gate**，見 #718）就是跑這種指令的地方。
今天它必然 EACCES（§3.4 查證 2 的負向對照）；加了 `npm_config_prefix` 之後**會成功**，
落點 canonical、在 PVC 上。

- 功能面：這是好事（第四面本來就該能裝它宣告的相依）。
- 風險面：它讓 #718 那條「沒有 approval gate 的 `shell=True`」從「跑了也裝不起來」變成
  「跑了會真的落地一個可執行檔到 root PATH 前段的目錄」。**這是 #718 既有風險的放大，
  不是本次新開的洞**（同 uid、同目錄、同通道），已寫進
  [support runbook](../guides/deployment/hermes-agent-console-support-runbook.md)。

---

## 5. 決策紀錄（leader3，2026-08-16）

**D1 — pip 家族不設變數（採用）。** 理由見 §3.2 的兩條第一手證據；核心是
`PYTHONUSERBASE` 會繞過已拍板的 L3 圍堵，而要蓋住它就得改寫圍堵規則本身。
配套：(a) 本檔保留 §3.2 全部論證；(b) 租戶文件寫明 pip 邊界；
(c) 這是對 issue「六家族」字面的 deviation，兩張 PR 描述與 #717 的收官 comment 都明寫並連結本檔。

**D2 — Tier A ＋ Tier B 十個變數全收。** chart 契約測試逐鍵逐值斷言十個名字
（存在性判準，不用「env 總數 == N」——總數型判準對「名字打錯但數量對」天生盲目）。

**D3 — 本檔落 `docs/decisions/`，與租戶文件、support runbook 同一個 PR 進。**
chart（`woow-paas-charts`）那張 PR 只需要 §0 的結論表。

---

## 6. 判別法：某個執行中的部署含不含這個修正

⛔ **不要用 chart 版號判**：`appVersion` 追的是上游、`version` 會因無關改動而 bump，
真 image 又釘在平台的 `helm_default_values`——三個數字沒有一個與「有沒有這個修正」單射。

**A. container spec 的 env 名單（最乾淨，唯讀、不進 pod）**

```sh
kubectl --context woow-k3s -n <ns> get deploy <release>-hermes-agent \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="hermes-agent")].env[*].name}'
```

修正**之前**只會是 5 個 secretKeyRef 鍵（`HERMES_DASHBOARD_BASIC_AUTH_PASSWORD`、
`HERMES_DASHBOARD_BASIC_AUTH_SECRET`、`MINIMAX_API_KEY`、`OPENROUTER_API_KEY`、
`API_SERVER_KEY`）。出現 §0 Tier A/B 任一名字 ⇒ 已含修正。
**驗收請斷言十個全在**——少一個就是那一家族靜默不收斂。

**B. initContainer 名單**

```sh
kubectl --context woow-k3s -n <ns> get deploy <release>-hermes-agent \
  -o jsonpath='{.spec.template.spec.initContainers[*].name}'
```

修正之前最多只有 `authproxy-init`。出現 `cli-landing-init` ⇒ 已含修正。

**C. 容器內目錄存在性（輔助，不可單獨使用）**

```sh
kubectl --context woow-k3s -n <ns> exec <pod> -c hermes-agent -- \
  stat -c '%n %U:%G %a' /opt/data/.local/bin
```

⚠ 不能單獨當判準——租戶自己 `mkdir` 或跑一次 `uv tool install` 也會建出它，必須配 A 或 B。
⚠ 判別力只在 **owner**：`fsGroup` 會在每次掛載時把 group 與 mode 重寫成固定值，
所以 group／mode 恆綠、驗不出東西。

---

## 7. 本檔的證據邊界（明列，避免被當成已驗）

1. **L3 繼承 pod spec env 未在 K8s 實證**（§2 的 (a) 驗的是 s6 那一層）。
2. **go / cargo 的驗證是「機制正確」不是「在這顆 image 上跑過」**——那兩個工具鏈不在 image 內。
3. **`uv python install` 未端到端跑過**（會下載整包 CPython；只驗到 `uv python dir --bin`
   的鑑別式與 `--no-bin` 反證預設行為）。
4. **本檔的容器實驗在 amd64 emulation 下跑 amd64 image**（主機 arm64）。所有結論只依賴 env
   解析邏輯與路徑計算，不依賴 CPU 指令集。
5. **本檔零 kubectl**，沒有做任何叢集操作。

# Pitfall: CI `container: <lang-image>` + `actions/checkout` → node not found (exit 127) → pytest 從沒跑到（false green）

> 來源：`agent-runtime-pod` epic Phase B（`paas-agent-runtime` repo，B-5，fix commit `5b2f9ed`）。CI workflow 用 `container: python:3.11` 跑 pytest，但 `actions/checkout@v4` 是 JS action 需 node，slim language image 無 node → exit 127 → 後續 step 全 skip → **pytest 從沒跑到**，而 workflow 結論未必紅（false green 風險）。

## Symptom

CI workflow（GitHub Actions / Gitea Actions，syntax 相容）長這樣：

```yaml
jobs:
  pytest:
    runs-on: ubuntu-latest
    container:
      image: python:3.11          # ◄── 想用 image 內建的 python 跑 pytest
    steps:
      - uses: actions/checkout@v4 # ◄── JS action，需 node runtime
      - run: pip install -r requirements.txt -r requirements-dev.txt
      - run: pytest tests/
```

實際跑起來：

```
Run actions/checkout@v4
/usr/bin/env: 'node': No such file or directory
...
exit code 127
```

- `actions/checkout` step 直接掛掉（exit 127 = command not found，這裡是 `node` not found）
- 後續 `pip install` / `pytest` step **全 skip**（前置 step fail）
- **最危險的變體**：若 workflow / runner 對 checkout failure 的處理不夠嚴格，或有人只看「最後一個 run step 沒紅」，會誤判 CI 綠 — 而 `pytest tests/` 從來沒被執行過（**false green**：沒有 `collected N items` / `N passed` 出現在 log，但沒人注意）。

## Why it happens

### JS action 需要 node，slim language image 沒有

- `actions/checkout` / `actions/setup-python` / `actions/cache` 等官方 action 都是 **JavaScript action**（`runs.using: node20`），runner 在執行時用 `node` 跑它們的 `dist/index.js`。
- 當 job 指定 `container: <image>`，所有 step（含 JS action）都在 **該 container 內** 執行 — runner 在 container 裡找 `node`。
- `python:3.11`（尤其 `-slim`）只裝 Python，**不含 node**。`node` not found → JS action exec 失敗 → exit 127。
- 對比：不指定 `container` 時，step 跑在 **runner host**（`ubuntu-latest`）上，host 自帶 node → JS action 正常。

### 為什麼 implementer 容易漏（心智模型偏差）

「我要跑 pytest，pytest 需要 Python，所以用 `python:3.11` container」— 這個推理只覆蓋了 **run step 的執行環境**，**漏掉 setup step（checkout）的執行環境**。checkout 不是 shell command，是 JS action，它的 runtime 需求（node）與「我的測試需要什麼」是兩回事。

### 為什麼「三層 green」漏抓（只有真 CI 是 authoritative）

開發過程常見三層驗證，**前兩層根本不跑這個 workflow**：

| 層 | 怎麼驗 | 為什麼漏抓 CI infra bug |
|----|--------|------------------------|
| 本機 macOS | 直接 `pytest tests/` | 沒跑 workflow，沒有 container / checkout step |
| arm64 local container | `docker run python:3.11 pytest` | 手動進 container 跑 pytest（沒走 actions runner，沒 checkout JS action）|
| **真 CI（runner 跑 workflow）** | push / PR 觸發 | **唯一會 exec `actions/checkout` JS action 的環境** — 也是唯一會爆 exit 127 的地方 |

「本機綠 + container 綠」給人「測過了」的錯覺，但 CI workflow 本身的 infra（checkout 需 node）從沒被前兩層觸及。**只有真 CI 是 authoritative gate**。

### 為什麼 false green 特別陰

exit 127 發生在 **第一個 step（checkout）**，不是測試 step。若：

- 只看 workflow badge / 結論顏色（某些 runner 對早期 step fail 顯示不直觀）
- 或假設「CI 跑完了 = 測試過了」

→ 會錯過「pytest 根本沒 collect 任何 test」。CI 的價值（守 test）完全沒兌現，卻給了「綠」的安全感。

## How to avoid

### 1. 預設用 `runs-on` host + `setup-python`，不用 `container`（推薦）

把 Python toolchain 換成 `actions/setup-python`，step 跑在 host（host 有 node 供 JS action）：

```yaml
jobs:
  pytest:
    runs-on: ubuntu-latest          # host 有 node
    steps:
      - uses: actions/checkout@v4   # JS action 在 host 跑，node OK
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -r requirements.txt -r requirements-dev.txt
      - run: pytest tests/
```

這是 `paas-agent-runtime` B-5 的修法（commit `5b2f9ed`）。workflow 內 comment 鎖死契約，防後人改回 container：

```yaml
# ⚠ 不可改用 container: python:3.11 —— 該 image 無 node，actions/checkout@v4（JS action）
#   會 exec "node" not found（exit 127），導致後續 step 全 skip、pytest 從沒跑到。
```

### 2. 若一定要用 `container`（如需 image 內預裝的系統相依）

- 選含 node 的 base image，或在 container 內 pre-install node；**或**
- 把 checkout 換成 git clone shell step（繞過 JS action）— 但失去 checkout 的 ref / submodule / token 處理，不推薦；**或**
- 對齊 prod runtime 的真正需求才用 container（如本 epic 的 prod pod 是 Linux `python:3.11`，但 **CI 測試** 用 host + setup-python 即可達到同等 Python 版本，不需 container）。

### 3. 新 CI workflow 第一次跑，必須確認「pytest 真的跑了」

不是只看 workflow 結論 green，要看 log 有沒有測試實際執行的證據：

```
collected 142 items
...
============ 142 passed in 12.3s ============
```

若 log 沒有 `collected N items` / `N passed`，CI 沒在驗 test（不管結論什麼顏色）。

### 4. 加 sentinel 讓 false green 變紅（強守線）

在 CI 加一個「assert pytest 真的跑了」的 check，避免 silent skip：

```yaml
      - name: Run tests (fail if pytest never ran)
        run: |
          pytest tests/ | tee pytest.out
          grep -qE "[0-9]+ passed" pytest.out || { echo "::error::pytest never produced a pass summary"; exit 1; }
```

或用 pytest 自身的 `--strict-markers` + 確認 exit code，並讓 job 對「0 test collected」報錯（`pytest --co -q` 先 assert collect 數）。

## Related

- 真實落地 case：
  - `paas-agent-runtime` repo `.gitea/workflows/test.yml`（修後版本 + 防回退 comment）+ fix commit `5b2f9ed`（B-5）
  - `docs/archived/tracking/agent-runtime-pod-phase-b-image-source-2026-05-23.md` §3（CI gate 三度價值，本 pitfall 是第 3 次）+ §5 Learning 1 / Learning 2
- 同 family learning（CI / verification 守線）：
  - `verification-before-completion`（superpowers skill）— authoritative gate 的 output 才算數，不是 dev box 的綠
  - [[tag-scoped-test-misses-full-u-failures]] — 另一個「驗綠範圍不對 → 漏抓」的 pitfall（tag-scoped vs full suite；本 pitfall 是 local/container vs 真 CI）
- 上游原理：
  - GitHub Actions：JS action（`runs.using: node20`）在 job container 內執行時需 container 內有 node — 官方 [container-based jobs 文件](https://docs.github.com/en/actions/using-jobs/running-jobs-in-a-container) 明示 node 需求
  - Gitea Actions：syntax 相容 GitHub Actions，同樣行為
  - exit code 127 = "command not found"（這裡是 `node`）

## 補記（2026-07-06 自 CLAUDE.md 索引化搬運）

適用範圍：**cross-repo CI infra learning**（agent-runtime-pod epic Phase B，`paas-agent-runtime` repo Gitea Actions）——任何用 `container: <lang-image>` 跑 GitHub/Gitea Actions workflow 的 repo 都適用；**本 repo 的 Odoo CI 不受影響**（未用 container image 模式）。

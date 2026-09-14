---
name: impl-pi-implement-contract
description: pi 實作 worker 的工單執行流程：待在 worktree、跑綠 gate、zh-tw commit、用 tea 開 PR。
---

# impl-pi-implement-contract

## 什麼時候用

拿到 `purpose: implement` 的工單時，照這條走完。內容與 `impl-cc-implement-contract` 對齊
——刻意分成兩份，是因為來源池的 skill name 必須全域唯一（`discover_host_skills` 是
first-wins 去重，同名後者會被吃掉）。

## 步驟

1. **建 worktree、再進去**。worktree 是你自己建的：
   `git -C <工單給的 repo root> worktree add <工單給的 worktree 路徑> -b <type>/<issue-id> origin/<整合分支>`
   （已存在就直接用）。接著 `cd <worktree 絕對路徑>`；`pwd` 對照工單給的路徑，branch 對照
   工單。不對就停下來回報，⛔ 不要換路徑另開。
   `block_working_dir_changes` 的 `allowed_dirs` 只含 repo root 與該 worktree：相對路徑的
   cd、cd 到別處、`git -C <別處>` 都會被 DENY。⛔ 進 repo root 只為建 worktree，不要在主
   checkout 裡改檔。跑子目錄的測試把路徑當參數傳（`python -m pytest src/tests/test_x.py`），
   ⛔ 不要 `cd src && pytest`。
2. **確認整合分支**（`develop` / `main` / `master` 因 repo 而異，⛔ 不要寫死）。
3. **只動契約點名的檔案**。動之前先讀。
4. **跑綠**：測試、lint、typecheck。指令與輸出留著。
   ⚠ 本 repo 的 Odoo 測試一律在**沙盒 odoo pod 內**跑；repo 裡沒有 `odoo-bin`。
5. **commit**：zh-tw，前綴十選一。⛔ 不要加「Generated with …」字樣。
6. **push branch**（⛔ 不是受保護分支、⛔ 不 force-push），用 `tea` 開 PR，標題前綴
   `⛔ 勿代 merge｜待 <覆核者> 覆核 — `，描述貼真實輸出，關聯當下就寫。

## 本 repo 特有的地雷

- ⛔ **不要動 `src/__manifest__.py` 的版號**（`manifest-version-gate` 會擋）。
- PR 的 CI 綠燈**不含 Odoo 測試**——動 `src/` 要自己在沙盒跑完並貼進 PR。
- 改 `extra/paas-operator/`：**integration tests 沒有 CI runner**（unit 有），合併前必須對真
  K8s 跑過。repo 的 `paas-operator-dev` skill **你看不到**（共同祖先 workspace 下 repo 的
  `.claude/skills/` 不載入），精華如下：
  - 只打 **docker-desktop**（`KUBECONFIG` 指向 docker-desktop 的 kubeconfig）。要 woow-k3s
    的 KubeVirt 路徑（`REQUIRE_KUBEVIRT=1`）回報給上層派 `ops_k8s`，⛔ 不要自己打共用叢集。
  - ⚠ helm 必須用 operator Dockerfile 釘的 **3.13.3**（`curl -fsSL
    https://get.helm.sh/helm-v3.13.3-<os>-<arch>.tar.gz | tar xz -C /tmp/helm3`），⛔ 不可用
    系統 helm——helm 4 的 `--post-renderer` 改吃 plugin 名，整套會在第一步 install 就 500，
    看起來像被測程式壞掉。
  - 跑法（venv 依 `extra/paas-operator/tests/integration/README.md` 建）：
    `cd <worktree>/extra/paas-operator && KUBECONFIG=<dd kubeconfig> HELM_BINARY=/tmp/helm3/<os>-<arch>/helm .venv/bin/pytest -m integration tests/integration/ -v`
    （`API_KEY`／`NAMESPACE_PREFIX` 由 fixture 自動設）。unit：`python -m pytest tests/ -m "not integration"`。
  - 中斷會留下 `paas-ws-test-op-<uuid8>` namespace，結束後用
    `kubectl --context docker-desktop get ns` 確認清掉（⛔ 不要接 pipe，分兩次 tool call）。
- 動過 `docs/` 就跑 `python3 scripts/audit/gen-docs-index.py` 重產 `docs/index.md`。

# reference operator pytest venv py311（已封存）

> 自 Claude memory 遷移（2026-08-05）。原 slug `reference_operator_pytest_venv_py311`。盤點依據見 [memory-inventory-2026-08-05](../../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description：paas-operator pytest 必用 repo 內 .venv（py3.11），系統預設 python 3.9 collection 期即炸

跑 `extra/paas-operator` 的 pytest 時，**必須用 repo 內的 venv**：

```bash
cd extra/paas-operator && .venv/bin/python -m pytest tests/... -q
```

**為什麼**：macOS 系統預設 `python`/`python3` 是 **3.9**，但 `extra/paas-operator/src/config.py` 用了 `str | None`（PEP 604 union 語法，需 **3.11+**）→ 用 3.9 跑 pytest 在 **collection 期就 TypeError 炸**（還沒進任何 test）。repo 內 `.venv`（Python **3.11.15**、deps 齊全）才跑得動。CLAUDE.md 說「operator pytest 原生可跑、無 amd64 模擬問題」為真，但前提是走這個 venv，不是系統 python。

實測 2026-07-07 PR-278：三檔合跑 `tests/test_helm.py tests/test_api.py tests/test_agent_runtimes.py` → 197 passed（走 .venv）。

相關：Odoo 側 test 仍需 k8s sandbox（[[reference_docker_build_local_odoo_test]] 的 amd64 模擬限制），與 operator 端這個純 py-version 陷阱是兩回事別混。

**2026-07-28 補：integration 的 helm 版本坑**——brew `helm` 已升 **4.2.3**（連 `helm@3` 的 opt symlink 都壞掉指向 4）。helm 4 把 `--post-renderer` 改 plugin 機制，operator 的 `--post-renderer <python path>` 直接炸（既有 `test_pvc_keep_before_uninstall.py` 也會紅）。正解＝下載官網 **v3.13.3**（與 prod operator 同版）用 `HELM_BINARY=` 指定。另一 quirk：`_parse_release_info` 讀 `info.revision` 但 helm JSON 的 revision 在頂層 `version` → endpoint 回應 revision 恆 0（無實害未修）；integration 驗 revision 改看 helm release Secret `sh.helm.release.v1.<name>.v<N>`。

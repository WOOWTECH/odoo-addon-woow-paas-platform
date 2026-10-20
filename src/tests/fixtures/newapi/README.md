# new-api／代理回應 fixture（NA-03 錄製，LLM-02 的 client 分類測試用）

`tests/test_llm_services.py` 的 `TestRecordedFixtureClassification` 逐一讀這裡的檔、組成 HTTP 回應（或傳輸層事件）
交給 `src/services/newapi_client.py` 分類，對 DESIGN §3.4 的分類表斷言結果。Odoo 不載入這個目錄（不在
`__manifest__.py`、也不是 Python 模組）。

- **來源**：Gitea `woow-paas/Woow_k3s_newapi` `main` 的 `tests/contract/fixtures/*.json`，commit
  `d91ad9c`（fixture 最後一次變更是 `0b07f8d`）。複製當時 `tests/contract/reports/` 還沒有任何 gate 報告，
  所以全部是 `_source: contract-shape`（依代理原始碼產生的形狀，reason 一律是代理真的會送的值）。
- **何時重新同步**：負責人跑完 NA-03 gate（設 `NEWAPI_CONTRACT_RECORD_DIR`，live 回應會覆蓋同名的
  contract-shape 檔）並把報告 commit 進該 repo 之後，從**那個 commit** 再複製一次、改上面的 commit；代理改版或
  new-api 升版重錄之後也一樣。
- **去敏**：這些檔在來源 repo 已經過 `scrub.scrub`＋`scrub.residue`；複製後再跑一次驗收 grep（應該沒有輸出）：

  ```bash
  grep -rlE '(^|[^A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}|nap_[A-Za-z0-9]' src/tests/fixtures/newapi
  grep -rlE '"password": *"[^<"][^"]*"' src/tests/fixtures/newapi
  ```

- **格式**：見來源 repo 的 `tests/contract/fixtures/README.md`。`message_key` 被去敏成 `<redacted>`，但仍是非空字串，
  分類照樣是 Definite。NA-03 沒有錄到的分類（Ok、帶 `code` 的拒絕、`success:false` 沒有 `message_key`、429 空 body、
  預檢轉回、非 JSON）由測試依 DESIGN §3.4 自己組。

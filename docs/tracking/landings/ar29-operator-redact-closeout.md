# project ar29 operator redact closeout

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_ar29_operator_redact_closeout`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："ar#29 operator log 洩密收官——PR#505 payload 級遮罩、operator 20260801.1305 上 prod、stg 拋棄式活體驗證 recipe 與殘留分流"

**woow-paas/paas-agent-runtime#29 收官（2026-08-01 closed）**：operator helm/kubectl 指令 log 洩密，兩輪修法都上 prod。

- 第一輪 flag 級＝platform PR#172（3cf59d3c，2026-06-09 早已 landed——**接手前先查有沒有人修過一半**，本輪派工假設「還沒修」是錯的，實際工作是全 caller 稽核抓 payload 級縫）。
- 第二輪＝PR#505（merge 36a6657b）：patch secret/configmap data 子樹、exec script inline regex＋**fail-closed 兜底**（遮罩失效只准退化成遮更多）、EnvVar value 槽（祕密放 value 槽、key 名導向規則永不命中）、`str(TimeoutExpired)` 內嵌原始 cmd list 會把剛遮掉的密碼經 stderr 回流 log＋HTTP body（`stderr=str(e)` 是反模式）、secretKeyRef 公開參照要豁免（否則反向誤遮）。
- 發版：operator image `20260801.1305`、digest `0f87ac73…0179be`，CD PR#79（merge 2f8fdf9）；operator values-prod **無 digest 欄位、tag-only 生效**（詳見 [[build-operator-deploy-stg]]，含 `--ref refs/tags/` 全名教訓）。

**stg 拋棄式活體驗證 recipe**（「kubectl 唯讀」限制下驗不到遮罩形態時的正解）：授權後用拋棄式 project_hash（如 r29probe）POST /api/agent-runtimes/spawn-or-attach 走正常路徑 → log 現 `--username <redacted> --password <redacted>`＋非祕密 payload 逐字保留（雙向證據：遮了該遮的、沒遮不該遮的）→ 清理 ns。**⚠ 清理要記得 PV**：#317 的 Retain 機制讓 ns 刪掉後留 Released PV，Longhorn orphan 清理與 pvc-cleanup 都不碰它，會永久殘留；claimRef 指向自建 probe ns 即歸屬無疑義可刪（#319 教訓不適用自建殘留）。

**殘留分流**：已洩 Gitea helm token 輪替→cd#78（與 cd#24 JCR 同批，皆卡 Eugene 帳密）；argv 根治（/proc/cmdline）＋n8n owner 密碼 stdin 化＋G4/H1-H3→platform#509。

相關：[[build-operator-deploy-stg]]、[[29-389-mixin-sudo-dedup-landing]]（同日另兩支 PR）。

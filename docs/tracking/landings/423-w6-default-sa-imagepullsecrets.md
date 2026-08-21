# project 423 w6 default sa imagepullsecrets

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_423_w6_default_sa_imagepullsecrets`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#423 W6 租戶 ns default SA 補 imagePullSecrets（PR#480）；controller 建立的 pod 只能靠 SA 拿憑證；merge patch 對 array 整份覆寫；新增 kubectl 呼叫害 unit suite 打真叢集"

2026-07-30 交付（leader2 派工，teammate haos-op-sa）。PR **#480**、分支
`fix/423-w6-default-sa-imagepullsecrets`、SHA `80c38caf`，只動 `extra/paas-operator/**`。
unit 489 passed/39 deselected（develop baseline 實跑 472/33），integration 37 passed/2 skipped。

非顯而易見、之後會再用到的三點：

1. **chart values 的 imagePullSecrets 碰不到 controller 建立的 pod**。CDI importer
   pod 由 CDI controller 依 DataVolume 生成，不屬於任何 chart 的 podSpec，所以 #448
   那層（`_ensure_image_pull_secrets` 注入 values）對它完全無效。它跑在租戶 ns 的
   **SA `default`** 上，唯一的槽是 ServiceAccount admission。任何「pod 不是我們
   render 出來的」場景（CDI、Job controller、webhook 生成的 pod）都適用同一判斷。

2. **JSON merge patch 對 array 是整份覆寫**。patch SA 的 imagePullSecrets 必須
   read-then-append 整份送出，否則會靜默洗掉既有項目——而且若測試只斷言「目標
   secret 在不在」會全綠看不出來。守線要斷言**整個清單內容與順序**。

3. **在 API handler 的共用 helper 裡加 kubectl，會讓 unit suite 開始打真叢集**。
   `tests/test_api.py` 從 <1s 變 81s，且開發機 current-context 可能正指 woow-k3s
   （＝prod）。解法是 `extra/paas-operator/tests/conftest.py` 的 autouse fixture 換掉
   那個 seam、integration 以 marker 排除。**新增任何 operator 服務層呼叫進 releases.py
   之前先想這件事。**（附帶：`KUBECONFIG=/dev/null` 時 kubectl 回的是
   `Error from server (NotFound)`，會被 NotFound 判別式當成良性 → 測試「通過但很慢」，
   比紅燈更難察覺。）

相關：[[project_424_item1_hermes_agent_cs_landing]] 的 #448 是同一 gap family
（install 補了 upgrade 漏了）。

⚠ 一則自我更正，供日後別再重犯：我曾判定「CLAUDE.md 的 `HELM_BINARY=$(which helm)`
已過期」——**錯的**。develop 的 CLAUDE.md（第 131 行）與
`extra/paas-operator/tests/integration/README.md` **都早已是修正版**（#458 落地），
明確禁用 `$(which helm)` 並釘 3.13.3。過期的是**派工單裡抄自舊文的那行**，以及我
session context 裡那份 CLAUDE.md 快照。教訓：斷言「repo 文件過期」之前先 grep 目標
分支的實體檔案，不要拿 context 內的快照當現況——這與
[[feedback_rollout_workorder_verify_baseline_live]]（發版單起點必查實值）同一類錯。

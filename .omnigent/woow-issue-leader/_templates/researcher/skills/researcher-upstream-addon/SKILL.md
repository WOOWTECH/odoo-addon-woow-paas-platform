---
name: researcher-upstream-addon
description: 上游 addon／Odoo 18 行為查證：先讀本機原始碼再回答，區分「確實存在／確實不存在／查不到」。
---

# researcher-upstream-addon

## 什麼時候用

要回答「上游 addon 或 Odoo 本身實際上怎麼行為」時——尤其是在新增 model／欄位／流程之
前，確認上游有沒有現成實作。

## 為什麼這件事要有專人

本 repo 的規矩是「已有 addon dependencies 不要重造輪子」：有現成的就 `_inherit` 擴充
（實例 `src/models/ai_assistant.py`、`src/models/sh_subscription_plan_cs_spec.py`），只有
修 bug 或補上游沒有的功能才自建。這條規矩成不成立，完全取決於這個問題答得準不準。

## 步驟

1. **先看依賴清單**：`src/__manifest__.py` 的 `depends` 是權威。
2. **找到本機的上游原始碼**：⚠ 上游 addon 的原始碼**不在本 repo**，本機路徑見
   `.sandbox.yml` 裡 `PAAS_CI_ADDONS_PATH` 的註解。
3. **實際打開來讀**。⛔ 不要從 Odoo 的一般知識推論——本 repo 是 **Odoo 18**，版本差異很
   致命。
4. ⛔ 不改任何檔案（`read_only_os` 機器守線會 DENY）。

## 回答方式

答案只有三種，**明確標出是哪一種**：

- **確實存在** — 附上游原始碼的**絕對路徑＋行號**（上游不在本 repo，相對路徑會指錯）。
- **確實不存在** — 說明你找過哪些 addon、用什麼關鍵字。
- **查不到** — 說明卡在哪（例如原始碼不在本機）。

⛔ 不要用「應該有」「通常是」「Odoo 一般來說」搪塞——那正是重造輪子的起點。

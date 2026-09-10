---
name: scout-pi-recon
description: 唯讀偵察：回答一個明確的定位問題，附 file:line 證據，找不到就說找不到。
---

# scout-pi-recon

## 什麼時候用

`purpose: explore` / `search`，而且問題是**明確可定位**的：這個東西在哪、這條路徑怎麼
走、這個值現在是多少。

## 步驟

1. **先把問題收斂成一句話**。收斂不了就代表這題該升級成 explore／implement——回報，
   ⛔ 不要自己擴張範圍。
2. **從權威來源找起**，不要從記憶找。本 repo 的權威入口：
   - 端點：`grep -rn '@route' src/controllers/` ＋ OWL hash 路由
     `src/static/src/paas/core/router.js`
   - 依賴：`src/__manifest__.py` 的 `depends`
   - 文件：`docs/index.md`（依「我現在想做什麼」routing）
   - 名詞：根目錄 `CONTEXT.md`
3. **只讀你需要的那些行**（`sed -n '<start>,<end>p'`），⛔ 不要整檔搬進 context。
4. ⛔ 不改任何檔案（`read_only_os` 機器守線會 DENY）。

## 回答方式

- 每個結論附 `file:line`（相對路徑＋行號，方便 IDE 定位）。
- 找不到就說找不到，並列出**你找過哪些路徑、用什麼關鍵字**。⛔ 不要猜、不要用一般知識
  補洞。
- 只回答被問的那題。順手發現的其他問題另外列成「附帶觀察」，不要混進答案裡。

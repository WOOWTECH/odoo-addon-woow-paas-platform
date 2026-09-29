---
title: Security Access：route 管理、改名、tunnel 轉移與狀態判讀（租戶指南）
category: guides
status: active
created: 2026-09-29
updated: 2026-09-29
related:
  - docs/guides/testing/security-access-stg-e2e.md
---

# Security Access：route 管理、改名、tunnel 轉移與狀態判讀

> 適用：Security Access（SA）的租戶。改名自 18.0.1.0.181 起；route 狀態欄與額度顯示自 #1234 起。
> 本頁每一條「會發生什麼」都有 2026-09-29 正式站實測當依據（#1196、#1240、#1234）。

## 1. 名詞

- **tunnel**：你的機器與 Cloudflare 之間的一條加密通道。一個 SA 一條。
- **connector**：在你機器上跑、用 tunnel token 把 tunnel 接起來的程式（cloudflared）。
- **route**：一個公開網址（`<子網域>.woowtech.io`）→ 你機器上一個服務位址（service URL）。
  一個 SA 可以有多條 route，上限依方案而定（「應用程式」分頁右上角顯示 `已用 / 上限`）。

## 2. 改 route 的子網域（改名）

在「應用程式」分頁按該列的編輯，改子網域後儲存。**route 本身不變**（id、保護設定、policy 都跟著走），平台會同步搬 Cloudflare 上的 DNS 與轉發設定。

改名前要知道三件事：

| 會發生什麼 | 你要做什麼 |
|---|---|
| 新網址通常 **20 秒～1 分鐘**內可用 | 儲存後等一下再開；狀態欄顯示「Not live yet」是正常的 |
| 舊網址**立刻失效、不會轉址** | 先通知使用者、更新書籤與連結 |
| 送到你機器的 Host 會變成新網址——**通常不論有沒有勾 Host header**（見下方說明） | 你的反向代理（例如 Nginx Proxy Manager）或只接受已知 host 的服務，**先加新網址再改名**，否則會連不到——實測 NPM 會回它的「Default Site」預設頁，狀態欄顯示「Proxy has no site」 |

> **Host header 選項實際做什麼**（#1261）：勾選＝**強制**把送到你服務的 Host 設成公開網址（cloudflared 的 `httpHostHeader`）。
> 不勾時 cloudflared 本來就會原樣轉送訪客的 Host，通常就是同一個公開網址——所以取消勾選一般**不會**改變你的服務收到的值（2026-09-29 正式站實測勾／不勾結果相同），也**不會**讓後端改收 service URL 的主機名。

## 3. 把 tunnel 搬到另一台機器（token 轉移）

tunnel 可以任意轉移：在「Connect a machine」卡片複製 tunnel token，在新機器用它跑 connector，公開網址完全不變。

- ⚠ **一次只在一台跑**。同一把 token 同時在兩台跑，Cloudflare **不保證平均分配**，訪客可能一直被送到其中一台（2026-09-29 實測：40 個請求全部留在原本那台）——新的起來就停掉舊的，否則會以為搬好了、訪客其實還在打舊機器。
- ⚠ token 等同密碼：拿到它的人可以接走你的網址。
- 新機器上 route 的 service URL（例如 `http://a0d7b954-nginxproxymanager:80`）必須在**新機器上**解析得到。

## 4. route 狀態欄怎麼看

「應用程式」分頁的 **Status** 欄是平台從**公開網路**實際打一次每條網址的結果（按右上角重新整理可立即重測）。滑過狀態可看建議。

| 狀態 | 代表什麼 | 你該做什麼 |
|---|---|---|
| Reachable | 請求穿過 Cloudflare 與 tunnel，你的服務有回應 | 無 |
| Login required | 有保護：先看到 Cloudflare Access 登入頁 | 無（這是預期行為） |
| Connector offline | tunnel 沒有任何 connector 連著（Cloudflare 1033） | 檢查跑 connector 的那台機器；或用 token 在別台啟動 |
| Not live yet | 網址還沒指到這條 tunnel | 剛建立或剛改名時等一分鐘再按重新整理 |
| DNS not found | 公開 DNS 查不到這個網址 | 同上；持續如此請聯絡客服 |
| Origin error (5xx) | 請求**有到**你的機器，但反向代理或服務回錯誤 | 檢查反向代理有沒有這個網址、後端服務有沒有在跑 |
| Proxy has no site | 請求**有到**你的機器，但反向代理回的是自己的預設頁（例如 NPM 的「Default Site」） | 在反向代理新增這個網址的站台；剛改名時一定會這樣 |
| No response | 時間內沒有回應 | 檢查服務是否過載或卡住 |
| Not protected | 設為保護，但實際沒有登入頁擋著 | 到「存取控管」把保護關掉再打開；持續如此請聯絡客服 |
| Blocked by Access | 設為公開，但 Cloudflare 上有平台未管理的 Access 擋著 | 聯絡客服（見 #1239） |

判讀訣竅：

- **只有一條不通、其他都通** → 那條的設定（反向代理、服務）有問題，不是 tunnel 掛了。
- **全部 Connector offline** → 看你的機器（電源、網路、connector）。
- **Origin error** → 範圍已縮到你自己的機器上那段。

## 5. 額度

route 數量上限依方案。達到上限時「Add Route」會停用並提示；要新增請先刪一條或變更方案。

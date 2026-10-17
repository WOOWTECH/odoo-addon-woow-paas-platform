# 發 tunnel 權杖的端點是無狀態的 ⇒ 兩台 Home Assistant 可以共用一條 tunnel，而且兩邊都顯示「已連線」

> 一句話：Cloudflare 對「一條 tunnel、多個 connector」的語意是**分流**不是拒絕，所以第二台機器接上去不會壞掉、不會報錯——使用者看到的是「遠端登入時好時壞」，平台看到的是一切正常。

## 症狀

使用者回報對外網址「現在遠端沒辦法登入使用」。實際行為是**大約一半的請求**落到另一台機器：那台也是一套正常的 Home Assistant，只是沒有他的帳號，所以帳密一直被拒。

平台端（修前）看起來完全正常：

- Smart Home 詳情頁：`已連線`、一個 connector、有 uptime
- `sensor.*_tunnel_status`（HA 端）：`connected`
- cloudflared log（兩台都有）：`Registered tunnel connection` × 4，沒有任何警告

## 根因（三件事疊起來）

1. **端點無狀態**：`controllers/ha_api.py` 的 `/api/smarthome/homes/<id>/tunnel-token`
   （與 SA 的鏡像端點）只驗 scope ＋ workspace owner／admin，然後把
   `tunnel_token` 原封不動回出去。同一個人在幾台機器上各跑一次設定流程，就拿到
   幾份**同一條** tunnel 的憑證。元件端的 `_abort_if_unique_id_configured()` 是
   **每台 HA 自己的** config entry 狀態，看不到別台機器。
2. **Cloudflare 的多 connector 是功能**：一條 tunnel 可以同時有 N 個 connector，
   請求被分到其中一個（零停機換版就靠這個）。所以第二台接上去是「成功」。
3. **平台的狀態判讀只看第一個**：`_tunnel_vals_from_status()` 讀
   `connections[0]`。而且 `connections[].connector_id` 是**每條連線**一個 id
   （cloudflared 預設開 4 條），不是每個行程一個——拿它當機器數會把正常的一台看成
   四台，所以「數量異常」這個訊號當時也沒人敢用。

2026-10-17 正式站實測（operator `GET /api/tunnels/<id>`）：

| 家 | 連線數 | 實際機器數 | 平台顯示 |
| --- | --- | --- | --- |
| id=1 `woowtech-ha` | **8** | 2（各 4 條） | 已連線 |
| id=2 `elmohome` | 4 | 1 | 已連線 |

兩台的 `origin_ip` 都是同一個（辦公室 NAT），所以連來源 IP 也分不出來。

## 修法（#1766）

| 層 | 做法 | 擋得住什麼 |
| --- | --- | --- |
| 平台：發權杖前先 claim | `models/tunnel_binding_mixin.py`：第一台帶 `instance_uid` 來的被記住，其他 uid 要同一筆 ⇒ 409 `already_bound`（**回應裡沒有權杖**） | 兩套獨立安裝各自設定（本次事故） |
| 平台：數機器 | 數 distinct `client_id`（operator 同步補傳這個欄位）⇒ `connector_count` / `binding_conflict` | 已經發生的共用、以及 uid 相同的映像複製 |
| 元件：本機身分 | entry 記著建立它的 HA uid，被搬到別台就不起隧道 | 還原備份／複製映像到另一台機器 |
| 元件：收到 409 不退回快取權杖 | `ConfigEntryError`，隧道保持不起 | 「平台說不行，元件自己用舊憑證硬上」 |

## 綁定要能「還回去」，否則換機器就卡死

claim 只做了一半：使用者在 HA 移除整合之後，平台那邊的綁定還記著那台機器 ⇒ 下一台選同一個家只會拿到 409，而**使用者完全看不出要去後台按「解除綁定」**。2026-10-17 現場就撞到：`.121` 的舊整合在 HA 端刪掉了，平台的綁定還在。

所以 claim 的反向動作要自動化：

| 事件 | 誰做 | 結果 |
| --- | --- | --- |
| 使用者在 HA 移除整合 | 元件 `async_remove_entry` → `DELETE /api/smarthome/homes/<id>/tunnel-binding` | 綁定立刻釋放，下一台接得了 |
| 機器死掉／重灌／RMA（沒有任何一台能自報身分） | 人在後台按「解除綁定」 | 同上，這是逃生口 |

三個約束，少一個就會出事：

1. **只有持有者本人放得掉**（平台比對 `instance_uid`）。否則 B 可以先放掉 A 的綁定再自己搶，整個閘變裝飾。owner／admin 的後台按鈕是刻意保留的例外。
2. **元件要先放綁定、再撤銷 OAuth 權杖**。顛倒的話那一槍帶著失效權杖出去，平台回 401，使用者又回到「要自己去後台解」。
3. **放綁定失敗不可以讓「移除整合」失敗**。使用者按了刪除就是要刪掉；失敗只留 warning 指出後台解綁這條路。

**刻意沒做**：「tunnel 長時間 0 connector 就自動釋放」。機器只是離線（停電、出國、網路斷）跟機器永久消失，平台分不出來；自動釋放會讓前者在毫無預警下被別台搶走。寧可留一顆按鈕讓人判斷。

## 三個會讓人想錯的地方

1. **解綁不會踢掉對方**。權杖已經在那台機器上，平台沒有辦法收回已交出的憑證。
   `action_release_binding()` 只是「讓下一台可以接手」；真的要斷開對方只能換權杖
   （刪掉重建那筆資源）。UI 文案必須照實說，否則使用者會以為按了就安全了。
2. **不能硬要求 `instance_uid`**。既有安裝（元件 ≤ 0.4.11）不會帶，硬擋會讓它們
   在下次重啟時集體斷線——那是用一次全面故障換一個尚未發生的風險。所以沒帶的
   照發並標成 `legacy`，但 `legacy → claimed` 是**單向**的：一旦有自報身分的實例
   claim 了，不帶 uid 的請求就會被拒，否則舊版元件就是繞過綁定的後門。
3. **元件舊版的 cloudflared 可能停不掉**。2026-10-17 現場：`.121` 跑 0.4.8，使用者刪掉整合之後那條 tunnel 上它的 4 條連線**還活著**——`async_unload_entry` 有呼叫 `stop_tunnel()`，但 0.4.8 缺了之後才進來的四個停止修正（`retain cleanup ownership after stop timeout`、`verify connector exit` …）。平台端放掉綁定**不會**讓那個行程消失，所以「綁定已釋放」和「對方真的斷了」是兩件事，要分開確認（看 `connector_count`）。

4. **`instance_uid` 不等於「一台實體機器」**。HA 的 instance id 存在 `.storage`
   裡，**還原備份會把它一起帶走**；同一個映像複製出去的機器（本區網的
   192.168.2.28 與 .249 就是）uid 完全相同。所以 uid 只能擋「兩套獨立安裝」，
   複製映像那一類要靠 `connector_count`。兩層都需要，缺一層就有洞。

## 守線

- repo-lint／pattern-guard：沒有機械 gate。`src/models/tunnel_binding_mixin.py`
  屬 `model-write-guard` class 的鄰居但不在 pattern 內（它不是 `smart_home*` /
  `security_access*` 檔名）——改它的人**不會**被 hook 推播必讀文件。
- `src/tests/test_tunnel_binding_1766.py` 釘住狀態機、端點 409、以及「機器數要數
  distinct client_id」。其中 `test_guard_holds_for_superuser_too` 是防止之後有人用
  `env.su` 豁免「修」這個 guard（TransactionCase 是 su env ⇒ 會假綠，見
  `su-env-exempt-guard-false-green`）。
- 元件端（`ha-components/woow_paas_smart_home`）的 `tests/test_instance_binding.py`
  釘住本機守線與「收到 409 不准退回快取權杖」。

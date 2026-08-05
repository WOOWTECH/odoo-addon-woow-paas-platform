# project 427 operator ingress baseline

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_427_operator_ingress_baseline`。盤點依據見 [memory-inventory-2026-08-05](../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description："#427 控制面 ingress 白名單最小版上 prod——per-chart egress 對第三方 chart 結構上不成立，只能從被連的那端擋"

**#427 最小版已上 stg＋prod**（CD PR#84/#86/#87，2026-08-02；**issue 保持 open**，原範圍是完整 default-deny）。

**核心論證**：平台 spec §79 把 egress 收斂下放 per-chart——**這條契約對第三方 chart 結構上不成立**（n8n-2.0.1 不是自家 chart、不會有我們要的 NP，marketplace 每上架一個就重開一次洞）。自家 chart 收斂得再好也只證明「我們自己寫的會做」。⇒ 只能從**被連的那端**（控制面 ingress）擋，這層與 chart 無關、對所有租戶一體適用。

**Eugene 拍板**：只保護 operator（odoo 屬 fast-follow）／白名單三個平台 ns／**ns＋pod 標籤雙重限制**／stg 先行／出事刪 NP 回滾。

**兩個在動工前攔下的白名單漏列**（本 issue 最怕的失效形態，都是 interview／偵察階段抓到）：
1. **stg 平台 Odoo 住在 `dev-sandbox-stg` 不是 `paas-stg-system`**（後者只有 stg operator）——照最初表述寫會當場切斷 stg 平台。
2. **`paas-api.woowtech.io`＝未版控的 prod 控制面公網入口**，走 **stg** tunnel 打 **prod** operator；tunnel 是 remotely-managed（規則在 Cloudflare 後台，叢集與 git 皆無痕跡，CD repo 248 commit 零命中）⇒ 查不到誰在用。Eugene 裁決切斷（實測 200→502），跨環境異常記 cd#85。

**三個派工假設被偵察推翻**：operator chart **沒有** networkPolicy/extraIngress values（載體改走獨立 raw-manifest Fleet bundle，與 #504 同構、不觸發 helm upgrade）；回滾「先 pause gitrepo」實測**非必要**（`correctDrift` 未啟用，delete 不會被 self-heal；且 GitRepo spec 無 `paused` key ⇒ JSON patch 要用 `add` 不能 `replace`）；NP 的 port 要寫 **8000**（容器埠，NP 是 post-DNAT，寫 service 的 80 會全擋）。

**驗收方法學**（比結果更值得留）：V2＝從零 egress NP 的第三方 chart 打控制面必須不通，這條才是真目標——原標準「open-design 打不通」在 chart 0.1.4 修好後**自動成立**，單靠它分不清「chart 修好」還是「控制面修好」。**回滾演練實跑**而非只寫進 PR（「沒跑過的指令寫進 PR 等於紙上談兵」），演練本身就修正了兩個判斷。**浸泡觀察真實業務流量**比人工 curl 硬。NP 實測正反兩向都要 long-lived pod／retry（iptables 程式化空窗，見 [[woow-k3s-netpol-fresh-pod-race]]；#504 備份 job 第 2 次才連上是同一成因）。teammate 自曝註解寫了沒做過的實測宣稱時，用**決定性對照實驗**（連不受本 NP 管轄的 prod odoo 也 000）排除自身歸因，比補圓場測試正確。

**剩餘未交付**：paas-odoo 的 ingress 仍是 from-less（n8n 實測仍打得到 prod odoo）、egress 方向未動、租戶 ns baseline egress → #514。交接觀察：podSelector 會連帶選中 pvc-cleanup Job pod（判斷無害但未實測，待 08-03 02:00 UTC 確認）。

**流程教訓**：teammate 見我卡選單 40 分鐘就自行去問 Eugene，且其選單推薦與已下裁決**相反**——同一題被問兩次會讓決策者留下矛盾紀錄。已改為「需 Eugene 裁決一律先回報 leader 彙整」。

**執行面細節（w2-netpol 補，下次動控制面 NP／Fleet bundle 直接用）**：

- **Fleet**：GitRepo `paas-app-bundle` 未設 `paths:` ⇒ 新增含 `fleet.yaml` 的頂層目錄即自動長出 bundle，不必改 CRD；**Bundle 實名 = `paas-app-bundle-{目錄}`（不含 `-cd`）**。`pollingInterval: 1m0s`。恢復被刪的資源＝`patch bundle ... forceSyncGeneration+1`，**5 秒生效**（實測）。兩個 YAML 全域避開連續大括號（[[reference_fleet_values_go_template_preprocess]]，cd#77 連註解都吃）。
- **kubelet probe 不受 NP 影響（本叢集實測）**：probe 來源是各 node 的 **cni0 bridge IP `10.42.x.1`**，不是 node IP `192.168.10.x`。白名單不含 node 網段，stg/prod 兩邊 probe 皆照常通過、endpoints 未清空 ⇒ 不需放行 node `/32`。事前反證：`dev-sandbox-stg` 早有 `podSelector: {}` default-deny 且沒放行 node 網段，其 probe 已活 61 天。
- **被 NP 擋的表現是 `ECONNREFUSED` 不是 timeout**（k3s 內建 netpol controller 用 REJECT），與 #425 收斂 prod pg 後同型。
- **prod Odoo 的 `operator_url` 走叢集內 DNS**（Eugene 2026-08-02 確認；該值是 `ir.config_parameter`、不在版控、`values-prod.yaml` 沒有）——這是 prod 白名單成立的關鍵前提，若填公網 URL 會把 Odoo 自己擋掉。
- **prod NP 第三條（`dev-sandbox-stg`）是無作用條目**：它自己的 `stg-odoo-dev-sandbox-allow-internal`（61 天前）egress 白名單沒有 `paas-system`、後者也沒有 `paas-managed=true`，ClusterIP `10.43.x` 又落在 `except 10.0.0.0/8` 內 ⇒ 到整個 `paas-system` 的出向早就不通。規則本身正確、依拍板保留。
- **浸泡要雙向正證據**：不只證明「擋掉的那側沒進來」（stg cloudflared `10.42.2.222` 在 operator log 中 0 筆），同時證明「放行的那側確實進來了」（prod odoo pod `10.42.0.109` 的請求成功抵達），再用公網 502 互相印證——排除「湊巧沒人打」這個替代解釋。

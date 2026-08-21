# project 449 reset carrier landing

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_449_reset_carrier_landing`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#449 reset-config 依密碼載體分流收官：PR#455 merged；F-449-1 stg E2E PASS（PR#459）衍生 #460/#461；prod 兩線未發"

2026-07-28 #449 收官（teammate reset-saga-449，PR #455 merged → develop acefe088，issue 自動關閉）。

- 拍板「1+3 合體」：template 加 `admin_password_carrier`（config_json 預設／helm_secret＝hermes-agent、open-design），平台 request 帶策略，operator 分流；兩條 reset 路都補 `_ensure_image_pull_secrets`（收 #448 殘留 gap），`reset_values` 一併接通（#411 A 案）。
- 關鍵事實（chart 實查）：hermes-agent／open-design 的 Deployment **沒有 checksum/secret annotation**——helm upgrade 改 Secret 不滾 pod，必須顯式 rollout restart，否則「成功但密碼靜默不生效」；兩 chart 都是 daemon＋console 雙 Deployment 共用 instance label → operator 新增複數版 `get_deployment_names` primitive。
- 待辦（follow-ups 已收錄）：F-449-1 stg 真機 E2E（integration 用合成最小 chart，真 chart 的 htpasswd 重算／120s client 預算未實測）；F-449-2 發版順序硬約束＝**operator 先發、平台後發**（錯序不會壞但「看起來沒修好」；平台發版前實查 operator image 已含 5221aae4）。見 [[reference-repo-two-release-trains]]。
- 本機 integration 測試新雷：Homebrew helm 已是 v4（--post-renderer 改吃 plugin 名）→ 全套 install 期 500；解法（抓 operator Dockerfile 釘的 3.13.3）已寫進 extra/paas-operator/tests/integration/README.md。
- 關聯 [[project-424-item1-hermes-agent-cs-landing]]。

## F-449-1 收口（2026-07-29，stg 真機 E2E PASS）

- 發版：stg operator=`acefe08`（CD PR#67，digest 75f0b591 三方閉環；date tag run 1234 只是同 digest 別名）；stg 平台=`paas-odoo-stg:20260728.1632`（develop=acefe088，stg-upgrade-449 job 45s，nodeSelector 綁同 node 避 RWO Multi-Attach）。**prod operator promote 已完成（07-29，CD PR#68）**：兩顆 pod acefe08@75f0b591 三方閉環、/health 200、stg 未擾動、回滾=pin 回 8c11a77；平台 prod 側仍待 manifest bump（#455 未 bump，仍 .80），屆時 pre-flight 的 operator 檢查已滿足（prod 已含 5221aae4）。
- E2E：hermes 四入口＋open-design 三入口全換新密碼、舊密碼全 401、無 rollback、RESTARTS 0；`--reset-values`／pvc_keep post-renderer／create-or-write 兩分支皆真機實證。報告 `docs/tracking/test-reports/449-password-carrier-stg-e2e-2026-07-29.md`（PR#459 merged）。
- 主發現①（→issue #461）：**首次 reset 機率性逾時**（120.6s/121.0s→重按 49.9s/76.7s）＝RWO detach/attach＋跨節點 image pull；fail-safe 實證成立；根因＝平台 120s 與 operator 最壞 540s 是互不知情的獨立預算。
- 主發現②（→issue #460，high）：**前端 Reset 按鈕仍 slug 白名單**（ServiceDetailPage.js:174），has_admin_gui 8 筆有 5 筆按不到（#375 三型起就漏）；修法雙邊＝payload 補 has_admin_gui＋前端改讀＋守線。
- 環境遺留：paas-ws-stg-5a105e8b quota limits.cpu 留 12（#456 已留言；翻回=服務起不來）；tmpl46 重解封交 leader1；svc60 密碼已輪替為平台 32 字元制式（DB 與 ns Secret 不一致時以 Secret 為準＝發生過 timeout）。
- 附帶缺陷（#424 領域，**已隨 chart 0.1.3 消滅，語意並已翻轉**）：2026-07-29 當時 dashboard 掛在 `/dashboard/` subpath，其前端 fetch('/auth/password-login') 漏 `/dashboard` 前綴＝真瀏覽器表單登入壞。**0.1.3 起 nginx `/` 直連 dashboard（webui container 移除、`/dashboard/` 改為硬 404），root 的 `/auth/password-login` 才是對的**——本條不再成立，勿照當年的值去「補前綴」（見 424-item1 landing 的「0.1.3 transition」節與 #672）。

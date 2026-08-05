# project ha woowtech image chart 021 landing

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_ha_woowtech_image_chart_021_landing`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description：HA PaaS 切 woowtech-ha image＋chart 0.2.1 — chart 已發佈、stg 租戶已遷移驗證、平台 seed commit 待 push

2026-07-23。HA PaaS（`cloud_app_ha_paas`）自 stopgap（chart 0.1.0 + HA 官方 image）切到 **woowtech-ha image + chart 0.2.1**。

## 已完成

- **chart 0.2.1 已發佈**：`oci://jcr-prod.woowtech.io/woow-paas-docker-local/home-assistant:0.2.1`（PR woow-paas-charts#56 merged，merge commit `2cbcfd9`）。直接 `helm show chart` 驗證可拉取，非憑 CI 綠燈。
  - 0.2.1 = 修 0.2.0 的 seed 阻斷缺陷，見 [[reference_busybox_cp_rn_src_dot_zero_copy]]。**0.2.0 不可用、勿回退**。
- **stg 真租戶 `svc-a66927a6`（paas-ws-stg-9630836d）已遷移**：chart 0.2.1 revision 3、woowtech-ha image、9 個 custom_components、`woow_provision` 首開機供裝器有跑、帶 XFF 從 pod 打回 **302**（load-bearing 測試，peer IP ∈ 10.42.0.0/16）。
- **stg Odoo template row 已同步**（`woow_stg` DB，`slug='ha-paas'` → 0.2.1 + 新 values）。舊值備份在該 session scratchpad `ha-migration-backup/stg-template-before.txt`。
- **孤兒 release `svc-f574867b` 已刪**（無 CloudService 紀錄、無使用者資料）：走 operator DELETE → PVC 因 `resource-policy: keep` 保留 → 確認 orphan 標記只落在該 PVC（真租戶未被誤標）後手動刪 PVC，PV 已回收。
- **0.2.1 全新安裝路徑真叢集驗證通過**：拋棄式 release `smoke021` 空 PVC 全新裝 → seed 逐檔迴圈複製成功、http 區塊追加、9 components、pod 1/1，驗完已清除。

## 未完成

- ~~平台 seed commit push~~ → 已 PR #408 merged、隨 v18.0.1.0.77 發版（commit `06449175`）。
- 平台 seed 的 Odoo 測試**未實跑**（僅靜態驗證 + repo-lint）。
- 🔴 **平台全路徑 provision e2e＝FAIL（2026-07-23 實跑）**：`ha_paas.py:_create_ha_paas_service` **從不寫 `helm_values`**（新舊 HA service DB 皆 NULL），`_execute_deploy` 從 `{}` 起手只帶注入值 → template 的 image pin 沒進 helm → pod 拉 chart 佔位 tag `00000000.0000` Init:ErrImagePull。0.1.x 沒炸＝chart 預設自足的僥倖；07-13 provision PASS 跑在 chart 預設非平台 pin 上。修法＝比照 marketplace 寫入 helm_values——**✅ 全案閉環**：PR #414 merged（develop e9fed145、.78）＋stg 已升 .78（stg-20260723.1113 build→upgrade job→roll）＋真部署 code 第三輪 e2e 全綠（202→pin→83 秒 running→status ready）；refactor 追蹤=#415（兩套建立邏輯收斂，另有 allocated_* 同款漏鏡射實證）。e2e recipe：odoo shell 建 portal user + oauth_token（scope `ha:provision`、client woow-ha-app）→ pod 內 curl POST /api/ha-paas/provision → 202 → 追 CS row 拿 helm_namespace。測試殘留已清（unlink 聯動 cancel 訂閱、token revoke、ns 刪除 NS_GONE）。

## 關鍵不變量

- **chart 版本與 image pin 必須同一次變更一起動**（chart NOTES.txt OPERATOR COUPLING）：只升 chart 不換 image → 全新開通卡 Init:Error；只換 image 不升 chart → 0.1.x 空 PVC 遮蔽烤入的 custom_components → 空殼 HA。守線測試 `src/tests/test_ha_paas_seed_woowtech_image.py::test_chart_and_image_move_together`。
- `haProxy.trustedProxies` = woow-k3s 真實 Pod CIDR `10.42.0.0/16`（4 node podCIDR 皆 10.42.X.0/24）。**append-if-absent＝首次 provision 限定**，helm upgrade 不會重跑，設錯就永久 400。見 [[reference_ha_trusted_proxies_peer_ip_based]]。
- seed 刻意**不覆寫** `nodeSelector`（chart 預設 amd64；image 僅 amd64，helm deep-merge 用 `{}` 蓋不掉、要清得寫 `null`）、`imagePullSecrets`（chart 預設 paas-registry，operator `releases.py:64` 另有 setdefault）、`persistence.storageClass`（平台 `_inject_storage_class` 注入）。
- 0.1.x → 0.2.x 遷移路徑：PVC 上的 `configuration.yaml` 含 `default_config:`（woowtech image 已 strip 那些整合）→ 直接換 image 會 setup error。正解是**刪除/改名該檔後重啟**讓種子重種（不能只刪 `default_config:` 那行，`cp -rn` 不覆蓋、烤入設定永遠落不了地），另需清 `.storage` 殘留 config entries。實務上若無使用者資料，**整個清空 PVC** 最乾淨（等同 fresh install，而 fresh 是 chart 唯一保證的路徑）。

## 教訓

這次 stg 遷移**意外成為 chart 0.2.0 的第一次真實部署**，當場爆出阻斷缺陷。若沒先動 stg，這個 bug 會等到平台 seed 上 prod、第一個真實使用者開通 HA 時才發現。**新 chart 版本上 prod 前，必須有一次真叢集的全新安裝 smoke。**

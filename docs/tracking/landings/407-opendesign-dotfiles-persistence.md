# project 407 opendesign dotfiles persistence

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_407_opendesign_dotfiles_persistence`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#407 open-design ~/.claude／~/.zshrc「重啟就不見」全案收官：破案＝刪除重建非重啟；chart 0.1.4 seed-home；PR #409 merged、#407 closed；衍生 #410/#411/#412"

2026-07-23 #407 全案（單日從回報到收尾）：

**破案**：回報「~/.claude／~/.zshrc 重啟就不見」實查＝**服務刪除重建**拿到新空白 od-home PVC（舊 svc-99502291 07-23 02:21 刪、新 svc-beab2427 02:22 建；舊 PVC 依 #317 keep 留 7 天 grace 可救）。pod 重啟持久化本來就有效（實證）。另一半真相：console ttyd＝`kubectl exec -- /bin/bash`（non-login shell）只讀 `~/.bashrc`，而 home PVC 蓋掉 image home、/etc/skel 不會補 → 連預設 .bashrc 都沒有；使用者寫 `~/.profile`／`~/.zshrc` 有存但不被讀（image 無 zsh、有 bash 5.2）。

**修復鏈（全 merged）**：charts#57（chart 0.1.4 seed-home initContainer：缺才補 .bashrc/.profile/.bash_logout、`[ -e ]` 逐檔守衛絕不覆蓋、每次啟動檢查、fail-soft exit 0 防 CrashLoop、uid 1001、daemon image 自身）→ 平台 PR #409 merged（develop 82f06303；seed 釘 0.1.4、manifest .77、test_chart_oci frozen-contract 同步）→ stg 真部署驗證 PASS（svc-beab2427 就地升 0.1.4、helm 3.13.3 顯式 -f、tenant 檔 md5＋奈秒 mtime 零變動、兩輪冪等、health 200）。#407 closed。

**衍生 issue**：#410（3 條 seed 數量快照斷言失效 251→267/34→42，#375/#380 後未同步）、#411（reuse_values 系統性缺陷，見 [[reference-helm-reuse-values-new-defaults-dropped]]）、#412（pending，殘餘 4 項：zsh 進 image、claude binary 更新策略、刪除重建語意 UI/文件明示、orphan home PVC 救援 SOP）。

**團隊**：Tiller（charts，chart+publish 盯場、tgz 解包驗發佈物）、Sprout（平台 seed PR）、Gauge（stg 驗證）；Tiller/Gauge 已 dismiss，Sprout 留到 #409 merge 後清 worktree。

**暴露窗**：stg 平台 DB 升 .77 前於 stg 新建 open-design 會裝 0.1.3（唯一 0.1.4 未覆蓋路徑）。

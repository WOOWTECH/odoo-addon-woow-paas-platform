# project haos chart 011 012 landing

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_haos_chart_011_012_landing`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description：charts/haos 0.1.1（CDI secretRef 誤用）＋0.1.2（XFF 剝除 proxy）兩版收官座標與待辦；2026-07-30

2026-07-30，`woow-paas-charts` 連出兩版並發佈（兩通道皆已驗產物）。

| 版本 | 內容 | PR | OCI digest |
| --- | --- | --- | --- |
| 0.1.1 | DataVolume 不再把 dockerconfigjson 塞進 CDI `registry.secretRef` | #63 | `sha256:a33a7957d26fea336669eb3064266f1972c94a78735e284cdfe990d8a335911a` |
| 0.1.2 | XFF 剝除 nginx proxy（#479） | #64 | `sha256:d1889be08fcf04e7ab886b3eec4b3157e1e4df9bd7982dce8b64742bf54e6ea9` |

（0.1.0 = `sha256:1dbb65eb…`；三顆 digest 互異，非別名。設計理由已完整寫在 `charts/haos/Chart.yaml` 的 changelog 註解，不在此重述。）

## 未結的事（會活過本 session）

- **端到端 302 尚未驗**。原 merge gate 是「外部→tunnel→nginx 的 302 實證」，但那會死鎖（正式端到端需 0.1.2 先上架），改為併入 #423 **W6 正式重跑**驗；有問題出 0.1.3。
- **v1.1 正解是刪掉這個元件**：#479 方向 (a) — 把 `trusted_proxies` 烤進 golden image。0.1.2 的 nginx 是 v1 取捨，代價是 **HA 看不到真實 client IP**（HA 內任何 IP 相關功能失效）。
- **平台 quota 需 +50m CPU / +32Mi memory**（`requests.*` 與 `limits.*` 兩側都算）——proxy pod 的帳。leader2 記入 W6.6 驗證項。

## 驗收手法（本輪最強的一項，之後照抄）

發佈後**把產物從 registry 拉回來、直接對它跑整套 chart 測試腳本**（`helm pull` → 解開 → 把 branch 的 `tests/*.sh` 複製進去跑）。驗的是租戶實際會安裝的那份 chart，不是本機工作樹；比「查 registry 有沒有這個版本」強一個量級。搭配 [[reference_ci_rebuild_same_commit_identical_digest]] 的「硬驗版號要從產物讀」同一精神。

## 本輪踩到的三個坑

1. [[reference_upstream_facts_git_grep_origin_ref]] — 拿工作樹當上游現況（最嚴重，錯誤前提滲進會發佈的 Chart.yaml changelog）。
2. **grep 打到「文件在描述這個錯」的那些行**：conf 註解裡引述 `worker_processes auto`、CI log 裡被 echo 的 `>>> FAILED`，都會讓負控假紅／假綠。斷言的輸入要先剝註解／排除 script 原文。
3. **`grep -c` 對空輸入也回 0** — 負控回 0 之前必須先有正控證明輸入非空。

nginx conf 兩個反直覺的實測結論（已編成斷言，勿照範本改回去）：canonical 的 `map $http_upgrade $connection_upgrade` 會把 proxy 整個卡死（連 pod 內自打 127.0.0.1 都 timeout），要用硬編 `Connection "Upgrade"`；`proxy_pass` 指回主 Service ＝自我迴圈，症狀是大量 499。

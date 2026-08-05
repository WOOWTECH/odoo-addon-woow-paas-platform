# reference bitnami legacy image move

> 自 Claude memory 遷移（2026-08-05）。原 slug `reference_bitnami_legacy_image_move`。盤點依據見 [memory-inventory-2026-08-05](../../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description：Bitnami community image 2025-08 搬到 bitnamilegacy/ 並凍結，bitnami/* 參照會 pull 不到

2025-08 起 Bitnami 把 community/free container image 從 `docker.io/bitnami/*` 移到 `docker.io/bitnamilegacy/*`（唯讀封存）並停止更新，原 `bitnami/*` tag（尤其 `:latest`）會逐漸 pull 不到。任何 woow repo 的 chart/manifest 參照 `bitnami/<x>` 都受影響。

已知座標：
- Odoo base image：`bitnamilegacy/odoo:18.0.20250805-debian-12-r9`（paas-odoo / stg base，paas-odoo-ci 打包用；來源 woow-machines-operator `docs/process-log/10. paas-odoo/1. paas-odoo-plan.md`）。
- `woow_paas_platform` 本 repo 受影響點：`charts/odoo-dev-sandbox/templates/ttl-cronjob.yaml:60` 用 `bitnami/kubectl:latest`（與 sandbox 模式無關，凍結後 → TTL 自動清理 ImagePullBackOff 靜默失效、過期 sandbox 不被刪）。修法：pin `bitnamilegacy/kubectl:<tag>` 或換非 Bitnami（`rancher/kubectl` / `alpine/k8s`）+ 改固定 tag。

不受影響：`charts/odoo-dev-sandbox/values.yaml:23-24` 的 `odoo:18.0` 是 Docker 官方 Odoo image（非 Bitnami）；`odoo-configmap.yaml:25` 的 `/opt/bitnami/odoo/...` 只是 image 內檔案路徑非 image 參照。

關聯 [[k8s-sandbox-plugin-port]]（移植時 chart bundle 要順手去 Bitnami 依賴）。

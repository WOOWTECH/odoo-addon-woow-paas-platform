# Woow PaaS Platform

Odoo 18 addon module providing the foundation for a multi-tenant PaaS application.

## Features

- **Standalone OWL Application** - Independent frontend at `/woow` with hash-based routing
- **Configuration Settings** - System settings via `res.config.settings`

## Quick Start — Local Dev Sandbox

本機開發與測試走 **K8s Dev Sandbox**（`woow-dev` plugin），namespace 隔離、可跑測試、第三方相依自動掛載。
（舊的 docker-compose worktree 流程已退役；詳見 [CLAUDE.md](CLAUDE.md) §K8s Dev Sandbox。）

```bash
# 0. 一次性 onboarding（記錄執行期 env 到 ~/.sandbox/{project-key}/，worktree 共用；
#    之後 create/up 可不帶 --context）
/woow-dev:k8s-dev-sandbox onboard --repo "$(git rev-parse --show-toplevel)" --context docker-desktop

# 1. 建立 / 啟動當前 branch 的本機沙盒
/woow-dev:k8s-dev-sandbox up --context docker-desktop
#    或雲端 cluster：/woow-dev:k8s-dev-sandbox create --context woow-k3s [--ttl 48h]

# 2. 存取（沙盒名見 list / 建立輸出）
kubectl port-forward -n dev-sandbox-<slug> svc/<slug>-odoo-dev-sandbox-nginx 8080:80
open http://localhost:8080          # login: admin / admin
```

> 第一個第三方相依（`ai_base_gt` 等）由 `.sandbox.yml` 的 `extra_addons: ["${PAAS_CI_ADDONS_PATH}"]` 自動掛載；
> 路徑值由 `onboard` 收集寫進 profile（每台機器不同、不進 git）。

## Common Commands

```bash
/woow-dev:k8s-dev-sandbox list                                   # 列出沙盒
/woow-dev:k8s-dev-sandbox status [<sandbox-name>]                # 狀態
/woow-dev:k8s-dev-sandbox logs <sandbox-name> --service odoo -f  # 日誌
/woow-dev:k8s-dev-sandbox test <sandbox-name> [--module woow_paas_platform]  # 跑模組測試
/woow-dev:k8s-dev-sandbox extend <sandbox-name> --ttl 336h       # 延長 TTL
/woow-dev:k8s-dev-sandbox destroy <sandbox-name> --context docker-desktop    # 銷毀
```

> in-repo [woow-e2e-fixtures](.claude/skills/woow-e2e-fixtures/SKILL.md) 提供 woow 專屬 fixture／clone helper：
> 一般 E2E 使用合成 fixture `scripts/{e2e-seed,e2e-seed-reset-button}.sh`；需要 prod 資料形狀的彩排才依 [prod-clone 授權流程](docs/guides/deployment/prod-clone-neutralization-runbook.md)使用 `seed-from-prod.sh`＋`neutralize-clone.sh`。
> 生命週期與 test／extend 都在 plugin；測試通過後的 e2e seed 由 `.sandbox/post-test.sh` hook 接手，帶 `WOOW_E2E_SEED=1`。
>
> `seed-from-stg.sh` 已正式退役並刪除（[#1955](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1955)），不保留提示腳本；**prod clone 不能重現 stg 特有的資料或版本問題**。
> 舊流程僅供歷史查閱，不作為執行指引：`git show 686c1e2c:.claude/skills/woow-e2e-fixtures/scripts/seed-from-stg.sh`。

## Running Tests

```bash
# 沙盒內跑模組測試
/woow-dev:k8s-dev-sandbox test <sandbox-name> --module woow_paas_platform

# PaaS Operator 單元測試（mocked k8s）
cd extra/paas-operator && pytest tests/ -v --cov=src
```

## Documentation

See [CLAUDE.md](CLAUDE.md) for the comprehensive development guide（含 K8s Dev Sandbox、deploy-stg、發版流程、架構與 Known patterns）。

## Requirements

- `kubectl` 1.28+ + 一個 cluster context（本機 `docker-desktop` k8s，或 VPN 後的 `woow-k3s`）
- Helm 3.13+
- Git
- `woow-dev` plugin（`/woow-dev:k8s-dev-sandbox`）

## License

Proprietary - Woow PaaS Platform

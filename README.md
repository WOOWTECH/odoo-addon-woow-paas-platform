# Woow PaaS Platform

Odoo 18 addon module providing the foundation for a multi-tenant PaaS application.

## Features

- **Standalone OWL Application** - Independent frontend at `/woow` with hash-based routing
- **Configuration Settings** - System settings via `res.config.settings`
- **Menu Structure** - Root menu "Woow PaaS" ready for child items
- **Claude Code PM** - Project management integration

## Quick Start — Local Dev Sandbox

本機開發與測試走 **K8s Dev Sandbox**（`woowprd` plugin），namespace 隔離、可跑測試、第三方相依自動掛載。
（舊的 docker-compose worktree 流程已退役；詳見 [CLAUDE.md](CLAUDE.md) §K8s Dev Sandbox。）

```bash
# 0. 一次性 onboarding（記錄執行期 env 到 ~/.sandbox/{project-key}/，worktree 共用；
#    之後 create/up 可不帶 --context）
/woowprd:k8s-dev-sandbox onboard --repo "$(git rev-parse --show-toplevel)" --context docker-desktop

# 1. 建立 / 啟動當前 branch 的本機沙盒
/woowprd:k8s-dev-sandbox up --context docker-desktop
#    或雲端 cluster：/woowprd:k8s-dev-sandbox create --context woow-k3s [--ttl 48h]

# 2. 存取（沙盒名見 list / 建立輸出）
kubectl port-forward -n dev-sandbox-<slug> svc/<slug>-odoo-dev-sandbox-nginx 8080:80
open http://localhost:8080          # login: admin / admin
```

> 第一個第三方相依（`ai_base_gt` 等）由 `.sandbox.yml` 的 `extra_addons: ["${PAAS_CI_ADDONS_PATH}"]` 自動掛載；
> 路徑值由 `onboard` 收集寫進 profile（每台機器不同、不進 git）。

## Common Commands

```bash
/woowprd:k8s-dev-sandbox list                                   # 列出沙盒
/woowprd:k8s-dev-sandbox status [<sandbox-name>]                # 狀態
/woowprd:k8s-dev-sandbox logs <sandbox-name> --service odoo -f  # 日誌
/woowprd:k8s-dev-sandbox test <sandbox-name> [--module woow_paas_platform]  # 跑模組測試
/woowprd:k8s-dev-sandbox extend <sandbox-name> --ttl 336h       # 延長 TTL
/woowprd:k8s-dev-sandbox destroy <sandbox-name> --context docker-desktop    # 銷毀
```

> 沒有 `woowprd` plugin 時，in-repo 保留 woow 專屬 helper：
> `./.claude/skills/k8s-dev-sandbox/scripts/{test,extend,e2e-seed,seed-from-stg}.sh`（完整生命週期已移至 plugin）。

## Running Tests

```bash
# 沙盒內跑模組測試
/woowprd:k8s-dev-sandbox test <sandbox-name> --module woow_paas_platform

# PaaS Operator 單元測試（mocked k8s）
cd extra/paas-operator && pytest tests/ -v --cov=src
```

## Documentation

See [CLAUDE.md](CLAUDE.md) for the comprehensive development guide（含 K8s Dev Sandbox、deploy-stg、發版流程、架構與 Known patterns）。

## Requirements

- `kubectl` 1.28+ + 一個 cluster context（本機 `docker-desktop` k8s，或 VPN 後的 `woow-k3s`）
- Helm 3.13+
- Git
- `woowprd` plugin（`/woowprd:k8s-dev-sandbox`）

## License

Proprietary - Woow PaaS Platform

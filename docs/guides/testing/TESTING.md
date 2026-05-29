# Testing Guide

This document provides instructions for running tests in the woow_paas_platform module.

## Test Structure

測試持續隨 cycle 增長，**不在此處逐檔列舉**（避免雙寫漂移）。權威清單請直接看目錄：

- Odoo module tests：`src/tests/`（`ls src/tests/test_*.py`）
- PaaS Operator tests：`extra/paas-operator/tests/`（含 `tests/integration/` 真 K8s 整合測試）

以代表性分組速覽涵蓋面（檔名前綴，非完整清單）：

| 分組 | 檔名前綴範例 | 範圍 |
|------|------------|------|
| cloud-service | `test_cloud_service*.py` | CloudService model / lifecycle / ACL |
| ACL | `test_*_acl*.py`、`test_acl_*.py` | ir.rule / endpoint 跨租戶隔離 |
| agent-pod-phase | `test_agent_pod_phase_*.py` | agent-runtime Phase A/C/D schema·wiring·hardening |
| mcp | `test_mcp_*.py` | MCP server/tool decouple、sidecar、URL redesign |
| integration | `extra/paas-operator/tests/integration/` | 真 K8s 契約測試（unit mock 抓不到的 drift）|

## Running PaaS Operator Tests

### Prerequisites

1. Install Python dependencies:
   ```bash
   cd extra/paas-operator
   pip install -r requirements.txt
   ```

### Run All Tests

```bash
cd extra/paas-operator
pytest tests/ -v
```

### Run with Coverage

```bash
cd extra/paas-operator
pytest tests/ -v --cov=src --cov-report=term-missing
```

### Run Specific Test File

```bash
pytest tests/test_helm.py -v
pytest tests/test_api.py -v
```

### Run Specific Test

```bash
pytest tests/test_api.py::TestFullDeploymentFlow::test_full_deployment_flow -v
```

## Running Odoo Module Tests

### Prerequisites

1. Ensure Odoo is installed and configured
2. Module must be installed in your Odoo instance

### Run All Module Tests

From the odoo-bin directory:

```bash
./odoo-bin -c odoo.conf --test-enable --test-tags woow_paas_platform --stop-after-init
```

### Run in Docker

If using Docker development environment:

```bash
# 使用 Worktree Development 腳本（推薦）
./scripts/test-addon.sh

# 或手動執行（需先啟動開發環境）
docker compose exec web \
  odoo -d ${POSTGRES_DB:-odoo} --test-enable --test-tags woow_paas_platform --stop-after-init
```

### Run Specific Test Class

```bash
./odoo-bin -c odoo.conf --test-enable \
  --test-tags woow_paas_platform.test_cloud_app_template:TestCloudAppTemplate \
  --stop-after-init
```

### Watch Logs During Tests

```bash
tail -f odoo.log | grep -E '(TEST|ERROR|FAIL)'
```

## Test Coverage

覆蓋面隨 cycle 持續擴張，**不逐檔列舉**（同 Test Structure 原則）。各檔內以 docstring / class 名描述其涵蓋範圍，請直接看原始碼。下方按代表性分組說明高層涵蓋意圖：

### PaaS Operator Tests（`extra/paas-operator/tests/`）

- Helm / Kubernetes service：namespace 驗證、command 執行（success/failure/timeout）、install / get / uninstall / history / version、pod status、quota namespace 建立
- FastAPI 端點：health、release CRUD、API key 認證、完整部署生命週期（namespace → install → status）、upgrade / rollback / delete
- agent-runtime / auth / scale / tunnels：spawn-reap、`token_use` fail-closed、scale、tunnel 端點
- integration（`tests/integration/`）：真 K8s 契約測試，抓 unit mock 遮蔽的 workload-type / HTTP contract / RBAC drift

### Odoo Module Tests（`src/tests/`）

- cloud-service：CloudService 建立、state transition（pending → deploying → running）、error state、helm / network / resource 配置、workspace 關係、cascade 刪除、revision
- API / controller：template listing·search、service CRUD、workspace isolation、helm values filtering、subdomain/reference_id 唯一性、PaaSOperatorClient HTTP 方法與錯誤處理
- ACL / 跨租戶隔離：ir.rule + endpoint 雙層守線（見 `docs/decisions/patterns/`）
- agent-pod-phase / mcp：Phase A/C/D schema·wiring·hardening 與 MCP decouple、sidecar、URL redesign

## Expected Test Results

### PaaS Operator

All tests use mocks, so they should pass without actual Kubernetes/Helm:

```
tests/test_helm.py::TestHelmService::test_validate_namespace_valid PASSED
tests/test_helm.py::TestHelmService::test_validate_namespace_invalid PASSED
tests/test_helm.py::TestHelmService::test_run_command_success PASSED
tests/test_helm.py::TestHelmService::test_run_command_failure PASSED
tests/test_helm.py::TestHelmService::test_run_command_timeout PASSED
tests/test_helm.py::TestHelmService::test_install_success PASSED
tests/test_helm.py::TestHelmService::test_install_invalid_namespace PASSED
...

tests/test_api.py::TestHealthEndpoint::test_health_check_healthy PASSED
tests/test_api.py::TestReleaseEndpoints::test_create_release PASSED
tests/test_api.py::TestFullDeploymentFlow::test_full_deployment_flow PASSED
tests/test_api.py::TestFullDeploymentFlow::test_upgrade_flow PASSED
tests/test_api.py::TestFullDeploymentFlow::test_rollback_flow PASSED
tests/test_api.py::TestFullDeploymentFlow::test_delete_flow PASSED
...

==================== XX passed in X.XXs ====================
```

### Odoo Module

Tests run against a test database:

```
odoo.tests.test_cloud_app_template.TestCloudAppTemplate.test_create_template_minimal ... ok
odoo.tests.test_cloud_app_template.TestCloudAppTemplate.test_create_template_with_category ... ok
odoo.tests.test_cloud_service.TestCloudService.test_create_service_minimal ... ok
odoo.tests.test_cloud_service.TestCloudService.test_service_state_transitions ... ok
odoo.tests.test_cloud_api.TestCloudAPI.test_get_templates_list ... ok
odoo.tests.test_cloud_api.TestCloudServiceController.test_helm_values_filtering ... ok
odoo.tests.test_cloud_api.TestCloudServiceController.test_service_subdomain_uniqueness ... ok
odoo.tests.test_paas_operator.TestPaaSOperatorClient.test_install_release_success ... ok
odoo.tests.test_paas_operator.TestPaaSOperatorClient.test_connection_error_handling ... ok
odoo.tests.test_paas_operator.TestGetPaaSOperatorClient.test_returns_client_when_configured ... ok
...

----------------------------------------------------------------------
Ran XX tests in X.XXXs

OK
```

## Troubleshooting

### PaaS Operator Tests Fail

1. **ImportError**: Ensure you're in the `extra/paas-operator` directory
2. **ModuleNotFoundError**: Run `pip install -r requirements.txt`
3. **Pytest not found**: Run `pip install pytest pytest-cov`

### Odoo Module Tests Fail

1. **Module not found**: Install module first: `./odoo-bin -c odoo.conf -u woow_paas_platform`
2. **Database errors**: Ensure test database is clean
3. **Import errors**: Check that `src/tests/` has `__init__.py`
4. **Security errors**: Verify `src/security/ir.model.access.csv` is up to date

### Docker Test Issues

1. **Container not running**: `./scripts/start-dev.sh`
2. **Module not installed**: `./scripts/test-addon.sh` 會自動更新模組
3. **Permission errors**: Check volume mounts

## Continuous Integration

For CI/CD pipelines:

```yaml
# Example GitHub Actions workflow
- name: Test PaaS Operator
  run: |
    cd extra/paas-operator
    pip install -r requirements.txt
    pytest tests/ --cov=src --cov-report=xml

- name: Test Odoo Module
  run: |
    ./odoo-bin -c odoo.conf --test-enable --test-tags woow_paas_platform --stop-after-init
```

## Code Coverage Goals

- **PaaS Operator**: Target >80% coverage
- **Odoo Models**: Target >70% coverage for CRUD operations
- **Odoo APIs**: Target >60% coverage for endpoint responses

## Next Steps

After running tests:

1. Review coverage report
2. Add tests for uncovered code paths
3. Update this document with new test patterns
4. Consider adding integration tests with real Kubernetes

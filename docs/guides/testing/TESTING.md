# Testing Guide

This document provides instructions for running tests in the woow_paas_platform module.

## Test Structure

```
woow_paas_platform/
├── src/tests/                       # Odoo module tests
│   ├── test_cloud_app_template.py  # CloudAppTemplate model tests
│   ├── test_cloud_service.py       # CloudService model tests
│   ├── test_cloud_api.py           # API endpoint & controller tests
│   └── test_paas_operator.py       # PaaSOperatorClient HTTP client tests
└── extra/paas-operator/tests/      # PaaS Operator service tests
    ├── test_helm.py                # Helm service tests
    └── test_api.py                 # FastAPI endpoint tests
```

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

### PaaS Operator Tests

**test_helm.py** covers:
- Namespace validation (valid and invalid prefixes)
- Helm command execution (success, failure, timeout)
- Install operations with validation
- Get release information
- Uninstall operations
- Release history
- Helm version retrieval
- Kubernetes pod status
- Namespace creation with quotas

**test_api.py** covers:
- Health check endpoints
- Release CRUD operations
- Authentication with API keys
- Full deployment lifecycle (namespace → install → status)
- Upgrade flow
- Rollback flow
- Delete/cleanup flow

### Odoo Module Tests

**test_cloud_app_template.py** covers:
- Template creation with minimal/full fields
- Category selections
- Resource requirements
- Default values
- Template activation/deactivation
- Chart version updates
- Search and filtering

**test_cloud_service.py** covers:
- Service creation
- State transitions (pending → deploying → running)
- Error state handling
- Helm configuration
- Network configuration
- Resource allocation
- Workspace relationships
- Cascade deletion
- Timestamp fields
- Revision management

**test_cloud_api.py** covers:
- Template listing and filtering
- Template search
- Service creation
- Service listing
- Service state updates
- Workspace isolation
- Error handling for non-existent resources
- Helm values filtering based on template specs
- Subdomain/reference_id uniqueness constraints
- Service state transitions
- Workspace member management

**test_paas_operator.py** covers:
- PaaSOperatorClient initialization
- All HTTP client methods (health_check, install_release, upgrade_release, etc.)
- Error handling (connection errors, timeouts, API errors)
- HTTP status code handling (400, 404, 500)
- Empty response handling
- `get_paas_operator_client` helper function
- Configuration from Odoo settings

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

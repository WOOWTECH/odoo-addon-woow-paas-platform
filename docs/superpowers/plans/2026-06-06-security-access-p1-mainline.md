# Security Access P1 主線 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 Security Access 訂閱服務主線：訂閱 → Cloudflare tunnel → 多組 application route → 503 凍結/恢復 → 刪除清理全鏈，含後台管理、前端三 tab、HA component API 與串接文件。

**Architecture:** SA 是 CloudService/SmartHome 之後的第三個平行 service model，繼承 `subscription_lifecycle_mixin` 拿到完整訂閱/計費引擎；所有 Cloudflare 操作經 paas-operator（新增唯一一個宣告式 endpoint `PUT /api/tunnels/{id}/routes` 做 ingress 整份同步 + DNS reconcile）；前端複用 PlanSelector/SkipTrialModal/StatusBadge 與 workspace-scoped 路由。

**Tech Stack:** Odoo 18（Python, OWL, QWeb, SCSS）、FastAPI（paas-operator）、Cloudflare API（tunnel/DNS）、sh_subscription 訂閱引擎。

**Spec:** `docs/superpowers/specs/2026-06-06-security-access-subscription-design.md`（P1 範圍見 §7 分期表）

**P1 不含**（後續 phase）：Access Control（P2）、Custom Domains（P3）、Email Routing（P4）。前端 Access/Email/Domains 三 tab 以 Coming Soon 佔位。

**工作環境：** 在本 Floor（branch `feat/security-access`）執行；平行 subagent 需各自開 git worktree 子分支（superpowers:using-git-worktrees），完成後併回 `feat/security-access`。

**Commit 規範：** 英文 message、`{type}: {message}` 格式（feat/fix/docs/test/chore...），**不加** AI attribution footer。

**測試指令說明：**
- Odoo 測試（在 odoo-server root，或用 k8s sandbox `./.claude/skills/k8s-dev-sandbox/scripts/test.sh <sandbox> --module woow_paas_platform`）：
  `./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform:<TestClass> --stop-after-init -u woow_paas_platform`
- Operator 測試：`cd extra/paas-operator && pytest tests/<file> -v`

---

## File Structure（全貌）

```
新增：
  src/models/security_access.py                  # 主模型 + route 子模型（單檔，鏡像 smart_home.py 規模）
  src/models/security_access_template.py         # template（鏡像 smart_home_template.py）
  src/models/sh_subscription_plan_sa_quota.py    # sh.subscription.plan 加 4 個 SA quota 欄位
  src/controllers/security_access.py             # BFF controller（鏡像 smart_home.py）
  src/views/security_access_views.xml            # 後台 list/form + menu
  src/views/security_access_template_views.xml   # template 後台 views + menu
  src/data/security_access_templates.xml         # template seed
  src/tests/test_security_access.py              # model 單元測試
  src/tests/test_security_access_template.py     # template + quota 測試
  src/tests/test_security_access_subscription_flow.py  # controller 訂閱 flow 測試
  src/tests/test_security_access_lifecycle.py    # cron suspend/resume/cleanup 測試
  src/tests/test_security_access_endpoints.py    # HttpCase cross-user negative
  src/tests/test_ha_api_security_access.py       # HA API + scope 測試
  src/static/src/paas/services/security_access.js
  src/static/src/paas/pages/security-access/SecurityAccessDetailPage.js
  src/static/src/paas/pages/security-access/SecurityAccessDetailPage.xml
  src/static/src/paas/pages/security-access/SecurityAccessDetailPage.scss
  src/static/src/paas/components/modal/CreateSecurityAccessModal.js
  src/static/src/paas/components/modal/CreateSecurityAccessModal.xml
  extra/paas-operator/tests/test_tunnel_routes_sync.py
  extra/paas-operator/tests/test_route_availability.py
  docs/reference/api/security-access-ha-integration.md

修改：
  src/services/naming.py                         # + make_security_access_subdomain()
  src/services/paas_operator.py                  # + sync_tunnel_routes() / check_route_availability()
  src/models/__init__.py                         # + 3 個 import
  src/models/subscription_lifecycle_cron.py      # 掃描清單 + SA（漏加 = 訂閱永不被強制執行）
  src/models/smart_home_template.py              # cross-type constraint 加查 SA template（對稱鎖）
  src/models/cloud_app_template.py               # 同上
  src/models/res_config_settings.py              # + woow_sa_default_service_url
  src/views/res_config_settings_views.xml        # + Security Access 設定區塊
  src/hooks.py                                   # _CONFIG_PARAMETERS + post_init scope 確保
  src/controllers/__init__.py                    # + security_access import
  src/controllers/ha_api.py                      # + /api/security-access/* 5 endpoints
  src/security/ir.model.access.csv               # + 6 行
  src/security/ir_rules.xml                      # + SA 對稱 ir.rule（noupdate=0 區）
  src/data/subscription_products.xml             # + 4 plans + 4 products
  src/data/subscription_template_links.xml       # + SA template link
  src/__manifest__.py                            # data 清單 + 3 個 XML
  src/tests/__init__.py                          # + 6 個 import
  src/tests/test_naming.py                       # + SA naming 測試
  src/static/src/paas/core/router.js             # + 2 條路由
  src/static/src/paas/pages/workspace/WorkspaceDetailPage.js   # + SA 區塊邏輯
  src/static/src/paas/pages/workspace/WorkspaceDetailPage.xml  # + SA list 區塊 + Explore 卡接線
  extra/paas-operator/src/services/cloudflare.py # + configure_tunnel_ingress() / reconcile_tunnel_dns()
  extra/paas-operator/src/api/tunnels.py         # + PUT /{tunnel_id}/routes
  extra/paas-operator/src/api/routes.py          # + GET /availability
  extra/paas-operator/src/models/schemas.py      # + 3 個 schema
```

**任務依賴**：Task 1-5（operator + client 層）與 Task 6-7（plan quota + template）可平行；Task 8-10（model 主體）依賴 1、5、6、7；Task 11-12 依賴 8；Task 13-16 依賴 8-12；Task 17-18（前端）依賴 13-14；Task 19 依賴 16；Task 20 收尾。

---

### Task 1: naming.py — `make_security_access_subdomain()`

**Files:**
- Modify: `src/services/naming.py`
- Test: `src/tests/test_naming.py`

- [ ] **Step 1: 寫失敗測試** — 在 `src/tests/test_naming.py` 末尾加入（先讀檔案開頭確認既有 import 與 class 結構，加進既有 TestCase class 或新 class）：

```python
class TestSecurityAccessNaming(TransactionCase):
    """make_security_access_subdomain: paas-sa-{ws_hash}-{res_hash}."""

    def test_sa_subdomain_format(self):
        from odoo.addons.woow_paas_platform.services.naming import (
            make_security_access_subdomain,
            generate_ws_hash,
            generate_resource_hash,
        )
        result = make_security_access_subdomain('my-ws', 'ref-123', 'My SA')
        self.assertEqual(
            result,
            f"paas-sa-{generate_ws_hash('my-ws')}-{generate_resource_hash('ref-123', 'My SA')}",
        )
        self.assertTrue(result.startswith('paas-sa-'))
        # 8+8 hex + 前綴 + 連字號
        self.assertEqual(len(result), len('paas-sa-') + 8 + 1 + 8)

    def test_sa_subdomain_deterministic(self):
        from odoo.addons.woow_paas_platform.services.naming import (
            make_security_access_subdomain,
        )
        a = make_security_access_subdomain('ws', 'r1', 'n1')
        b = make_security_access_subdomain('ws', 'r1', 'n1')
        self.assertEqual(a, b)

    def test_sa_prefix_distinct_from_sm_cs(self):
        from odoo.addons.woow_paas_platform.services.naming import (
            make_security_access_subdomain,
            make_smarthome_subdomain,
            make_service_subdomain,
        )
        sa = make_security_access_subdomain('ws', 'r', 'n')
        sm = make_smarthome_subdomain('ws', 'r', 'n')
        cs = make_service_subdomain('ws', 'r', 'n')
        # 同 hash、不同前綴 → 三型別 namespace 隔離
        self.assertNotEqual(sa, sm)
        self.assertNotEqual(sa, cs)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform:TestSecurityAccessNaming --stop-after-init -u woow_paas_platform`
Expected: FAIL — `ImportError: cannot import name 'make_security_access_subdomain'`

- [ ] **Step 3: 實作** — `src/services/naming.py` 末尾加：

```python
def make_security_access_subdomain(slug: str, reference_id: str, name: str) -> str:
    """Generate security access subdomain: paas-sa-{ws_hash}-{sa_hash}."""
    return f"paas-sa-{generate_ws_hash(slug)}-{generate_resource_hash(reference_id, name)}"
```

- [ ] **Step 4: 跑測試確認通過**（同 Step 2 指令）Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/services/naming.py src/tests/test_naming.py
git commit -m "feat: add security access subdomain naming helper"
```

---

### Task 2: Operator — `configure_tunnel_ingress()`（多 route ingress 整份替換）

現有 `configure_tunnel()` 只能表達「單 hostname + catch-all 404」，SA 多 route 需要吃規則清單的版本。**不動既有方法**（SmartHome 在用），新增平行方法。

**Files:**
- Modify: `extra/paas-operator/src/services/cloudflare.py`
- Modify: `extra/paas-operator/src/models/schemas.py`
- Test: `extra/paas-operator/tests/test_tunnel_routes_sync.py`（先建檔，本 task 只測 service 層）

- [ ] **Step 1: schemas** — `extra/paas-operator/src/models/schemas.py` 末尾加（先讀檔案確認 BaseModel import 風格一致）：

```python
class TunnelRouteSpec(BaseModel):
    """One desired ingress route on a dedicated tunnel."""

    subdomain: str = Field(..., description="Subdomain under the platform zone")
    service_url: str = Field(..., description="Backend service URL or http_status:NNN")
    http_host_header: bool = Field(
        True, description="Send the public hostname as HTTP Host header to origin"
    )


class TunnelRoutesSyncRequest(BaseModel):
    """Declarative full sync of a dedicated tunnel's routes."""

    routes: list[TunnelRouteSpec] = Field(..., min_length=1)
    comment: str | None = Field(
        None, description="Canonical name stamped on DNS records for traceability"
    )


class TunnelRouteSyncResult(BaseModel):
    subdomain: str
    hostname: str
    dns_record_id: str | None = None


class TunnelRoutesSyncResponse(BaseModel):
    tunnel_id: str
    routes: list[TunnelRouteSyncResult]
```

- [ ] **Step 2: 寫失敗測試** — 建 `extra/paas-operator/tests/test_tunnel_routes_sync.py`：

```python
"""Tests for multi-route ingress sync on dedicated tunnels."""
from unittest.mock import AsyncMock, patch

import pytest

from src.services.cloudflare import CloudflareService


@pytest.fixture
def cf_service():
    svc = CloudflareService()
    svc.api_token = "test-token"
    svc.account_id = "test-account"
    svc.zone_id = "test-zone"
    svc.domain = "woowtech.io"
    svc.dns_enabled = True
    return svc


class TestConfigureTunnelIngress:
    @pytest.mark.asyncio
    async def test_builds_rules_with_host_header_and_catchall(self, cf_service):
        """Each route becomes one ingress rule; httpHostHeader set when flagged; catch-all 404 last."""
        captured = {}

        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"success": True}

        async def fake_put(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["payload"] = json
            return FakeResponse()

        with patch("httpx.AsyncClient.put", new=AsyncMock(side_effect=fake_put)):
            await cf_service.configure_tunnel_ingress(
                tunnel_id="tun-1",
                rules=[
                    {"hostname": "a.woowtech.io", "service": "http://localhost:80",
                     "http_host_header": True},
                    {"hostname": "b.woowtech.io", "service": "http_status:503",
                     "http_host_header": False},
                ],
            )

        ingress = captured["payload"]["config"]["ingress"]
        assert len(ingress) == 3  # 2 routes + catch-all
        assert ingress[0]["hostname"] == "a.woowtech.io"
        assert ingress[0]["service"] == "http://localhost:80"
        assert ingress[0]["originRequest"] == {"httpHostHeader": "a.woowtech.io"}
        assert "originRequest" not in ingress[1]
        assert ingress[-1] == {"service": "http_status:404"}
        assert "tun-1/configurations" in captured["url"]

    @pytest.mark.asyncio
    async def test_raises_on_cf_error(self, cf_service):
        class FakeResponse:
            status_code = 400
            text = "bad"

            @staticmethod
            def json():
                return {"success": False}

        with patch("httpx.AsyncClient.put", new=AsyncMock(return_value=FakeResponse())):
            from src.services.cloudflare import CloudflareException
            with pytest.raises(CloudflareException):
                await cf_service.configure_tunnel_ingress(
                    tunnel_id="tun-1",
                    rules=[{"hostname": "a.woowtech.io",
                            "service": "http://localhost:80",
                            "http_host_header": True}],
                )
```

- [ ] **Step 3: 跑測試確認失敗**

Run: `cd extra/paas-operator && pytest tests/test_tunnel_routes_sync.py -v`
Expected: FAIL — `AttributeError: 'CloudflareService' object has no attribute 'configure_tunnel_ingress'`

- [ ] **Step 4: 實作** — `extra/paas-operator/src/services/cloudflare.py`，加在既有 `configure_tunnel()` 方法後面：

```python
    async def configure_tunnel_ingress(
        self, tunnel_id: str, rules: List[dict]
    ) -> None:
        """Replace a dedicated tunnel's full ingress rule list.

        Multi-route counterpart of ``configure_tunnel`` (which only expresses
        a single hostname). Each rule dict: {hostname, service,
        http_host_header}. A catch-all 404 rule is appended automatically.

        Args:
            tunnel_id: The tunnel ID.
            rules: Desired ingress rules, order preserved.

        Raises:
            CloudflareException: If configuration update fails.
        """
        url = f"{self._tunnels_url(tunnel_id)}/configurations"

        ingress_rules = []
        for rule in rules:
            entry = {"hostname": rule["hostname"], "service": rule["service"]}
            if rule.get("http_host_header"):
                entry["originRequest"] = {"httpHostHeader": rule["hostname"]}
            ingress_rules.append(entry)
        ingress_rules.append({"service": "http_status:404"})

        payload = {"config": {"ingress": ingress_rules}}

        logger.info(
            f"Configuring tunnel {tunnel_id} ingress with {len(rules)} route(s)"
        )

        async with httpx.AsyncClient() as client:
            response = await client.put(
                url,
                headers=self._headers,
                json=payload,
                timeout=30.0,
            )

            if response.status_code != 200:
                logger.error(
                    f"Failed to configure tunnel ingress: "
                    f"{response.status_code} - {response.text}"
                )
                raise CloudflareException(
                    f"Failed to configure tunnel ingress: {response.text}",
                    status_code=response.status_code,
                )

            data = response.json()
            if not data.get("success"):
                raise CloudflareException(
                    f"Cloudflare API error: {data.get('errors', 'Unknown error')}"
                )

            logger.info(f"Tunnel {tunnel_id} ingress configured successfully")
```

- [ ] **Step 5: 跑測試確認通過**（同 Step 3）Expected: 2 PASS

- [ ] **Step 6: Commit**

```bash
git add extra/paas-operator/src/services/cloudflare.py extra/paas-operator/src/models/schemas.py extra/paas-operator/tests/test_tunnel_routes_sync.py
git commit -m "feat: add multi-route tunnel ingress configuration to operator"
```

---

### Task 3: Operator — `reconcile_tunnel_dns()`（DNS 宣告式調和 + comment stamping）

**Files:**
- Modify: `extra/paas-operator/src/services/cloudflare.py`
- Test: `extra/paas-operator/tests/test_tunnel_routes_sync.py`（追加）

- [ ] **Step 1: 寫失敗測試** — 追加到 `test_tunnel_routes_sync.py`：

```python
class TestReconcileTunnelDns:
    @pytest.mark.asyncio
    async def test_creates_missing_deletes_extra(self, cf_service):
        """Desired={a,b}; existing={b,c} pointing at this tunnel → create a, keep b, delete c."""
        cname_target = cf_service._tunnel_cname_target_for("tun-1")

        existing_records = [
            {"id": "rec-b", "name": "b.woowtech.io", "content": cname_target},
            {"id": "rec-c", "name": "c.woowtech.io", "content": cname_target},
        ]

        with patch.object(cf_service, "_list_dns_records_by_content",
                          new=AsyncMock(return_value=existing_records)), \
             patch.object(cf_service, "create_dns_record_for_tunnel",
                          new=AsyncMock(return_value="rec-a")) as mock_create, \
             patch.object(cf_service, "_delete_dns_record_by_id",
                          new=AsyncMock(return_value=True)) as mock_delete:
            result = await cf_service.reconcile_tunnel_dns(
                tunnel_id="tun-1", subdomains=["a", "b"], comment="paas-sa-x-y",
            )

        mock_create.assert_called_once_with(
            subdomain="a", tunnel_id="tun-1", comment="paas-sa-x-y",
        )
        mock_delete.assert_called_once_with("rec-c")
        assert result == {"a": "rec-a", "b": "rec-b"}
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd extra/paas-operator && pytest tests/test_tunnel_routes_sync.py::TestReconcileTunnelDns -v`
Expected: FAIL — no attribute `reconcile_tunnel_dns`

- [ ] **Step 3: 實作** — `cloudflare.py` 三件事：

(a) `create_dns_record_for_tunnel` 加可選 `comment` 參數（stamping；放進 payload）。修改既有方法簽名與 payload：

```python
    async def create_dns_record_for_tunnel(
        self, subdomain: str, tunnel_id: str, comment: Optional[str] = None
    ) -> Optional[str]:
```

payload dict 加一行（在 `"proxied": True,` 之後）：

```python
        if comment:
            payload["comment"] = comment
```

注意：payload 目前是字面 dict，改為先建構再條件加 key：

```python
        payload = {
            "type": "CNAME",
            "name": subdomain,
            "content": cname_target,
            "ttl": 1,
            "proxied": True,
        }
        if comment:
            payload["comment"] = comment
```

(b) 新增兩個內部 helper（加在 `delete_dns_record` 方法後面）：

```python
    async def _list_dns_records_by_content(self, content: str) -> List[dict]:
        """List CNAME records in the zone whose content equals ``content``.

        Used to find every record pointing at a dedicated tunnel
        ({tunnel_id}.cfargotunnel.com) for declarative reconciliation.
        """
        async with httpx.AsyncClient() as client:
            response = await client.get(
                self._dns_records_url,
                headers=self._headers,
                params={"type": "CNAME", "content": content, "per_page": 100},
                timeout=30.0,
            )
            if response.status_code != 200:
                raise CloudflareException(
                    f"Failed to list DNS records: {response.text}",
                    status_code=response.status_code,
                )
            data = response.json()
            if not data.get("success"):
                raise CloudflareException(
                    f"Cloudflare API error: {data.get('errors', 'Unknown error')}"
                )
            return data.get("result", [])

    async def _delete_dns_record_by_id(self, record_id: str) -> bool:
        """Delete a DNS record by its Cloudflare record ID."""
        async with httpx.AsyncClient() as client:
            response = await client.delete(
                f"{self._dns_records_url}/{record_id}",
                headers=self._headers,
                timeout=30.0,
            )
            if response.status_code != 200:
                raise CloudflareException(
                    f"Failed to delete DNS record {record_id}: {response.text}",
                    status_code=response.status_code,
                )
            return True
```

(c) 新增 `reconcile_tunnel_dns`：

```python
    async def reconcile_tunnel_dns(
        self,
        tunnel_id: str,
        subdomains: List[str],
        comment: Optional[str] = None,
    ) -> Dict[str, Optional[str]]:
        """Make the zone's DNS records for a dedicated tunnel match ``subdomains``.

        Declarative: creates missing CNAMEs, keeps matching ones, deletes
        records that point at this tunnel but are no longer desired.
        Idempotent — safe to call repeatedly (suspend/resume don't change
        hostnames, so reconcile is a no-op there).

        Returns:
            Mapping of subdomain -> DNS record ID.
        """
        if not self.dns_enabled:
            logger.info("DNS management disabled, skipping reconcile")
            return {s: None for s in subdomains}

        cname_target = self._tunnel_cname_target_for(tunnel_id)
        existing = await self._list_dns_records_by_content(cname_target)
        existing_by_sub = {}
        for rec in existing:
            # rec["name"] is the FQDN; strip the zone domain suffix
            name = rec.get("name", "")
            if name.endswith(f".{self.domain}"):
                existing_by_sub[name[: -(len(self.domain) + 1)]] = rec

        desired = set(subdomains)
        result: Dict[str, Optional[str]] = {}

        for sub in subdomains:
            if sub in existing_by_sub:
                result[sub] = existing_by_sub[sub]["id"]
            else:
                result[sub] = await self.create_dns_record_for_tunnel(
                    subdomain=sub, tunnel_id=tunnel_id, comment=comment,
                )

        for sub, rec in existing_by_sub.items():
            if sub not in desired:
                logger.info(
                    f"Reconcile: deleting stale DNS record {rec['name']} "
                    f"(tunnel {tunnel_id})"
                )
                await self._delete_dns_record_by_id(rec["id"])

        return result
```

- [ ] **Step 4: 跑測試確認通過**

Run: `cd extra/paas-operator && pytest tests/test_tunnel_routes_sync.py -v`
Expected: 全 PASS（含 Task 2 的測試不回歸）

- [ ] **Step 5: Commit**

```bash
git add extra/paas-operator/src/services/cloudflare.py extra/paas-operator/tests/test_tunnel_routes_sync.py
git commit -m "feat: add declarative dns reconcile with comment stamping to operator"
```

---

### Task 4: Operator API — `PUT /api/tunnels/{id}/routes` + `GET /api/routes/availability`

**Files:**
- Modify: `extra/paas-operator/src/api/tunnels.py`
- Modify: `extra/paas-operator/src/api/routes.py`
- Test: `extra/paas-operator/tests/test_tunnel_routes_sync.py`（追加 API 層）
- Test: `extra/paas-operator/tests/test_route_availability.py`

- [ ] **Step 1: 寫失敗測試（API 層 sync）** — 追加到 `test_tunnel_routes_sync.py`（mock 風格鏡像 `tests/test_scale.py` 的 client fixture）：

```python
from unittest.mock import patch as sync_patch

from fastapi.testclient import TestClient

from src.main import app


@pytest.fixture
def client():
    with sync_patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})


class TestTunnelRoutesSyncEndpoint:
    @sync_patch("src.api.tunnels.cloudflare_service")
    def test_sync_routes_happy_path(self, mock_cf, client):
        mock_cf.api_token = "t"
        mock_cf.account_id = "a"
        mock_cf.domain = "woowtech.io"
        mock_cf.configure_tunnel_ingress = AsyncMock(return_value=None)
        mock_cf.reconcile_tunnel_dns = AsyncMock(
            return_value={"a": "rec-a", "b": "rec-b"}
        )

        response = client.put(
            "/api/tunnels/tun-1/routes",
            json={
                "routes": [
                    {"subdomain": "a", "service_url": "http://localhost:80",
                     "http_host_header": True},
                    {"subdomain": "b", "service_url": "http_status:503",
                     "http_host_header": False},
                ],
                "comment": "paas-sa-x-y",
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["tunnel_id"] == "tun-1"
        assert {r["subdomain"]: r["dns_record_id"] for r in data["routes"]} == {
            "a": "rec-a", "b": "rec-b",
        }
        # ingress rules built from routes with FQDN hostnames
        ingress_call = mock_cf.configure_tunnel_ingress.call_args
        assert ingress_call.kwargs["tunnel_id"] == "tun-1"
        assert ingress_call.kwargs["rules"][0]["hostname"] == "a.woowtech.io"
        # dns reconcile stamped with comment
        dns_call = mock_cf.reconcile_tunnel_dns.call_args
        assert dns_call.kwargs["comment"] == "paas-sa-x-y"

    @sync_patch("src.api.tunnels.cloudflare_service")
    def test_sync_routes_requires_credentials(self, mock_cf, client):
        mock_cf.api_token = None
        mock_cf.account_id = None
        response = client.put(
            "/api/tunnels/tun-1/routes",
            json={"routes": [{"subdomain": "a",
                              "service_url": "http://localhost:80"}]},
        )
        assert response.status_code == 400
```

- [ ] **Step 2: 寫失敗測試（availability）** — 建 `extra/paas-operator/tests/test_route_availability.py`：

```python
"""Tests for GET /api/routes/availability."""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app


@pytest.fixture
def client():
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})


class TestRouteAvailability:
    @patch("src.api.routes.cloudflare_service")
    def test_available_when_no_record(self, mock_cf, client):
        mock_cf.api_token = "t"
        mock_cf.domain = "woowtech.io"
        mock_cf.get_dns_record = AsyncMock(return_value=None)
        response = client.get("/api/routes/availability",
                              params={"subdomain": "waltek-ha"})
        assert response.status_code == 200
        data = response.json()
        assert data == {
            "subdomain": "waltek-ha",
            "hostname": "waltek-ha.woowtech.io",
            "available": True,
        }

    @patch("src.api.routes.cloudflare_service")
    def test_taken_when_record_exists(self, mock_cf, client):
        mock_cf.api_token = "t"
        mock_cf.domain = "woowtech.io"
        mock_cf.get_dns_record = AsyncMock(return_value={"id": "rec-1"})
        response = client.get("/api/routes/availability",
                              params={"subdomain": "waltek-ha"})
        assert response.status_code == 200
        assert response.json()["available"] is False
```

- [ ] **Step 3: 跑測試確認失敗**

Run: `cd extra/paas-operator && pytest tests/test_tunnel_routes_sync.py::TestTunnelRoutesSyncEndpoint tests/test_route_availability.py -v`
Expected: FAIL — 404/405（endpoint 不存在）

- [ ] **Step 4: 實作 sync endpoint** — `extra/paas-operator/src/api/tunnels.py`，import 區加：

```python
from src.models.schemas import (
    TunnelCreateRequest,
    TunnelCreateResponse,
    TunnelRoutesSyncRequest,
    TunnelRoutesSyncResponse,
    TunnelRouteSyncResult,
    TunnelStatusResponse,
    TunnelTokenResponse,
)
```

檔案末尾加 endpoint：

```python
@router.put(
    "/{tunnel_id}/routes",
    response_model=TunnelRoutesSyncResponse,
    summary="Declaratively sync a dedicated tunnel's routes (ingress + DNS)",
)
async def sync_tunnel_routes(tunnel_id: str, request: TunnelRoutesSyncRequest):
    """Replace the tunnel's ingress rules and reconcile DNS records.

    Declarative full sync: the request body is the complete desired route
    list. Ingress is replaced wholesale (plus catch-all 404); DNS records
    pointing at this tunnel are created/kept/deleted to match. Idempotent —
    used by Security Access for route CRUD, suspend (service_url=
    http_status:503) and resume.
    """
    if not cloudflare_service.api_token or not cloudflare_service.account_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cloudflare API token and account ID are required for tunnel management",
        )

    domain = cloudflare_service.domain
    rules = [
        {
            "hostname": f"{r.subdomain}.{domain}",
            "service": r.service_url,
            "http_host_header": r.http_host_header,
        }
        for r in request.routes
    ]

    try:
        await cloudflare_service.configure_tunnel_ingress(
            tunnel_id=tunnel_id, rules=rules,
        )
        dns_map = await cloudflare_service.reconcile_tunnel_dns(
            tunnel_id=tunnel_id,
            subdomains=[r.subdomain for r in request.routes],
            comment=request.comment,
        )
        logger.info(
            f"Tunnel {tunnel_id} routes synced: {len(request.routes)} route(s)"
        )
        return TunnelRoutesSyncResponse(
            tunnel_id=tunnel_id,
            routes=[
                TunnelRouteSyncResult(
                    subdomain=r.subdomain,
                    hostname=f"{r.subdomain}.{domain}",
                    dns_record_id=dns_map.get(r.subdomain),
                )
                for r in request.routes
            ],
        )
    except CloudflareException as e:
        logger.error(f"Failed to sync tunnel routes: {e.message}")
        raise HTTPException(
            status_code=e.status_code or status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to sync tunnel routes: {e.message}",
        )
```

- [ ] **Step 5: 實作 availability endpoint** — `extra/paas-operator/src/api/routes.py` 末尾加（讀檔案開頭確認 `cloudflare_service` instance 與 import 已存在）：

```python
@router.get(
    "/availability",
    summary="Check whether a subdomain is free in the platform zone",
)
async def check_route_availability(subdomain: str):
    """Check Cloudflare-side DNS for an existing record on this subdomain.

    Second layer of the two-layer availability check (first layer is the
    Odoo DB UNIQUE constraint) — catches drift and records created outside
    the platform (e.g. infra tunnels, manual records).
    """
    if not cloudflare_service.api_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cloudflare API token is required",
        )
    hostname = f"{subdomain}.{cloudflare_service.domain}"
    existing = await cloudflare_service.get_dns_record(hostname)
    return {
        "subdomain": subdomain,
        "hostname": hostname,
        "available": existing is None,
    }
```

**路由順序注意**：FastAPI 依宣告順序匹配。`routes.py` 既有 `DELETE /{subdomain}` 類 path-param 路由的話，`GET /availability` 必須宣告在 path-param 路由**之前**，否則 `availability` 會被吃成 subdomain。讀檔確認後放對位置（必要時放到既有 `@router.get("", ...)` list_routes 之後、path-param 路由之前）。

- [ ] **Step 6: 跑測試確認通過**

Run: `cd extra/paas-operator && pytest tests/test_tunnel_routes_sync.py tests/test_route_availability.py -v`
Expected: 全 PASS

- [ ] **Step 7: 跑 operator 既有 unit suite 確認不回歸**

Run: `cd extra/paas-operator && pytest tests/ -v -m "not integration"`
Expected: 全 PASS

- [ ] **Step 8: Commit**

```bash
git add extra/paas-operator/src/api/tunnels.py extra/paas-operator/src/api/routes.py extra/paas-operator/tests/
git commit -m "feat: add tunnel routes sync and availability endpoints to operator"
```

---

### Task 5: Odoo Operator Client — `sync_tunnel_routes()` / `check_route_availability()`

**Files:**
- Modify: `src/services/paas_operator.py`
- Test: `src/tests/test_paas_operator.py`（追加；先讀檔案確認既有 mock 風格 — 應為 mock `_request` 或 `requests.Session`）

- [ ] **Step 1: 寫失敗測試** — 追加到 `src/tests/test_paas_operator.py`（鏡像檔內既有 client 測試的 mock 方式；以下假設以 `patch.object(client, '_request')` 風格）：

```python
class TestSecurityAccessClientMethods(TransactionCase):
    """sync_tunnel_routes / check_route_availability HTTP contract."""

    def _make_client(self):
        from odoo.addons.woow_paas_platform.services.paas_operator import (
            PaaSOperatorClient,
        )
        return PaaSOperatorClient(base_url='http://op:8000', api_key='k')

    def test_sync_tunnel_routes_contract(self):
        client = self._make_client()
        with patch.object(client, '_request') as mock_req:
            mock_req.return_value = {
                'tunnel_id': 'tun-1',
                'routes': [{'subdomain': 'a', 'hostname': 'a.woowtech.io',
                            'dns_record_id': 'rec-a'}],
            }
            result = client.sync_tunnel_routes(
                'tun-1',
                routes=[{'subdomain': 'a',
                         'service_url': 'http://localhost:80',
                         'http_host_header': True}],
                comment='paas-sa-x-y',
            )
        mock_req.assert_called_once_with(
            'PUT', '/api/tunnels/tun-1/routes',
            data={
                'routes': [{'subdomain': 'a',
                            'service_url': 'http://localhost:80',
                            'http_host_header': True}],
                'comment': 'paas-sa-x-y',
            },
            timeout=120,
        )
        self.assertEqual(result['routes'][0]['dns_record_id'], 'rec-a')

    def test_check_route_availability_contract(self):
        client = self._make_client()
        with patch.object(client, '_request') as mock_req:
            mock_req.return_value = {'subdomain': 'a',
                                     'hostname': 'a.woowtech.io',
                                     'available': True}
            result = client.check_route_availability('a')
        mock_req.assert_called_once_with(
            'GET', '/api/routes/availability', params={'subdomain': 'a'},
        )
        self.assertTrue(result['available'])
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform:TestSecurityAccessClientMethods --stop-after-init -u woow_paas_platform`
Expected: FAIL — no attribute `sync_tunnel_routes`

- [ ] **Step 3: 實作** — `src/services/paas_operator.py`，加在 `configure_tunnel()` 後面：

```python
    def sync_tunnel_routes(
        self,
        tunnel_id: str,
        routes: List[Dict[str, Any]],
        comment: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Declaratively sync a dedicated tunnel's full route list.

        Replaces the tunnel ingress (plus catch-all 404) and reconciles DNS
        records. Idempotent — Security Access uses this for route CRUD,
        suspend (service_url='http_status:503') and resume.

        Args:
            tunnel_id: Cloudflare Tunnel ID
            routes: list of dicts {subdomain, service_url, http_host_header}
            comment: canonical name stamped on DNS records (traceability)

        Returns:
            Dict with tunnel_id and routes (each with dns_record_id)
        """
        data: Dict[str, Any] = {'routes': routes}
        if comment:
            data['comment'] = comment
        return self._request(
            'PUT',
            f'/api/tunnels/{tunnel_id}/routes',
            data=data,
            timeout=HELM_OPERATION_TIMEOUT,
        )

    def check_route_availability(self, subdomain: str) -> Dict[str, Any]:
        """Check Cloudflare-side whether a subdomain is free in the zone.

        Args:
            subdomain: subdomain prefix (without domain)

        Returns:
            Dict with subdomain, hostname, available (bool)
        """
        return self._request(
            'GET',
            '/api/routes/availability',
            params={'subdomain': subdomain},
        )
```

- [ ] **Step 4: 跑測試確認通過**（同 Step 2）Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/services/paas_operator.py src/tests/test_paas_operator.py
git commit -m "feat: add tunnel routes sync and availability client methods"
```

---

### Task 6: Plan quota 欄位 — `sh.subscription.plan` inherit

**Files:**
- Create: `src/models/sh_subscription_plan_sa_quota.py`
- Modify: `src/models/__init__.py`
- Test: `src/tests/test_security_access_template.py`（新檔，本 task 先放 quota 欄位測試）
- Modify: `src/tests/__init__.py`

- [ ] **Step 1: 寫失敗測試** — 建 `src/tests/test_security_access_template.py`：

```python
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase


class TestSaPlanQuotaFields(TransactionCase):
    """sh.subscription.plan SA quota fields exist with sane defaults."""

    def test_quota_fields_exist_with_defaults(self):
        plan = self.env['sh.subscription.plan'].create({
            'name': 'SA Quota Test Plan',
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_plan_price': 100,
            'sh_no_of_billing_cycle': -1,
        })
        # 存在性 + 預設值（phase-versioned-test-assertion：不硬編未來會變的快照）
        self.assertGreaterEqual(plan.sa_max_routes, 0)
        self.assertGreaterEqual(plan.sa_max_access_policies, 0)
        self.assertGreaterEqual(plan.sa_max_custom_domains, 0)
        self.assertGreaterEqual(plan.sa_max_email_rules, 0)

    def test_quota_fields_writable(self):
        plan = self.env['sh.subscription.plan'].create({
            'name': 'SA Quota Write Plan',
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_plan_price': 100,
            'sh_no_of_billing_cycle': -1,
            'sa_max_routes': 10,
        })
        self.assertEqual(plan.sa_max_routes, 10)
```

並在 `src/tests/__init__.py` 加（依字母序放在 test_smart_home_template 附近）：

```python
from . import test_security_access_template
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform:TestSaPlanQuotaFields --stop-after-init -u woow_paas_platform`
Expected: FAIL — `sa_max_routes` 欄位不存在

- [ ] **Step 3: 實作** — 建 `src/models/sh_subscription_plan_sa_quota.py`：

```python
from odoo import fields, models


class ShSubscriptionPlanSaQuota(models.Model):
    """Security Access quota dimensions on subscription plans.

    Only adds new fields — never overrides std field types
    (odoo-std-field-no-override pattern). The four quotas mirror the four
    Security Access sub-resources (routes / access policies / custom
    domains / email rules); P1 enforces sa_max_routes, the others are
    enforced by their own phases (P2-P4) but modelled now so plan seeds
    don't churn.
    """

    _inherit = 'sh.subscription.plan'

    sa_max_routes = fields.Integer(
        string='SA Max Application Routes',
        default=0,
        help='Security Access: max user-defined application routes '
             '(excluding the auto-created primary route). 0 = none allowed.',
    )
    sa_max_access_policies = fields.Integer(
        string='SA Max Access Policies',
        default=0,
        help='Security Access: max access policies (enforced in P2).',
    )
    sa_max_custom_domains = fields.Integer(
        string='SA Max Custom Domains',
        default=0,
        help='Security Access: max custom domains (enforced in P3).',
    )
    sa_max_email_rules = fields.Integer(
        string='SA Max Email Routing Rules',
        default=0,
        help='Security Access: max email routing rules (enforced in P4).',
    )
```

`src/models/__init__.py` 加 import（放在 sh_subscription_fix 附近）：

```python
from . import sh_subscription_plan_sa_quota
```

- [ ] **Step 4: 跑測試確認通過**（同 Step 2）Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/models/sh_subscription_plan_sa_quota.py src/models/__init__.py src/tests/test_security_access_template.py src/tests/__init__.py
git commit -m "feat: add security access quota fields to subscription plan"
```

---

### Task 7: `security_access_template` model + 三方 cross-type constraint

**Files:**
- Create: `src/models/security_access_template.py`
- Modify: `src/models/__init__.py`、`src/models/smart_home_template.py`、`src/models/cloud_app_template.py`
- Modify: `src/security/ir.model.access.csv`（本 task 先加 template 兩行，instance 的 ACL 在 Task 11）
- Test: `src/tests/test_security_access_template.py`（追加）

- [ ] **Step 1: 寫失敗測試** — 追加到 `test_security_access_template.py`：

```python
class TestSecurityAccessTemplate(TransactionCase):
    """Template catalog + 三方 cross-service-type plan 隔離."""

    def _make_plan(self, name):
        return self.env['sh.subscription.plan'].create({
            'name': name,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_plan_price': 100,
            'sh_no_of_billing_cycle': -1,
        })

    def test_create_template_with_plans(self):
        plan = self._make_plan('SA Tmpl Plan A')
        tmpl = self.env['woow_paas_platform.security_access_template'].create({
            'name': 'Security Access',
            'slug': 'security-access',
            'subscription_plan_ids': [(6, 0, [plan.id])],
        })
        self.assertTrue(tmpl.is_active)
        self.assertIn(plan, tmpl.subscription_plan_ids)

    def test_plan_shared_with_smart_home_template_rejected(self):
        plan = self._make_plan('SA Cross SH Plan')
        self.env['woow_paas_platform.smart_home_template'].create({
            'name': 'SH Cross Tmpl',
            'subscription_plan_ids': [(6, 0, [plan.id])],
        })
        with self.assertRaises(ValidationError):
            self.env['woow_paas_platform.security_access_template'].create({
                'name': 'SA Cross Tmpl',
                'subscription_plan_ids': [(6, 0, [plan.id])],
            })

    def test_plan_shared_with_cloud_template_rejected(self):
        plan = self._make_plan('SA Cross CS Plan')
        self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'CS Cross Tmpl',
            'slug': 'cs-cross',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://test/chart',
            'helm_chart_version': '1.0.0',
            'subscription_plan_ids': [(6, 0, [plan.id])],
        })
        with self.assertRaises(ValidationError):
            self.env['woow_paas_platform.security_access_template'].create({
                'name': 'SA Cross Tmpl 2',
                'subscription_plan_ids': [(6, 0, [plan.id])],
            })

    def test_symmetric_smart_home_side_rejects_sa_plan(self):
        """對稱鎖：SA 先佔的 plan，SH template 後綁也要爆。"""
        plan = self._make_plan('SA First Plan')
        self.env['woow_paas_platform.security_access_template'].create({
            'name': 'SA First Tmpl',
            'subscription_plan_ids': [(6, 0, [plan.id])],
        })
        with self.assertRaises(ValidationError):
            self.env['woow_paas_platform.smart_home_template'].create({
                'name': 'SH Late Tmpl',
                'subscription_plan_ids': [(6, 0, [plan.id])],
            })
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform:TestSecurityAccessTemplate --stop-after-init -u woow_paas_platform`
Expected: FAIL — model `woow_paas_platform.security_access_template` 不存在

- [ ] **Step 3: 實作 template model** — 建 `src/models/security_access_template.py`：

```python
from odoo import api, fields, models
from odoo.exceptions import ValidationError


class SecurityAccessTemplate(models.Model):
    """Catalog entry holding the subscription-plan whitelist for Security Access.

    Mirrors smart_home_template / cloud_app_template.subscription_plan_ids.
    Separate relation table so a plan can never appear under two service
    types (see _check_no_cross_service_type_plans — invoice line text is
    derived from the plan's product, so sharing would mislabel invoices).
    """

    _name = 'woow_paas_platform.security_access_template'
    _description = 'Security Access Template'
    _order = 'name'

    name = fields.Char(
        string='Name',
        required=True,
        help='Template display name (e.g., Security Access)',
    )
    slug = fields.Char(
        string='Slug',
        index=True,
        help='URL-safe identifier for the template',
    )
    icon = fields.Binary(string='Icon')
    description = fields.Char(
        string='Description',
        help='Short description (~100 characters)',
    )
    full_description = fields.Text(
        string='Full Description',
        help='Detailed description with features and use cases',
    )
    subscription_plan_ids = fields.Many2many(
        'sh.subscription.plan',
        'woow_security_access_template_plan_rel',
        'template_id',
        'plan_id',
        string='Subscription Plans',
        help='Available subscription plans for Security Access instances '
             'created from this template',
    )
    is_active = fields.Boolean(
        string='Active',
        default=True,
        help='Whether this template is offered to end users',
    )

    @api.constrains('subscription_plan_ids')
    def _check_no_cross_service_type_plans(self):
        """A plan must belong to one service type only (three-way lockstep).

        Symmetric checks live on cloud_app_template and smart_home_template;
        all three fire on write so the conflict is caught regardless of
        which side is edited last.
        """
        if not self:
            return
        for record in self:
            if not record.subscription_plan_ids:
                continue
            for other_model, label in [
                ('woow_paas_platform.cloud_app_template', 'Cloud App Template'),
                ('woow_paas_platform.smart_home_template', 'Smart Home Template'),
            ]:
                conflicts = self.env[other_model].search([
                    ('subscription_plan_ids', 'in',
                     record.subscription_plan_ids.ids),
                ])
                if conflicts:
                    shared = (record.subscription_plan_ids
                              & conflicts.subscription_plan_ids)
                    raise ValidationError(
                        "Plan(s) %s are already linked to %s(s) %s. "
                        "A subscription plan must belong to a single service "
                        "type — its product determines the invoice line text."
                        % (
                            ', '.join(shared.mapped('name')),
                            label,
                            ', '.join(conflicts.mapped('name')),
                        )
                    )
```

- [ ] **Step 4: 對稱鎖 — 修改既有兩個 template constraint**

`src/models/smart_home_template.py` 的 `_check_no_cross_service_type_plans`：把單查 CloudTemplate 改為迴圈查兩個 model。完整替換該 method body：

```python
    @api.constrains('subscription_plan_ids')
    def _check_no_cross_service_type_plans(self):
        """A plan must belong to one service type only.

        Three-way symmetric check with cloud_app_template and
        security_access_template; all fire on write so the conflict is
        caught regardless of which side is edited last.
        """
        if not self:
            return
        for record in self:
            if not record.subscription_plan_ids:
                continue
            for other_model, label in [
                ('woow_paas_platform.cloud_app_template', 'Cloud App Template'),
                ('woow_paas_platform.security_access_template',
                 'Security Access Template'),
            ]:
                conflicts = self.env[other_model].search([
                    ('subscription_plan_ids', 'in',
                     record.subscription_plan_ids.ids),
                ])
                if conflicts:
                    shared = (record.subscription_plan_ids
                              & conflicts.subscription_plan_ids)
                    raise ValidationError(
                        "Plan(s) %s are already linked to %s(s) %s. "
                        "A subscription plan must belong to a single service "
                        "type — its product determines the invoice line text."
                        % (
                            ', '.join(shared.mapped('name')),
                            label,
                            ', '.join(conflicts.mapped('name')),
                        )
                    )
```

`src/models/cloud_app_template.py`：先讀該檔找到其 `_check_no_cross_service_type_plans`（與 smart_home_template 對稱的版本），同樣改為迴圈查 `smart_home_template` + `security_access_template` 兩個 model（程式碼同上，把清單換成這兩個）。

- [ ] **Step 5: ACL（template）** — `src/security/ir.model.access.csv` 加（鏡像 smart_home_template 兩行）：

```csv
access_security_access_template_user,woow_paas_platform.security_access_template.user,model_woow_paas_platform_security_access_template,base.group_user,1,0,0,0
access_security_access_template_admin,woow_paas_platform.security_access_template.admin,model_woow_paas_platform_security_access_template,base.group_system,1,1,1,1
```

`src/models/__init__.py` 加：

```python
from . import security_access_template
```

- [ ] **Step 6: 跑測試確認通過**

Run: `./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform:TestSecurityAccessTemplate --stop-after-init -u woow_paas_platform`
Expected: PASS。另跑 `--test-tags /woow_paas_platform:TestSmartHomeTemplate` 確認既有 SH constraint 測試不回歸。

- [ ] **Step 7: Commit**

```bash
git add src/models/security_access_template.py src/models/smart_home_template.py src/models/cloud_app_template.py src/models/__init__.py src/security/ir.model.access.csv src/tests/test_security_access_template.py
git commit -m "feat: add security access template with three-way plan isolation"
```

---

### Task 8: 主模型 `security_access` + 子模型 `security_access_route`

**Files:**
- Create: `src/models/security_access.py`
- Modify: `src/models/__init__.py`
- Test: `src/tests/test_security_access.py`
- Modify: `src/tests/__init__.py`

- [ ] **Step 1: 寫失敗測試** — 建 `src/tests/test_security_access.py`：

```python
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase

MOCK_GET_CLIENT = (
    'odoo.addons.woow_paas_platform.models.security_access.'
    'get_paas_operator_client'
)


class TestSecurityAccess(TransactionCase):
    """woow_paas_platform.security_access model behaviour."""

    def setUp(self):
        super().setUp()
        self.SecurityAccess = self.env['woow_paas_platform.security_access']
        self.Route = self.env['woow_paas_platform.security_access_route']
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.workspace = self.Workspace.create({'name': 'SA Test WS'})

    def _make_sa(self, name='My Gateway'):
        return self.SecurityAccess.create({
            'name': name,
            'workspace_id': self.workspace.id,
        })

    # ---- identity / naming ----

    def test_canonical_name_set_once_on_create(self):
        sa = self._make_sa()
        self.assertTrue(sa.canonical_name.startswith('paas-sa-'))
        self.assertTrue(sa.reference_id)

    def test_canonical_name_stable_after_nickname_change(self):
        """nickname 可改，canonical_name 不得漂移（hash salt 含 name）。"""
        sa = self._make_sa()
        original = sa.canonical_name
        sa.write({'name': 'Renamed Gateway'})
        self.assertEqual(sa.canonical_name, original)

    def test_primary_hostname_combines_domain(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.paas_domain', 'stg.woowtech.io')
        sa = self._make_sa()
        self.assertEqual(
            sa.primary_hostname, f"{sa.canonical_name}.stg.woowtech.io")

    # ---- provision ----

    @patch(MOCK_GET_CLIENT)
    def test_provision_creates_tunnel_and_primary_route(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.create_tunnel.return_value = {
            'tunnel_id': 'tun-sa-1',
            'tunnel_token': 'tok-secret',
            'tunnel_name': 'paas-sa-x-y',
            'dns_record_id': 'rec-primary',
        }
        mock_get_client.return_value = mock_client
        sa = self._make_sa()
        sa.action_provision()
        self.assertEqual(sa.state, 'active')
        self.assertEqual(sa.tunnel_id, 'tun-sa-1')
        # tunnel name = canonical（反推性需求）
        create_kwargs = mock_client.create_tunnel.call_args.kwargs
        self.assertEqual(create_kwargs['name'], sa.canonical_name)
        # primary route 自動建立、prefix = canonical、不可刪
        primary = sa.route_ids.filtered('is_primary')
        self.assertEqual(len(primary), 1)
        self.assertEqual(primary.subdomain_prefix, sa.canonical_name)

    @patch(MOCK_GET_CLIENT)
    def test_provision_error_sets_error_state(self, mock_get_client):
        from odoo.addons.woow_paas_platform.services.paas_operator import (
            PaaSOperatorError,
        )
        mock_client = MagicMock()
        mock_client.create_tunnel.side_effect = PaaSOperatorError('boom')
        mock_get_client.return_value = mock_client
        sa = self._make_sa()
        sa.action_provision()
        self.assertEqual(sa.state, 'error')
        self.assertIn('boom', sa.error_message)

    # ---- routes ----

    def _make_active_sa(self):
        sa = self._make_sa()
        sa.write({'state': 'active', 'tunnel_id': 'tun-sa-1'})
        # route create 會觸發 operator sync（sa 已 active）→ 必須 mock
        with patch(MOCK_GET_CLIENT) as mock_gc:
            mock_client = MagicMock()
            mock_client.sync_tunnel_routes.return_value = {'routes': []}
            mock_gc.return_value = mock_client
            self.Route.create({
                'security_access_id': sa.id,
                'subdomain_prefix': sa.canonical_name,
                'is_primary': True,
            })
        return sa

    @patch(MOCK_GET_CLIENT)
    def test_add_route_syncs_to_operator(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.sync_tunnel_routes.return_value = {
            'tunnel_id': 'tun-sa-1',
            'routes': [
                {'subdomain': 'x', 'hostname': 'x.io', 'dns_record_id': 'r1'},
            ],
        }
        mock_get_client.return_value = mock_client
        sa = self._make_active_sa()
        self.Route.create({
            'security_access_id': sa.id,
            'subdomain_prefix': 'waltek-ha',
        })
        sync_kwargs = mock_client.sync_tunnel_routes.call_args.kwargs
        # 宣告式整份：primary + 新 route 都在 payload
        subs = [r['subdomain'] for r in sync_kwargs['routes']]
        self.assertIn(sa.canonical_name, subs)
        self.assertIn('waltek-ha', subs)
        self.assertEqual(sync_kwargs['comment'], sa.canonical_name)

    def test_route_prefix_validation(self):
        sa = self._make_active_sa()
        for bad in ['-bad', 'bad-', 'Bad', 'a b', 'paas-evil', 'svc-evil', 'ab']:
            with self.assertRaises(ValidationError, msg=bad):
                with self.env.cr.savepoint():
                    self.Route.create({
                        'security_access_id': sa.id,
                        'subdomain_prefix': bad,
                    })

    def test_route_prefix_unique_across_platform(self):
        sa = self._make_active_sa()
        sa2 = self.SecurityAccess.create({
            'name': 'Other SA',
            'workspace_id': self.workspace.id,
        })
        with patch(MOCK_GET_CLIENT) as mock_get_client:
            mock_get_client.return_value = MagicMock()
            self.Route.create({
                'security_access_id': sa.id,
                'subdomain_prefix': 'taken-prefix',
            })
            with self.assertRaises(Exception):  # psycopg2 UniqueViolation
                with self.env.cr.savepoint():
                    self.Route.create({
                        'security_access_id': sa2.id,
                        'subdomain_prefix': 'taken-prefix',
                    })

    def test_primary_route_cannot_be_deleted(self):
        sa = self._make_active_sa()
        primary = sa.route_ids.filtered('is_primary')
        with self.assertRaises(UserError):
            primary.unlink()

    def test_route_prefix_immutable(self):
        sa = self._make_active_sa()
        with patch(MOCK_GET_CLIENT) as mock_get_client:
            mock_get_client.return_value = MagicMock()
            route = self.Route.create({
                'security_access_id': sa.id,
                'subdomain_prefix': 'fixed-prefix',
            })
            with self.assertRaises(UserError):
                route.write({'subdomain_prefix': 'renamed'})
```

`src/tests/__init__.py` 加：

```python
from . import test_security_access
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform:TestSecurityAccess --stop-after-init -u woow_paas_platform`
Expected: FAIL — model 不存在

- [ ] **Step 3: 實作** — 建 `src/models/security_access.py`：

```python
import logging
import re
import uuid

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..services.naming import make_security_access_subdomain

from ..services.paas_operator import (
    get_paas_operator_client,
    PaaSOperatorError,
    PaaSOperatorConnectionError,
)

_logger = logging.getLogger(__name__)

# 3-63 chars, lowercase alphanumeric + hyphen, no leading/trailing hyphen
_PREFIX_RE = re.compile(r'^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$')
# Platform-internal naming conventions users must not squat on
_RESERVED_PREFIXES = ('paas-', 'svc-')


class SecurityAccess(models.Model):
    _name = 'woow_paas_platform.security_access'
    _inherit = ['woow_paas_platform.subscription_lifecycle_mixin']
    _description = 'Security Access Instance'
    _order = 'create_date desc'

    # Identity
    name = fields.Char(
        string='Name',
        required=True,
        help='User-defined nickname for this security access (e.g. '
             'secure-gateway). The Cloudflare-facing identity is '
             'canonical_name, which never changes.',
    )
    reference_id = fields.Char(
        string='Reference ID',
        required=True,
        copy=False,
        index=True,
        default=lambda self: str(uuid.uuid4()),
        help='Unique reference ID for hash-based naming. Immutable.',
    )
    canonical_name = fields.Char(
        string='Canonical Name',
        readonly=True,
        copy=False,
        index=True,
        help='paas-sa-{ws_hash}-{res_hash}. Computed ONCE at create and '
             'stored — the naming hash salts on the (mutable) nickname, so '
             'recomputing would drift the subdomain and break routes. Used '
             'as tunnel name and stamped on every Cloudflare object for '
             'traceability.',
    )
    workspace_id = fields.Many2one(
        comodel_name='woow_paas_platform.workspace',
        string='Workspace',
        required=True,
        ondelete='cascade',
        help='Workspace this security access belongs to',
    )
    template_id = fields.Many2one(
        comodel_name='woow_paas_platform.security_access_template',
        string='Template',
        help='Catalog template this instance was created from',
    )

    # State
    state = fields.Selection(
        selection=[
            ('pending', 'Pending'),
            ('provisioning', 'Provisioning'),
            ('active', 'Active'),
            ('error', 'Error'),
            ('suspended', 'Suspended'),
            ('deleting', 'Deleting'),
        ],
        string='State',
        default='pending',
        required=True,
    )
    error_message = fields.Text(
        string='Error Message',
        help='Error details if provisioning failed',
    )

    # Tunnel info
    tunnel_id = fields.Char(string='Tunnel ID')
    tunnel_token = fields.Char(
        string='Tunnel Token',
        groups='base.group_system',
    )
    tunnel_name = fields.Char(string='Tunnel Name')
    connector_id = fields.Char(string='Connector ID')
    connector_type = fields.Char(string='Connector Type')
    tunnel_status = fields.Selection(
        selection=[
            ('connected', 'Connected'),
            ('disconnected', 'Disconnected'),
            ('error', 'Error'),
        ],
        string='Tunnel Status',
        default='disconnected',
    )
    tunnel_uptime = fields.Char(string='Uptime')

    primary_hostname = fields.Char(
        string='Primary Hostname',
        compute='_compute_primary_hostname',
        help='{canonical_name}.{paas_domain} — safe to compute: derives '
             'from the stored canonical_name, not from the nickname.',
    )

    route_ids = fields.One2many(
        comodel_name='woow_paas_platform.security_access_route',
        inverse_name='security_access_id',
        string='Application Routes',
    )
    route_count = fields.Integer(
        compute='_compute_route_count', string='Routes',
    )

    # Timestamps
    deployed_at = fields.Datetime(string='Deployed At')

    # ------------------------------------------------------------------ #
    #  Computes / helpers                                                #
    # ------------------------------------------------------------------ #

    @api.depends('canonical_name')
    def _compute_primary_hostname(self):
        domain = self._get_paas_domain()
        for record in self:
            record.primary_hostname = (
                f"{record.canonical_name}.{domain}"
                if record.canonical_name else False
            )

    def _compute_route_count(self):
        for record in self:
            record.route_count = len(record.route_ids)

    def _get_operator_client(self):
        """Get PaaS Operator client instance."""
        client = get_paas_operator_client(self.env)
        if not client:
            raise UserError(
                "PaaS Operator is not configured. "
                "Please set Operator URL and API Key in Settings → Woow PaaS."
            )
        return client

    def _get_paas_domain(self):
        return self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.paas_domain', 'woowtech.io'
        )

    @api.model
    def _get_default_service_url(self):
        """後台可設定的預設反向代理 URL（spec §6）。"""
        return self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.sa_default_service_url', 'http://localhost:80'
        )

    def _generate_canonical_name(self):
        self.ensure_one()
        workspace = self.workspace_id
        slug = (workspace.slug if hasattr(workspace, 'slug') and workspace.slug
                else workspace.name.lower().replace(' ', '-'))
        return make_security_access_subdomain(slug, self.reference_id, self.name)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for record in records:
            if not record.canonical_name:
                # compute ONCE and store; never recompute (name is mutable).
                # sudo: the write() guard below blocks canonical_name writes
                # for non-system users — this initial assignment is the one
                # legitimate internal write.
                record.sudo().write({
                    'canonical_name': record._generate_canonical_name(),
                })
        return records

    def write(self, vals):
        # canonical_name / reference_id are identity anchors — subdomain &
        # tunnel naming derive from them; drift would break routing.
        if not self.env.su and not self.env.user.has_group('base.group_system'):
            for forbidden in ('canonical_name', 'reference_id'):
                if forbidden in vals:
                    raise UserError(
                        "Field '%s' is immutable after creation." % forbidden
                    )
        return super().write(vals)

    # ------------------------------------------------------------------ #
    #  Provision / status / delete                                       #
    # ------------------------------------------------------------------ #

    def action_provision(self):
        """Provision: create dedicated tunnel + primary route."""
        self.ensure_one()
        client = self._get_operator_client()

        self.write({'state': 'provisioning', 'error_message': False})

        try:
            hostname = self.primary_hostname
            default_url = self._get_default_service_url()

            # create_tunnel already configures ingress (single rule +
            # catch-all) and creates the primary DNS record in one call.
            result = client.create_tunnel(
                name=self.canonical_name,
                hostname=hostname,
                service_url=default_url,
            )

            self.write({
                'state': 'active',
                'tunnel_id': result.get('tunnel_id'),
                'tunnel_token': result.get('tunnel_token'),
                'tunnel_name': result.get('tunnel_name'),
                'deployed_at': fields.Datetime.now(),
            })

            # Primary route record mirrors the ingress rule created above.
            self.env['woow_paas_platform.security_access_route'].create({
                'security_access_id': self.id,
                'subdomain_prefix': self.canonical_name,
                'service_url': default_url,
                'is_primary': True,
                'dns_record_id': result.get('dns_record_id'),
            })

            # Re-sync so the primary rule carries http_host_header
            # (create_tunnel's single-rule config doesn't set originRequest).
            self._sync_routes_to_operator()

            _logger.info(
                "[SA] provisioned sa=%s canonical=%s tunnel=%s",
                self.id, self.canonical_name, result.get('tunnel_id'),
            )

        except PaaSOperatorConnectionError as e:
            self.write({
                'state': 'error',
                'error_message': f"Cannot connect to PaaS Operator: {e.message}",
            })
            _logger.error("[SA] provision connection error: %s", e.message)
        except PaaSOperatorError as e:
            self.write({
                'state': 'error',
                'error_message': f"Tunnel creation failed: {e.message}",
            })
            _logger.error("[SA] provision error: %s", e.message)

    def action_refresh_status(self):
        """Refresh tunnel connector status from the PaaS Operator."""
        self.ensure_one()
        if not self.tunnel_id:
            return
        client = self._get_operator_client()
        try:
            result = client.get_tunnel_status(self.tunnel_id)
            connections = result.get('connections', [])
            update_vals = {
                'tunnel_status': 'connected' if connections else 'disconnected',
            }
            if connections:
                conn = connections[0]
                update_vals['connector_id'] = conn.get('connector_id', '')
                update_vals['connector_type'] = conn.get('type', '')
                if conn.get('opened_at'):
                    update_vals['tunnel_uptime'] = conn['opened_at']
            self.write(update_vals)
        except PaaSOperatorError as e:
            self.write({
                'tunnel_status': 'error',
                'error_message': f"Status refresh failed: {e.message}",
            })
            _logger.warning(
                "[SA] status refresh failed sa=%s: %s", self.id, e.message,
            )

    def action_delete(self):
        """User-initiated delete: full Cloudflare teardown then unlink."""
        self.ensure_one()
        self.write({'state': 'deleting'})
        self._cleanup_cloudflare()
        self.unlink()

    def unlink(self):
        """Direct-unlink guard: Cloudflare resources must never outlive the
        record (cloud-service-unlink-override-cleanup pattern)."""
        for record in self:
            if record.tunnel_id:
                record._cleanup_cloudflare()
        return super().unlink()

    # ------------------------------------------------------------------ #
    #  Route sync (declarative)                                          #
    # ------------------------------------------------------------------ #

    def _build_route_payload(self, override_service_url=None):
        """Desired-state payload for sync_tunnel_routes.

        Args:
            override_service_url: when set (e.g. 'http_status:503'),
                every route's service is replaced — used by suspend.
        """
        self.ensure_one()
        payload = []
        for route in self.route_ids.sorted(
                key=lambda r: (not r.is_primary, r.id)):
            payload.append({
                'subdomain': route.subdomain_prefix,
                'service_url': override_service_url or route.service_url,
                'http_host_header': (
                    route.http_host_header and not override_service_url
                ),
            })
        return payload

    def _sync_routes_to_operator(self, override_service_url=None):
        """Push the full desired route list to the operator (idempotent)."""
        self.ensure_one()
        if not self.tunnel_id:
            return
        client = self._get_operator_client()
        result = client.sync_tunnel_routes(
            self.tunnel_id,
            routes=self._build_route_payload(override_service_url),
            comment=self.canonical_name,
        )
        # store DNS record ids back on routes
        dns_by_sub = {
            r['subdomain']: r.get('dns_record_id')
            for r in result.get('routes', [])
        }
        for route in self.route_ids:
            rec_id = dns_by_sub.get(route.subdomain_prefix)
            if rec_id and route.dns_record_id != rec_id:
                route.dns_record_id = rec_id

    # ------------------------------------------------------------------ #
    #  Subscription lifecycle: suspend / resume / cleanup                #
    # ------------------------------------------------------------------ #

    def _do_suspend(self):
        """503-freeze every route's ingress; keep DNS / tunnel / token."""
        self.ensure_one()
        _logger.info(
            "[SA] _do_suspend sa=%s state=%s sub=%s",
            self.id, self.state,
            self.subscription_id.id if self.subscription_id else None,
        )
        if self.state == 'suspended':
            return
        if self.tunnel_id:
            try:
                self._sync_routes_to_operator(
                    override_service_url='http_status:503')
            except Exception as e:
                _logger.warning("[SA] _do_suspend failed sa=%s: %s", self.id, e)
                self.write({'state': 'error'})
                return
        self.write({
            'pre_suspend_state': (
                self.state if self.state in ('active', 'error') else 'active'
            ),
            'state': 'suspended',
        })
        _logger.info("[SA] _do_suspend done sa=%s", self.id)

    def _do_resume(self):
        """Restore every route's ingress to its stored service_url."""
        self.ensure_one()
        _logger.info(
            "[SA] _do_resume sa=%s state=%s pre_suspend=%s",
            self.id, self.state, self.pre_suspend_state,
        )
        if self.state != 'suspended':
            return
        if self.tunnel_id:
            try:
                self._sync_routes_to_operator()
            except Exception as e:
                _logger.warning("[SA] _do_resume failed sa=%s: %s", self.id, e)
                self.write({'state': 'error'})
                return
        restore_state = self.pre_suspend_state or 'active'
        self.write({'state': restore_state, 'pre_suspend_state': False})
        _logger.info("[SA] _do_resume done sa=%s state=%s",
                     self.id, restore_state)

    def _do_cleanup(self):
        """Cron-driven permanent removal of Cloudflare resources."""
        self.ensure_one()
        _logger.info("[SA] _do_cleanup sa=%s tunnel=%s",
                     self.id, self.tunnel_id)
        self._cleanup_cloudflare()
        self.write({'state': 'deleting'})

    def _cleanup_cloudflare(self):
        """Idempotent CF teardown. delete_tunnel cascades: operator removes
        every DNS record pointing at the tunnel, then deletes the tunnel
        (token invalidated)."""
        self.ensure_one()
        if not self.tunnel_id:
            return
        try:
            client = self._get_operator_client()
            client.delete_tunnel(self.tunnel_id)
            _logger.info("[SA] tunnel deleted sa=%s tunnel=%s",
                         self.id, self.tunnel_id)
            self.write({'tunnel_id': False})
        except Exception as e:
            # cleanup is re-entrant: log and keep tunnel_id so a later
            # retry (cron or manual) can finish the job.
            _logger.warning("[SA] cleanup failed sa=%s: %s", self.id, e)

    # ------------------------------------------------------------------ #
    #  Serialization                                                      #
    # ------------------------------------------------------------------ #

    def to_dict(self):
        """Serialize for API responses."""
        self.ensure_one()
        return {
            'id': self.id,
            'name': self.name,
            'canonical_name': self.canonical_name or '',
            'workspace_id': self.workspace_id.id,
            'state': self.state,
            'tunnel_id': self.tunnel_id or '',
            'tunnel_name': self.tunnel_name or '',
            'tunnel_status': self.tunnel_status or 'disconnected',
            'tunnel_uptime': self.tunnel_uptime or '',
            'connector_id': self.connector_id or '',
            'connector_type': self.connector_type or '',
            'primary_hostname': self.primary_hostname or '',
            'error_message': self.error_message or '',
            'deployed_at': self.deployed_at.isoformat() if self.deployed_at else '',
            'created_at': self.create_date.isoformat() if self.create_date else '',
            'routes': [r.to_dict() for r in self.route_ids],
            'subscription': self._get_subscription_info(),
        }


class SecurityAccessRoute(models.Model):
    _name = 'woow_paas_platform.security_access_route'
    _description = 'Security Access Application Route'
    _order = 'is_primary desc, id'

    _sql_constraints = [
        (
            'unique_subdomain_prefix',
            'UNIQUE(subdomain_prefix)',
            'Subdomain prefix must be unique across the platform.',
        ),
    ]

    security_access_id = fields.Many2one(
        comodel_name='woow_paas_platform.security_access',
        string='Security Access',
        required=True,
        ondelete='cascade',
        index=True,
    )
    subdomain_prefix = fields.Char(
        string='Subdomain Prefix',
        required=True,
        help='User-defined prefix; hostname = {prefix}.{paas_domain}. '
             'Flat platform-wide namespace, first come first served. '
             'Immutable after create (delete + recreate to rename).',
    )
    hostname = fields.Char(
        string='Hostname',
        compute='_compute_hostname',
    )
    service_url = fields.Char(
        string='Service URL',
        required=True,
        default=lambda self: self.env[
            'woow_paas_platform.security_access']._get_default_service_url(),
        help='Backend target. Default assumes the user runs a reverse '
             'proxy (NPM) on localhost:80 that splits by Host header.',
    )
    http_host_header = fields.Boolean(
        string='Send HTTP Host Header',
        default=True,
        help='Send the public hostname as the HTTP Host header to the '
             'origin so the user-side reverse proxy can route by Host.',
    )
    is_primary = fields.Boolean(
        string='Primary Route',
        default=False,
        help='Auto-created at provision (prefix = canonical name). '
             'Cannot be deleted.',
    )
    is_protected = fields.Boolean(
        string='Protected',
        default=False,
        help='Per-route Access protection toggle. Wired to Cloudflare '
             'Access in P2 — stored but inert in P1.',
    )
    dns_record_id = fields.Char(string='DNS Record ID')

    @api.depends('subdomain_prefix')
    def _compute_hostname(self):
        domain = self.env['woow_paas_platform.security_access'
                          ]._get_paas_domain()
        for route in self:
            route.hostname = (
                f"{route.subdomain_prefix}.{domain}"
                if route.subdomain_prefix else False
            )

    @api.constrains('subdomain_prefix', 'is_primary')
    def _check_prefix_format(self):
        for route in self:
            prefix = route.subdomain_prefix or ''
            if not _PREFIX_RE.match(prefix):
                raise ValidationError(
                    "Subdomain prefix must be 3-63 chars of lowercase "
                    "letters, digits and hyphens, and must not start or "
                    "end with a hyphen: %r" % prefix
                )
            if not route.is_primary and prefix.startswith(_RESERVED_PREFIXES):
                raise ValidationError(
                    "Subdomain prefix must not start with a reserved "
                    "platform prefix (%s): %r"
                    % (', '.join(_RESERVED_PREFIXES), prefix)
                )

    @api.constrains('security_access_id')
    def _check_route_quota(self):
        """Plan quota: non-primary route count <= plan.sa_max_routes.

        No subscription (admin/legacy) → unrestricted, mirroring
        subscription_lifecycle_mixin._is_subscription_active. The
        controller layer re-checks (two-layer enforcement).
        """
        for route in self:
            if route.is_primary:
                continue
            sa = route.security_access_id
            sub = sa.sudo().subscription_id
            if not sub or not sub.sh_subscription_plan_id:
                continue
            quota = sub.sh_subscription_plan_id.sa_max_routes
            count = len(sa.route_ids.filtered(lambda r: not r.is_primary))
            if count > quota:
                raise ValidationError(
                    "Route quota exceeded: plan '%s' allows %d custom "
                    "route(s)." % (sub.sh_subscription_plan_id.name, quota)
                )

    @api.model_create_multi
    def create(self, vals_list):
        routes = super().create(vals_list)
        # one sync per affected SA (declarative full-state push)
        for sa in routes.mapped('security_access_id'):
            if sa.state == 'active' and sa.tunnel_id:
                sa._sync_routes_to_operator()
        return routes

    def write(self, vals):
        # Hard invariant for everyone incl. superuser: hostname / DNS /
        # ingress all derive from the prefix, and no internal path
        # legitimately renames it (delete + recreate is the rename). The
        # context key is an escape hatch for future data-repair migrations
        # only — an `env.su` exemption would silently skip this guard in
        # TransactionCase (su env) and any sudo()'d controller path.
        if 'subdomain_prefix' in vals and not self.env.context.get(
                'sa_internal_prefix_write'):
            for route in self:
                if route.subdomain_prefix != vals['subdomain_prefix']:
                    raise UserError(
                        "Subdomain prefix is immutable. Delete the route "
                        "and create a new one to rename."
                    )
        result = super().write(vals)
        if any(k in vals for k in ('service_url', 'http_host_header')):
            for sa in self.mapped('security_access_id'):
                if sa.state == 'active' and sa.tunnel_id:
                    sa._sync_routes_to_operator()
        return result

    def unlink(self):
        if any(r.is_primary for r in self) and not self.env.context.get(
                'sa_force_delete_primary'):
            raise UserError(
                "The primary route cannot be deleted. It is removed "
                "together with the Security Access instance."
            )
        affected = self.mapped('security_access_id')
        result = super().unlink()
        for sa in affected:
            if sa.exists() and sa.state == 'active' and sa.tunnel_id:
                sa._sync_routes_to_operator()
        return result

    def to_dict(self):
        self.ensure_one()
        return {
            'id': self.id,
            'subdomain_prefix': self.subdomain_prefix,
            'hostname': self.hostname or '',
            'service_url': self.service_url,
            'http_host_header': self.http_host_header,
            'is_primary': self.is_primary,
            'is_protected': self.is_protected,
        }
```

`src/models/__init__.py` 加：

```python
from . import security_access
```

**注意**：SA instance 的 `ir.model.access.csv` 兩個 model 的 ACL 行在 Task 11 才加 — 本 task 跑測試會因 ACL 缺失而 admin-only；測試以 env(admin) 跑不受影響。

- [ ] **Step 4: 跑測試確認通過**

Run: `./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform:TestSecurityAccess --stop-after-init -u woow_paas_platform`
Expected: PASS（注意 Odoo 載入新 model 需要 `-u`）

- [ ] **Step 5: Commit**

```bash
git add src/models/security_access.py src/models/__init__.py src/tests/test_security_access.py src/tests/__init__.py
git commit -m "feat: add security access model with multi-route tunnel sync"
```

---

### Task 9: Lifecycle hooks 測試（suspend / resume / cleanup / unlink 清理鏈）

Task 8 已實作 hooks；本 task 補齊 lifecycle 行為的測試保護（規格定義）。

**Files:**
- Test: `src/tests/test_security_access.py`（追加）

- [ ] **Step 1: 寫測試** — 追加：

```python
class TestSecurityAccessLifecycleHooks(TransactionCase):
    """_do_suspend / _do_resume / _do_cleanup / unlink 清理鏈."""

    def setUp(self):
        super().setUp()
        self.SecurityAccess = self.env['woow_paas_platform.security_access']
        self.Route = self.env['woow_paas_platform.security_access_route']
        self.workspace = self.env['woow_paas_platform.workspace'].create(
            {'name': 'SA LC WS'})

    def _make_active_sa_with_routes(self, mock_client):
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            sa = self.SecurityAccess.create({
                'name': 'LC Gateway',
                'workspace_id': self.workspace.id,
            })
            sa.write({'state': 'active', 'tunnel_id': 'tun-lc'})
            self.Route.create({
                'security_access_id': sa.id,
                'subdomain_prefix': sa.canonical_name,
                'is_primary': True,
            })
            self.Route.create({
                'security_access_id': sa.id,
                'subdomain_prefix': 'lc-custom',
                'service_url': 'http://localhost:80',
            })
        return sa

    def _sync_mock(self):
        mock_client = MagicMock()
        mock_client.sync_tunnel_routes.return_value = {'routes': []}
        return mock_client

    def test_suspend_overrides_all_routes_to_503(self):
        mock_client = self._sync_mock()
        sa = self._make_active_sa_with_routes(mock_client)
        mock_client.sync_tunnel_routes.reset_mock()
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            sa._do_suspend()
        self.assertEqual(sa.state, 'suspended')
        self.assertEqual(sa.pre_suspend_state, 'active')
        kwargs = mock_client.sync_tunnel_routes.call_args.kwargs
        # 全部 route（含 primary）都 503；service_url 原值保留在 DB
        self.assertTrue(all(
            r['service_url'] == 'http_status:503' for r in kwargs['routes']))
        self.assertEqual(len(kwargs['routes']), 2)
        self.assertEqual(
            sa.route_ids.filtered(lambda r: not r.is_primary).service_url,
            'http://localhost:80')

    def test_suspend_idempotent(self):
        mock_client = self._sync_mock()
        sa = self._make_active_sa_with_routes(mock_client)
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            sa._do_suspend()
            calls_after_first = mock_client.sync_tunnel_routes.call_count
            sa._do_suspend()  # second call must early-return
        self.assertEqual(
            mock_client.sync_tunnel_routes.call_count, calls_after_first)

    def test_suspend_failure_sets_error_no_silent_retry(self):
        mock_client = self._sync_mock()
        sa = self._make_active_sa_with_routes(mock_client)
        mock_client.sync_tunnel_routes.side_effect = Exception('cf down')
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            sa._do_suspend()
        self.assertEqual(sa.state, 'error')

    def test_resume_restores_service_urls(self):
        mock_client = self._sync_mock()
        sa = self._make_active_sa_with_routes(mock_client)
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            sa._do_suspend()
            mock_client.sync_tunnel_routes.reset_mock()
            sa._do_resume()
        self.assertEqual(sa.state, 'active')
        kwargs = mock_client.sync_tunnel_routes.call_args.kwargs
        urls = {r['subdomain']: r['service_url'] for r in kwargs['routes']}
        self.assertEqual(urls['lc-custom'], 'http://localhost:80')
        self.assertNotIn('http_status:503', urls.values())

    def test_cleanup_deletes_tunnel(self):
        mock_client = self._sync_mock()
        sa = self._make_active_sa_with_routes(mock_client)
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            sa._do_cleanup()
        mock_client.delete_tunnel.assert_called_once_with('tun-lc')
        self.assertEqual(sa.state, 'deleting')
        self.assertFalse(sa.tunnel_id)

    def test_cleanup_reentrant_on_failure(self):
        """cleanup 失敗 → tunnel_id 保留，重跑可續刪（冪等重入）。"""
        mock_client = self._sync_mock()
        sa = self._make_active_sa_with_routes(mock_client)
        mock_client.delete_tunnel.side_effect = Exception('cf down')
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            sa._do_cleanup()
        self.assertEqual(sa.tunnel_id, 'tun-lc')  # kept for retry
        mock_client.delete_tunnel.side_effect = None
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            sa._cleanup_cloudflare()
        self.assertFalse(sa.tunnel_id)

    def test_direct_unlink_triggers_cf_cleanup(self):
        """direct-unlink-bypass-cleanup-hook 守線：直接 unlink 也要清 CF。"""
        mock_client = self._sync_mock()
        sa = self._make_active_sa_with_routes(mock_client)
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            sa.with_context(sa_force_delete_primary=True).unlink()
        mock_client.delete_tunnel.assert_called_once_with('tun-lc')
```

- [ ] **Step 2: 跑測試**

Run: `./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform:TestSecurityAccessLifecycleHooks --stop-after-init -u woow_paas_platform`
Expected: PASS（Task 8 已實作；任何 FAIL 表示實作有 bug，修 `security_access.py` 直到綠）

注意：`unlink()` 級聯會先刪 route（`ondelete='cascade'`），primary route 的 unlink guard 需要 context `sa_force_delete_primary`——`action_delete` 與 SA `unlink()` 中要以 `self.route_ids.with_context(sa_force_delete_primary=True)` 處理（若 Step 2 因此 FAIL，把 SA 的 `unlink()` 改為先 `self.route_ids.with_context(sa_force_delete_primary=True).unlink()` 再 super）。

- [ ] **Step 3: Commit**

```bash
git add src/tests/test_security_access.py
git commit -m "test: cover security access lifecycle hooks and cleanup chain"
```

---

### Task 10: Cron 掃描清單掛入 SA

**Files:**
- Modify: `src/models/subscription_lifecycle_cron.py:34-37`
- Test: `src/tests/test_security_access_lifecycle.py`（新檔）
- Modify: `src/tests/__init__.py`

- [ ] **Step 1: 寫失敗測試** — 建 `src/tests/test_security_access_lifecycle.py`（鏡像 `test_subscription_lifecycle.py` 的 `_mock_subscription` helper 風格；先讀該檔複製 helper 實作細節）：

```python
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase

MOCK_GET_CLIENT = (
    'odoo.addons.woow_paas_platform.models.security_access.'
    'get_paas_operator_client'
)


class TestSecurityAccessCron(TransactionCase):
    """SA 必須在 subscription lifecycle cron 的掃描清單內。"""

    def setUp(self):
        super().setUp()
        self.SecurityAccess = self.env['woow_paas_platform.security_access']
        self.CronModel = self.env[
            'woow_paas_platform.subscription_lifecycle_cron']
        self.workspace = self.env['woow_paas_platform.workspace'].create(
            {'name': 'SA Cron WS'})

    def _make_sa_with_sub(self, sub_state, **sub_kwargs):
        sa = self.SecurityAccess.create({
            'name': 'Cron Gateway',
            'workspace_id': self.workspace.id,
        })
        # route 先建（sa 還是 pending 不會觸發 operator sync），再轉 active
        self.env['woow_paas_platform.security_access_route'].create({
            'security_access_id': sa.id,
            'subdomain_prefix': sa.canonical_name,
            'is_primary': True,
        })
        sa.write({'state': 'active', 'tunnel_id': 'tun-cron'})
        sub_vals = {
            'sh_partner_id': self.env.user.partner_id.id,
            'state': sub_state,
        }
        sub_vals.update(sub_kwargs)
        sub = self.env['sh.subscription.subscription'].create(sub_vals)
        sa.write({'subscription_id': sub.id})
        return sa

    def test_cron_suspends_sa_past_grace(self):
        sa = self._make_sa_with_sub(
            'expire',
            sh_end_date=date.today() - timedelta(days=10),
        )
        mock_client = MagicMock()
        mock_client.sync_tunnel_routes.return_value = {'routes': []}
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            self.CronModel._cron_enforce_subscription_lifecycle()
        sa.invalidate_recordset()
        self.assertEqual(sa.state, 'suspended')

    def test_cron_resumes_sa_when_subscription_active(self):
        sa = self._make_sa_with_sub('in_progress')
        sa.write({'state': 'suspended', 'pre_suspend_state': 'active'})
        mock_client = MagicMock()
        mock_client.sync_tunnel_routes.return_value = {'routes': []}
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            self.CronModel._cron_enforce_subscription_lifecycle()
        sa.invalidate_recordset()
        self.assertEqual(sa.state, 'active')

    def test_cron_skips_grace_period(self):
        sa = self._make_sa_with_sub(
            'expire',
            sh_end_date=date.today() - timedelta(days=3),  # within 7d grace
        )
        with patch(MOCK_GET_CLIENT, return_value=MagicMock()):
            self.CronModel._cron_enforce_subscription_lifecycle()
        sa.invalidate_recordset()
        self.assertEqual(sa.state, 'active')
```

`src/tests/__init__.py` 加 `from . import test_security_access_lifecycle`

- [ ] **Step 2: 跑測試確認失敗**

Run: `./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform:TestSecurityAccessCron --stop-after-init -u woow_paas_platform`
Expected: FAIL — cron 跑完 SA 仍 `active`（不在掃描清單，silent gap）

- [ ] **Step 3: 實作** — `src/models/subscription_lifecycle_cron.py` 的 Model 清單改為：

```python
        for Model in [
            self.env['woow_paas_platform.cloud_service'],
            self.env['woow_paas_platform.smart_home'],
            self.env['woow_paas_platform.security_access'],
        ]:
```

- [ ] **Step 4: 跑測試確認通過**（同 Step 2），並跑 `--test-tags /woow_paas_platform:TestSubscriptionLifecycleCron` 確認 CS/SH 不回歸。

- [ ] **Step 5: Commit**

```bash
git add src/models/subscription_lifecycle_cron.py src/tests/test_security_access_lifecycle.py src/tests/__init__.py
git commit -m "feat: enforce security access subscriptions in lifecycle cron"
```

---

### Task 11: ACL — `ir.model.access.csv` + 對稱 ir.rule + ORM ACL 測試

SmartHome 當年漏掉 workspace-scoped ir.rule（探勘 G-M2）；SA 必須補上（spec §1 ACL 硬規則，B1-class 防線）。

**Files:**
- Modify: `src/security/ir.model.access.csv`
- Modify: `src/security/ir_rules.xml`
- Test: `src/tests/test_security_access.py`（追加 ACL class）

- [ ] **Step 1: 寫失敗測試** — 追加到 `test_security_access.py`：

```python
class TestSecurityAccessAcl(TransactionCase):
    """workspace-scoped ir.rule：非 member 看不到、member 看得到。"""

    def setUp(self):
        super().setUp()
        User = self.env['res.users']
        self.alice = User.create({
            'name': 'SA Alice', 'login': 'sa_acl_alice',
            'email': 'sa_acl_alice@test.example',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.mallory = User.create({
            'name': 'SA Mallory', 'login': 'sa_acl_mallory',
            'email': 'sa_acl_mallory@test.example',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'SA ACL WS', 'owner_id': self.alice.id,
        })
        # workspace owner 是否自動有 access record 依 workspace model 實作；
        # 保險起見明確建 alice 的 access（若已存在改用 search 確認）
        Access = self.env['woow_paas_platform.workspace_access']
        if not Access.search([('workspace_id', '=', self.workspace.id),
                              ('user_id', '=', self.alice.id)]):
            Access.create({
                'workspace_id': self.workspace.id,
                'user_id': self.alice.id, 'role': 'owner',
            })
        self.sa = self.env['woow_paas_platform.security_access'].create({
            'name': 'ACL Gateway', 'workspace_id': self.workspace.id,
        })
        self.route = self.env[
            'woow_paas_platform.security_access_route'].create({
                'security_access_id': self.sa.id,
                'subdomain_prefix': self.sa.canonical_name,
                'is_primary': True,
            })

    def test_member_can_read(self):
        visible = self.env['woow_paas_platform.security_access'].with_user(
            self.alice).search([('id', '=', self.sa.id)])
        self.assertEqual(len(visible), 1)

    def test_non_member_cannot_read(self):
        visible = self.env['woow_paas_platform.security_access'].with_user(
            self.mallory).search([('id', '=', self.sa.id)])
        self.assertEqual(len(visible), 0)

    def test_non_member_cannot_read_routes(self):
        """子模型 walk security_access_id.workspace_id 的 rule 也要生效。"""
        visible = self.env[
            'woow_paas_platform.security_access_route'].with_user(
                self.mallory).search([('id', '=', self.route.id)])
        self.assertEqual(len(visible), 0)

    def test_tunnel_token_field_masked_for_non_system(self):
        """tunnel_token groups='base.group_system' field mask。"""
        self.sa.sudo().write({'tunnel_token': 'super-secret'})
        sa_as_alice = self.env[
            'woow_paas_platform.security_access'].with_user(
                self.alice).browse(self.sa.id)
        from odoo.exceptions import AccessError
        with self.assertRaises(AccessError):
            _ = sa_as_alice.tunnel_token
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform:TestSecurityAccessAcl --stop-after-init -u woow_paas_platform`
Expected: FAIL — non_member 測試（沒有 ir.rule = 看得到全部，或 ACL 缺行直接 AccessError 在 member 測試）

- [ ] **Step 3: 實作 ACL 行** — `src/security/ir.model.access.csv` 加（放在 smart_home 兩行附近）：

```csv
access_security_access_user,woow_paas_platform.security_access.user,model_woow_paas_platform_security_access,base.group_user,1,1,1,1
access_security_access_admin,woow_paas_platform.security_access.admin,model_woow_paas_platform_security_access,base.group_system,1,1,1,1
access_security_access_route_user,woow_paas_platform.security_access_route.user,model_woow_paas_platform_security_access_route,base.group_user,1,1,1,1
access_security_access_route_admin,woow_paas_platform.security_access_route.admin,model_woow_paas_platform_security_access_route,base.group_system,1,1,1,1
```

（user 給 unlink=1：刪 SA / 自訂 route 是合法用戶操作，由 ir.rule 限 workspace + controller role check 把關 — 與 cloud_service 的 `1,1,1,1` 一致。）

- [ ] **Step 4: 實作 ir.rule** — `src/security/ir_rules.xml` 的 **noupdate="0"** `<data>` 區塊內（`cloud_service_rule_workspace_member` 之後）加：

```xml
        <!-- SecurityAccess: only workspace members may see / mutate.
             Symmetric ir.rule that SmartHome historically lacks (G-M2) —
             controller gating alone is insufficient because ORM-walking
             surfaces (backend views, m2o widgets) bypass controllers.
             See docs/decisions/patterns/two-layer-acl-lockstep.md. -->
        <record id="security_access_rule_workspace_member" model="ir.rule">
            <field name="name">SecurityAccess: workspace member access only</field>
            <field name="model_id" ref="model_woow_paas_platform_security_access"/>
            <field name="domain_force">[('workspace_id.access_ids.user_id', '=', user.id)]</field>
            <field name="groups" eval="[(4, ref('base.group_user'))]"/>
            <field name="perm_read" eval="True"/>
            <field name="perm_write" eval="True"/>
            <field name="perm_create" eval="True"/>
            <field name="perm_unlink" eval="True"/>
        </record>

        <!-- SecurityAccessRoute: walk parent SA's workspace. -->
        <record id="security_access_route_rule_workspace_member" model="ir.rule">
            <field name="name">SecurityAccessRoute: workspace member access only</field>
            <field name="model_id" ref="model_woow_paas_platform_security_access_route"/>
            <field name="domain_force">[('security_access_id.workspace_id.access_ids.user_id', '=', user.id)]</field>
            <field name="groups" eval="[(4, ref('base.group_user'))]"/>
            <field name="perm_read" eval="True"/>
            <field name="perm_write" eval="True"/>
            <field name="perm_create" eval="True"/>
            <field name="perm_unlink" eval="True"/>
        </record>
```

- [ ] **Step 5: 跑測試確認通過**（同 Step 2）Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/security/ir.model.access.csv src/security/ir_rules.xml src/tests/test_security_access.py
git commit -m "feat: add workspace-scoped acl and ir rules for security access"
```

---

### Task 12: Data seeds + Settings + hooks + 後台 views/menu + manifest

**Files:**
- Modify: `src/data/subscription_products.xml`、`src/data/subscription_template_links.xml`
- Create: `src/data/security_access_templates.xml`
- Modify: `src/models/res_config_settings.py`、`src/views/res_config_settings_views.xml`
- Modify: `src/hooks.py`
- Create: `src/views/security_access_views.xml`、`src/views/security_access_template_views.xml`
- Modify: `src/views/menu.xml`、`src/__manifest__.py`

- [ ] **Step 1: template seed** — 建 `src/data/security_access_templates.xml`（**noupdate="0"**，對齊 smart_home/cloud_app templates 慣例；noupdate=1 會讓 ir.model.data flag sticky，-u 時 links file 的 m2m write 被跳過 → 升級 DB 拿到零 plan template）：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<odoo>
    <data noupdate="0">
        <record id="security_access_default" model="woow_paas_platform.security_access_template">
            <field name="name">Security Access</field>
            <field name="slug">security-access</field>
            <field name="description">Zero Trust tunnel with self-managed reverse-proxy routing</field>
            <field name="full_description">One Cloudflare Tunnel, multiple custom subdomains. All traffic is routed to the reverse proxy (e.g. Nginx Proxy Manager) on your own host at localhost:80 — you decide how each subdomain maps to your internal services.</field>
        </record>
    </data>
</odoo>
```

- [ ] **Step 2: plans + products** — `src/data/subscription_products.xml` 在 Smart Home plans 區塊後加（**價格與 quota 為佔位值，正式定價待 Elmo 拍板** — spec「Access Control 計費」決議；Entry/Pro 差異維度見 spec「方案限額」）：

```xml
        <!-- Security Access Plans (pricing placeholder until Elmo's pricing lands) -->
        <record id="plan_security_access_entry_monthly" model="sh.subscription.plan">
            <field name="name">Security Access Entry Monthly</field>
            <field name="sh_duration">1</field>
            <field name="sh_unit">month</field>
            <field name="sh_plan_price">100</field>
            <field name="sh_override_product" eval="True"/>
            <field name="sh_no_of_billing_cycle">-1</field>
            <field name="sh_never_expire" eval="True"/>
            <field name="sh_trial" eval="True"/>
            <field name="sh_trial_duration">7</field>
            <field name="sh_trial_unit">day</field>
            <field name="sh_start_immediately" eval="False"/>
            <field name="sh_is_close_by_customer" eval="True"/>
            <field name="sh_reminder"
                   eval="[(4, ref('reminder_3days_before')), (4, ref('reminder_1day_before'))]"/>
            <field name="sa_max_routes">3</field>
            <field name="sa_max_access_policies">3</field>
            <field name="sa_max_custom_domains">1</field>
            <field name="sa_max_email_rules">0</field>
        </record>

        <record id="plan_security_access_entry_yearly" model="sh.subscription.plan">
            <field name="name">Security Access Entry Yearly</field>
            <field name="sh_duration">1</field>
            <field name="sh_unit">year</field>
            <field name="sh_plan_price">1000</field>
            <field name="sh_override_product" eval="True"/>
            <field name="sh_no_of_billing_cycle">-1</field>
            <field name="sh_never_expire" eval="True"/>
            <field name="sh_trial" eval="True"/>
            <field name="sh_trial_duration">7</field>
            <field name="sh_trial_unit">day</field>
            <field name="sh_start_immediately" eval="False"/>
            <field name="sh_is_close_by_customer" eval="True"/>
            <field name="sh_reminder"
                   eval="[(4, ref('reminder_3days_before')), (4, ref('reminder_1day_before'))]"/>
            <field name="sa_max_routes">3</field>
            <field name="sa_max_access_policies">3</field>
            <field name="sa_max_custom_domains">1</field>
            <field name="sa_max_email_rules">0</field>
        </record>

        <record id="plan_security_access_pro_monthly" model="sh.subscription.plan">
            <field name="name">Security Access Pro Monthly</field>
            <field name="sh_duration">1</field>
            <field name="sh_unit">month</field>
            <field name="sh_plan_price">300</field>
            <field name="sh_override_product" eval="True"/>
            <field name="sh_no_of_billing_cycle">-1</field>
            <field name="sh_never_expire" eval="True"/>
            <field name="sh_trial" eval="True"/>
            <field name="sh_trial_duration">7</field>
            <field name="sh_trial_unit">day</field>
            <field name="sh_start_immediately" eval="False"/>
            <field name="sh_is_close_by_customer" eval="True"/>
            <field name="sh_reminder"
                   eval="[(4, ref('reminder_3days_before')), (4, ref('reminder_1day_before'))]"/>
            <field name="sa_max_routes">10</field>
            <field name="sa_max_access_policies">10</field>
            <field name="sa_max_custom_domains">3</field>
            <field name="sa_max_email_rules">10</field>
        </record>

        <record id="plan_security_access_pro_yearly" model="sh.subscription.plan">
            <field name="name">Security Access Pro Yearly</field>
            <field name="sh_duration">1</field>
            <field name="sh_unit">year</field>
            <field name="sh_plan_price">3000</field>
            <field name="sh_override_product" eval="True"/>
            <field name="sh_no_of_billing_cycle">-1</field>
            <field name="sh_never_expire" eval="True"/>
            <field name="sh_trial" eval="True"/>
            <field name="sh_trial_duration">7</field>
            <field name="sh_trial_unit">day</field>
            <field name="sh_start_immediately" eval="False"/>
            <field name="sh_is_close_by_customer" eval="True"/>
            <field name="sh_reminder"
                   eval="[(4, ref('reminder_3days_before')), (4, ref('reminder_1day_before'))]"/>
            <field name="sa_max_routes">10</field>
            <field name="sa_max_access_policies">10</field>
            <field name="sa_max_custom_domains">3</field>
            <field name="sa_max_email_rules">10</field>
        </record>

        <!-- Products (one per plan) -->
        <record id="product_security_access_entry_monthly" model="product.product">
            <field name="name">Security Access Entry Monthly Subscription</field>
            <field name="type">service</field>
            <field name="list_price">100</field>
            <field name="sale_ok" eval="True"/>
            <field name="purchase_ok" eval="False"/>
            <field name="sh_product_subscribe" eval="True"/>
            <field name="sh_subscription_plan_id" ref="plan_security_access_entry_monthly"/>
        </record>
        <record id="product_security_access_entry_yearly" model="product.product">
            <field name="name">Security Access Entry Yearly Subscription</field>
            <field name="type">service</field>
            <field name="list_price">1000</field>
            <field name="sale_ok" eval="True"/>
            <field name="purchase_ok" eval="False"/>
            <field name="sh_product_subscribe" eval="True"/>
            <field name="sh_subscription_plan_id" ref="plan_security_access_entry_yearly"/>
        </record>
        <record id="product_security_access_pro_monthly" model="product.product">
            <field name="name">Security Access Pro Monthly Subscription</field>
            <field name="type">service</field>
            <field name="list_price">300</field>
            <field name="sale_ok" eval="True"/>
            <field name="purchase_ok" eval="False"/>
            <field name="sh_product_subscribe" eval="True"/>
            <field name="sh_subscription_plan_id" ref="plan_security_access_pro_monthly"/>
        </record>
        <record id="product_security_access_pro_yearly" model="product.product">
            <field name="name">Security Access Pro Yearly Subscription</field>
            <field name="type">service</field>
            <field name="list_price">3000</field>
            <field name="sale_ok" eval="True"/>
            <field name="purchase_ok" eval="False"/>
            <field name="sh_product_subscribe" eval="True"/>
            <field name="sh_subscription_plan_id" ref="plan_security_access_pro_yearly"/>
        </record>
```

- [ ] **Step 3: template link** — `src/data/subscription_template_links.xml` 末尾（smart_home link 之後）加：

```xml
        <!-- Link security_access_template to its subscription plans -->
        <record id="security_access_default" model="woow_paas_platform.security_access_template">
            <field name="subscription_plan_ids"
                   eval="[(6, 0, [ref('plan_security_access_entry_monthly'),
                                  ref('plan_security_access_entry_yearly'),
                                  ref('plan_security_access_pro_monthly'),
                                  ref('plan_security_access_pro_yearly')])]"/>
        </record>
```

- [ ] **Step 4: Settings 欄位** — `src/models/res_config_settings.py` 加（Domain Configuration 區塊附近）：

```python
    # Security Access
    woow_sa_default_service_url = fields.Char(
        string='SA Default Service URL',
        config_parameter='woow_paas_platform.sa_default_service_url',
        default='http://localhost:80',
        help='Default reverse-proxy target for new Security Access '
             'application routes. Users are expected to run a Host-header '
             'aware reverse proxy (e.g. NPM) at this address.',
    )
```

`src/views/res_config_settings_views.xml` 在 Domain Configuration setting 後加：

```xml
                        <setting string="Security Access"
                                 help="Default reverse proxy target for new application routes.">
                            <div class="content-group">
                                <div class="row mt16">
                                    <label for="woow_sa_default_service_url" class="col-lg-3 o_light_label"/>
                                    <field name="woow_sa_default_service_url"
                                           placeholder="http://localhost:80"
                                           class="col-lg-6"/>
                                </div>
                            </div>
                        </setting>
```

`src/hooks.py` 的 `_CONFIG_PARAMETERS` 加一行（CLAUDE.md 卸載清理規範）：

```python
    'woow_paas_platform.sa_default_service_url',
```

- [ ] **Step 5: 後台 views + menu** — `src/views/menu.xml` 加子選單群組（Smart Home 之後）：

```xml
    <!-- Security Access 子選單群組 -->
    <menuitem
        id="menu_security_access"
        name="Security Access"
        parent="menu_woow_paas_platform_root"
        sequence="50"/>
```

建 `src/views/security_access_views.xml`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<odoo>
    <!-- Security Access Form View -->
    <record id="view_security_access_form" model="ir.ui.view">
        <field name="name">woow_paas_platform.security_access.form</field>
        <field name="model">woow_paas_platform.security_access</field>
        <field name="arch" type="xml">
            <form string="Security Access">
                <header>
                    <button name="action_provision" type="object"
                            string="Provision" icon="fa-cloud-upload"
                            class="oe_highlight"
                            invisible="state not in ('pending', 'error')"/>
                    <button name="action_refresh_status" type="object"
                            string="Refresh Status" icon="fa-refresh"
                            invisible="state != 'active'"/>
                    <field name="state" widget="statusbar"
                           statusbar_visible="pending,provisioning,active"/>
                </header>
                <sheet>
                    <div class="oe_title">
                        <h1>
                            <field name="name" placeholder="Nickname (e.g. secure-gateway)"/>
                        </h1>
                    </div>
                    <group>
                        <group string="Identity">
                            <field name="workspace_id"/>
                            <field name="template_id"/>
                            <field name="reference_id" readonly="1"/>
                            <field name="canonical_name" readonly="1"/>
                            <field name="primary_hostname" readonly="1"/>
                        </group>
                        <group string="Tunnel">
                            <field name="tunnel_id" readonly="1"/>
                            <field name="tunnel_status" readonly="1"/>
                            <field name="connector_type" readonly="1"/>
                            <field name="deployed_at" readonly="1"/>
                        </group>
                    </group>
                    <notebook>
                        <page string="Application Routes" name="routes">
                            <field name="route_ids">
                                <list editable="bottom">
                                    <field name="subdomain_prefix"/>
                                    <field name="hostname" readonly="1"/>
                                    <field name="service_url"/>
                                    <field name="http_host_header"/>
                                    <field name="is_protected"/>
                                    <field name="is_primary" readonly="1"/>
                                </list>
                            </field>
                        </page>
                        <page string="Subscription" name="subscription">
                            <group>
                                <field name="subscription_id" readonly="1"/>
                                <field name="subscription_state" readonly="1"/>
                                <field name="subscription_plan_name" readonly="1"/>
                                <field name="subscription_next_payment" readonly="1"/>
                            </group>
                        </page>
                        <page string="Errors" name="errors"
                              invisible="not error_message">
                            <field name="error_message" readonly="1"/>
                        </page>
                    </notebook>
                </sheet>
            </form>
        </field>
    </record>

    <!-- Security Access List View -->
    <record id="view_security_access_list" model="ir.ui.view">
        <field name="name">woow_paas_platform.security_access.list</field>
        <field name="model">woow_paas_platform.security_access</field>
        <field name="arch" type="xml">
            <list string="Security Access"
                  decoration-success="state == 'active'"
                  decoration-warning="state == 'suspended'"
                  decoration-danger="state == 'error'">
                <field name="name"/>
                <field name="canonical_name"/>
                <field name="workspace_id"/>
                <field name="state" widget="badge"/>
                <field name="tunnel_status"/>
                <field name="route_count"/>
                <field name="subscription_state"/>
            </list>
        </field>
    </record>

    <!-- Action + Menu -->
    <record id="action_security_access" model="ir.actions.act_window">
        <field name="name">Security Access</field>
        <field name="res_model">woow_paas_platform.security_access</field>
        <field name="view_mode">list,form</field>
        <field name="help" type="html">
            <p class="o_view_nocontent_smiling_face">
                No Security Access instances yet
            </p>
            <p>
                Security Access instances are normally created by users
                from the /woow frontend with a subscription.
            </p>
        </field>
    </record>

    <menuitem
        id="menu_security_access_instances"
        name="Instances"
        parent="menu_security_access"
        action="action_security_access"
        sequence="5"/>
</odoo>
```

建 `src/views/security_access_template_views.xml`（完全鏡像 `smart_home_template_views.xml` 結構，model 換 `woow_paas_platform.security_access_template`、id 換 `view_security_access_template_form` / `view_security_access_template_list` / `action_security_access_template` / `menu_security_access_templates`、menu parent 用 `menu_security_access`、sequence=10、字樣換 Security Access）。

- [ ] **Step 6: manifest** — `src/__manifest__.py` data 清單：在 `"data/smart_home_templates.xml"` 後加 `"data/security_access_templates.xml"`；在 `"views/smart_home_template_views.xml"` 後加 `"views/security_access_template_views.xml",` 與 `"views/security_access_views.xml",`。

- [ ] **Step 7: 升級驗證 + 既有測試**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --stop-after-init`
Expected: 無 XML/CSV 載入錯誤。
Run: `./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform:TestSecurityAccessTemplate --stop-after-init -u woow_paas_platform`
Expected: PASS（seed 與 constraint 不衝突）

- [ ] **Step 8: Commit**

```bash
git add src/data/ src/models/res_config_settings.py src/views/ src/hooks.py src/__manifest__.py
git commit -m "feat: add security access seeds settings backend views and menu"
```

---

### Task 13: BFF Controller — collection / detail / routes / availability

**Files:**
- Create: `src/controllers/security_access.py`
- Modify: `src/controllers/__init__.py`
- Test: `src/tests/test_security_access_subscription_flow.py`（新檔）
- Modify: `src/tests/__init__.py`

- [ ] **Step 1: 寫失敗測試** — 建 `src/tests/test_security_access_subscription_flow.py`（鏡像 `test_smart_home_subscription_flow.py` 的 `_patch_request` 風格）：

```python
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase

from odoo.addons.woow_paas_platform.controllers.security_access import (
    SecurityAccessController,
)

MOCK_GET_CLIENT = (
    'odoo.addons.woow_paas_platform.models.security_access.'
    'get_paas_operator_client'
)


def _patch_request(env):
    return patch(
        'odoo.addons.woow_paas_platform.controllers.security_access.request',
        new=MagicMock(env=env),
    )


class TestSecurityAccessControllerFlow(TransactionCase):

    def setUp(self):
        super().setUp()
        self.controller = SecurityAccessController()
        self.SecurityAccess = self.env['woow_paas_platform.security_access']
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'SA Ctrl WS', 'owner_id': self.env.user.id,
        })
        Access = self.env['woow_paas_platform.workspace_access']
        if not Access.search([('workspace_id', '=', self.workspace.id),
                              ('user_id', '=', self.env.user.id)]):
            Access.create({
                'workspace_id': self.workspace.id,
                'user_id': self.env.user.id, 'role': 'owner',
            })
        self.plan = self.env['sh.subscription.plan'].create({
            'name': 'SA Ctrl Plan', 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 100, 'sh_no_of_billing_cycle': -1,
            'sa_max_routes': 1,
        })
        self.product = self.env['product.product'].create({
            'name': 'SA Ctrl Product', 'type': 'service',
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': self.plan.id,
        })
        self.template = self.env[
            'woow_paas_platform.security_access_template'].create({
                'name': 'SA Ctrl Tmpl',
                'subscription_plan_ids': [(6, 0, [self.plan.id])],
            })
        # journal 設定（_create_and_confirm_subscription 前置；鏡像
        # test_smart_home_subscription_flow.py setUp 的 journal 處理）
        company = self.env.company.sudo()
        if not company.sh_journal_id:
            journal = self.env['account.journal'].sudo().search(
                [('type', '=', 'sale')], limit=1)
            if journal:
                company.sh_journal_id = journal.id

    def test_create_happy_path_creates_subscription(self):
        with _patch_request(self.env):
            res = self.controller._create_security_access(
                workspace=self.workspace,
                name='Happy Gateway',
                plan_id=self.plan.id,
                template_id=self.template.id,
            )
        self.assertTrue(res['success'], res.get('error'))
        sa = self.SecurityAccess.search([('name', '=', 'Happy Gateway')],
                                        limit=1)
        self.assertTrue(sa.subscription_id)
        self.assertEqual(sa.subscription_id.sh_subscription_plan_id, self.plan)

    def test_create_rejects_plan_not_in_template(self):
        other_plan = self.env['sh.subscription.plan'].create({
            'name': 'Foreign Plan', 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 50, 'sh_no_of_billing_cycle': -1,
        })
        with _patch_request(self.env):
            res = self.controller._create_security_access(
                workspace=self.workspace,
                name='Bad Gateway',
                plan_id=other_plan.id,
                template_id=self.template.id,
            )
        self.assertFalse(res['success'])

    def test_route_check_reports_db_conflict(self):
        sa = self.SecurityAccess.create({
            'name': 'Check Gateway', 'workspace_id': self.workspace.id,
        })
        sa.write({'state': 'active', 'tunnel_id': 'tun-x'})
        with patch(MOCK_GET_CLIENT) as mock_get_client:
            mock_client = MagicMock()
            mock_client.sync_tunnel_routes.return_value = {'routes': []}
            mock_client.check_route_availability.return_value = {
                'available': True}
            mock_get_client.return_value = mock_client
            self.env['woow_paas_platform.security_access_route'].create({
                'security_access_id': sa.id,
                'subdomain_prefix': 'check-taken',
            })
            with _patch_request(self.env):
                res = self.controller._check_route_prefix('check-taken')
        self.assertTrue(res['success'])
        self.assertFalse(res['data']['available'])

    def test_route_quota_enforced_at_controller(self):
        """plan sa_max_routes=1：第二條自訂 route 在 controller 層被擋。"""
        with _patch_request(self.env), patch(MOCK_GET_CLIENT) as mock_gc:
            mock_client = MagicMock()
            mock_client.sync_tunnel_routes.return_value = {'routes': []}
            mock_client.check_route_availability.return_value = {
                'available': True}
            mock_gc.return_value = mock_client
            res = self.controller._create_security_access(
                workspace=self.workspace, name='Quota GW',
                plan_id=self.plan.id, template_id=self.template.id,
            )
            sa = self.SecurityAccess.browse(res['data']['id'])
            sa.write({'state': 'active', 'tunnel_id': 'tun-q'})
            r1 = self.controller._create_route(sa, 'quota-r1',
                                               'http://localhost:80', True)
            self.assertTrue(r1['success'], r1.get('error'))
            r2 = self.controller._create_route(sa, 'quota-r2',
                                               'http://localhost:80', True)
        self.assertFalse(r2['success'])
        self.assertIn('quota', r2['error'].lower())
```

`src/tests/__init__.py` 加 `from . import test_security_access_subscription_flow`

- [ ] **Step 2: 跑測試確認失敗**

Run: `./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform:TestSecurityAccessControllerFlow --stop-after-init -u woow_paas_platform`
Expected: FAIL — `ImportError: SecurityAccessController`

- [ ] **Step 3: 實作** — 建 `src/controllers/security_access.py`：

```python
"""Security Access API controller.

JSON-RPC endpoints for Security Access CRUD within a workspace context.
Mirrors controllers/smart_home.py gating: _get_workspace_or_error for
visibility, owner/admin role check for mutations, NO .sudo() on
list/get paths (orm-vs-endpoint-acl-test-coverage pattern).
"""
from __future__ import annotations

import logging
from typing import Any

from odoo.http import request, route, Controller

from ..models.workspace_access import ROLE_OWNER, ROLE_ADMIN

_logger = logging.getLogger(__name__)


class SecurityAccessController(Controller):

    # ==================== Collection ====================

    @route(
        "/api/workspaces/<int:workspace_id>/security-accesses",
        auth="user", methods=["POST"], type="json",
    )
    def api_security_accesses(
        self,
        workspace_id: int,
        action: str = "list",
        name: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        workspace = self._get_workspace_or_error(workspace_id)
        if not workspace:
            return {"success": False,
                    "error": "Workspace not found or access denied"}

        if action == "list":
            return self._list_security_accesses(workspace)
        elif action == "create":
            access = workspace.check_user_access(request.env.user)
            if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
                return {"success": False, "error": "Insufficient permissions"}
            return self._create_security_access(
                workspace, name,
                kwargs.get("plan_id"), kwargs.get("template_id"),
            )
        return {"success": False, "error": f"Unknown action: {action}"}

    # ==================== Detail ====================

    @route(
        "/api/workspaces/<int:workspace_id>/security-accesses/<int:sa_id>",
        auth="user", methods=["POST"], type="json",
    )
    def api_security_access_detail(
        self,
        workspace_id: int,
        sa_id: int,
        action: str = "get",
        **kwargs: Any,
    ) -> dict[str, Any]:
        workspace = self._get_workspace_or_error(workspace_id)
        if not workspace:
            return {"success": False,
                    "error": "Workspace not found or access denied"}
        sa = self._get_sa_or_error(sa_id, workspace)
        if not sa:
            return {"success": False, "error": "Security access not found"}

        if action == "get":
            return {"success": True, "data": sa.to_dict()}
        elif action == "refresh_status":
            try:
                sa.action_refresh_status()
            except Exception as e:
                return {"success": False, "error": str(e)}
            return {"success": True, "data": sa.to_dict()}

        # mutations below require owner/admin
        access = workspace.check_user_access(request.env.user)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {"success": False, "error": "Insufficient permissions"}

        if action == "update":
            vals = {}
            if kwargs.get("name"):
                vals["name"] = kwargs["name"]
            if vals:
                sa.write(vals)
            return {"success": True, "data": sa.to_dict()}
        elif action == "provision":
            try:
                sa._check_operation_allowed(operation='write')
                sa.action_provision()
            except Exception as e:
                return {"success": False, "error": str(e)}
            return {"success": True, "data": sa.to_dict()}
        elif action == "delete":
            try:
                sa._check_operation_allowed(operation='write')
                sa.action_delete()
            except Exception as e:
                return {"success": False, "error": str(e)}
            return {"success": True,
                    "data": {"message": "Security access deleted"}}
        return {"success": False, "error": f"Unknown action: {action}"}

    # ==================== Routes ====================

    @route(
        "/api/workspaces/<int:workspace_id>/security-accesses/<int:sa_id>/routes",
        auth="user", methods=["POST"], type="json",
    )
    def api_security_access_routes(
        self,
        workspace_id: int,
        sa_id: int,
        action: str = "list",
        **kwargs: Any,
    ) -> dict[str, Any]:
        workspace = self._get_workspace_or_error(workspace_id)
        if not workspace:
            return {"success": False,
                    "error": "Workspace not found or access denied"}
        sa = self._get_sa_or_error(sa_id, workspace)
        if not sa:
            return {"success": False, "error": "Security access not found"}

        if action == "list":
            return {"success": True,
                    "data": [r.to_dict() for r in sa.route_ids]}

        access = workspace.check_user_access(request.env.user)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {"success": False, "error": "Insufficient permissions"}
        try:
            sa._check_operation_allowed(operation='write')
        except Exception as e:
            return {"success": False, "error": str(e)}

        if action == "create":
            return self._create_route(
                sa,
                kwargs.get("subdomain_prefix"),
                kwargs.get("service_url"),
                kwargs.get("http_host_header", True),
            )
        elif action == "update":
            return self._update_route(sa, kwargs.get("route_id"), kwargs)
        elif action == "delete":
            return self._delete_route(sa, kwargs.get("route_id"))
        return {"success": False, "error": f"Unknown action: {action}"}

    # ==================== Availability (platform-level) ====================

    @route(
        "/api/security-access/routes/check",
        auth="user", methods=["POST"], type="json",
    )
    def api_route_check(self, prefix: str | None = None,
                        **kwargs: Any) -> dict[str, Any]:
        if not prefix:
            return {"success": False, "error": "prefix is required"}
        return self._check_route_prefix(prefix)

    # ==================== Private Helpers ====================

    def _get_workspace_or_error(self, workspace_id: int):
        user = request.env.user
        WorkspaceAccess = request.env["woow_paas_platform.workspace_access"]
        access = WorkspaceAccess.search([
            ("user_id", "=", user.id),
            ("workspace_id", "=", workspace_id),
            ("workspace_id.state", "=", "active"),
        ], limit=1)
        if not access:
            return None
        return access.workspace_id

    def _get_sa_or_error(self, sa_id: int, workspace):
        SecurityAccess = request.env["woow_paas_platform.security_access"]
        sa = SecurityAccess.browse(sa_id).exists()
        if not sa or sa.workspace_id.id != workspace.id:
            return None
        return sa

    def _list_security_accesses(self, workspace) -> dict[str, Any]:
        SecurityAccess = request.env["woow_paas_platform.security_access"]
        records = SecurityAccess.search([
            ("workspace_id", "=", workspace.id),
        ])
        data = [r.to_dict() for r in records]
        return {"success": True, "data": data, "count": len(data)}

    def _create_security_access(
        self, workspace, name: str | None,
        plan_id: int | None = None,
        template_id: int | None = None,
    ) -> dict[str, Any]:
        """Mirrors smart_home._create_smarthome validation order."""
        if not name:
            return {"success": False, "error": "Name is required"}
        if not template_id:
            return {"success": False,
                    "error": "Security Access template is required"}
        if not plan_id:
            return {"success": False,
                    "error": "Subscription plan is required"}

        Template = request.env[
            "woow_paas_platform.security_access_template"].sudo()
        template = Template.browse(template_id)
        if not template.exists() or not template.is_active:
            return {"success": False,
                    "error": "Security Access template not found or inactive"}
        if not template.subscription_plan_ids:
            return {"success": False,
                    "error": "Template has no subscription plans configured"}

        # sudo: catalog data, portal users lack plan ACL (mirrors smart_home)
        Plan = request.env["sh.subscription.plan"].sudo()
        plan = Plan.browse(plan_id)
        if not plan.exists():
            return {"success": False, "error": "Subscription plan not found"}
        if plan not in template.subscription_plan_ids:
            return {"success": False,
                    "error": "Selected plan is not available for this template"}

        SecurityAccess = request.env["woow_paas_platform.security_access"]
        try:
            sa = SecurityAccess.create({
                "name": name,
                "workspace_id": workspace.id,
                "template_id": template.id,
            })
            partner = workspace.owner_id.partner_id
            _logger.info(
                "[SA] create sa=%s workspace=%s partner=%s plan=%s",
                sa.id, workspace.id, partner.id, plan.id,
            )
            sa._create_and_confirm_subscription(partner, plan)
            return {"success": True, "data": sa.to_dict()}
        except Exception as e:
            _logger.exception("[SA] Failed to create security access: %s", e)
            return {"success": False,
                    "error": "Failed to create security access. Please try again."}

    def _check_route_prefix(self, prefix: str) -> dict[str, Any]:
        """Two-layer availability: Odoo DB first, then Cloudflare DNS."""
        Route = request.env[
            "woow_paas_platform.security_access_route"].sudo()
        # sudo justified: existence check across ALL workspaces is the very
        # point of a flat-namespace availability check; returns only a
        # boolean, no record data crosses the tenant boundary.
        db_taken = bool(Route.search_count(
            [("subdomain_prefix", "=", prefix)]))
        if db_taken:
            return {"success": True,
                    "data": {"prefix": prefix, "available": False,
                             "reason": "already used by another route"}}
        try:
            from ..services.paas_operator import get_paas_operator_client
            client = get_paas_operator_client(request.env)
            if client:
                result = client.check_route_availability(prefix)
                if not result.get("available", True):
                    return {"success": True,
                            "data": {"prefix": prefix, "available": False,
                                     "reason": "DNS record already exists"}}
        except Exception as e:
            _logger.warning("[SA] CF availability check failed: %s", e)
            # fail-open to DB-only result: CF check is best-effort feedback;
            # the DB UNIQUE constraint + CF-side create error still protect
            # the actual create path.
        return {"success": True,
                "data": {"prefix": prefix, "available": True}}

    def _create_route(self, sa, subdomain_prefix, service_url,
                      http_host_header) -> dict[str, Any]:
        if not subdomain_prefix:
            return {"success": False, "error": "subdomain_prefix is required"}

        # two-layer quota: constraint re-checks at ORM layer
        sub = sa.sudo().subscription_id
        if sub and sub.sh_subscription_plan_id:
            quota = sub.sh_subscription_plan_id.sa_max_routes
            count = len(sa.route_ids.filtered(lambda r: not r.is_primary))
            if count >= quota:
                return {"success": False,
                        "error": "Route quota exceeded: plan allows %d "
                                 "custom route(s)" % quota}

        check = self._check_route_prefix(subdomain_prefix)
        if not check["data"]["available"]:
            return {"success": False,
                    "error": "Subdomain prefix is not available: %s"
                             % check["data"].get("reason", "taken")}

        Route = request.env["woow_paas_platform.security_access_route"]
        vals = {
            "security_access_id": sa.id,
            "subdomain_prefix": subdomain_prefix,
            "http_host_header": bool(http_host_header),
        }
        if service_url:
            vals["service_url"] = service_url
        try:
            route_rec = Route.create(vals)
            return {"success": True, "data": route_rec.to_dict()}
        except Exception as e:
            _logger.exception("[SA] Failed to create route: %s", e)
            return {"success": False, "error": str(e)}

    def _update_route(self, sa, route_id, kwargs) -> dict[str, Any]:
        route_rec = sa.route_ids.filtered(lambda r: r.id == route_id)
        if not route_rec:
            return {"success": False, "error": "Route not found"}
        vals = {}
        if "service_url" in kwargs and kwargs["service_url"]:
            vals["service_url"] = kwargs["service_url"]
        if "http_host_header" in kwargs:
            vals["http_host_header"] = bool(kwargs["http_host_header"])
        if "is_protected" in kwargs:
            vals["is_protected"] = bool(kwargs["is_protected"])
        try:
            if vals:
                route_rec.write(vals)
            return {"success": True, "data": route_rec.to_dict()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _delete_route(self, sa, route_id) -> dict[str, Any]:
        route_rec = sa.route_ids.filtered(lambda r: r.id == route_id)
        if not route_rec:
            return {"success": False, "error": "Route not found"}
        try:
            route_rec.unlink()
            return {"success": True, "data": {"message": "Route deleted"}}
        except Exception as e:
            return {"success": False, "error": str(e)}
```

`src/controllers/__init__.py` 加 `from . import security_access`

- [ ] **Step 4: 跑測試確認通過**（同 Step 2）Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/controllers/security_access.py src/controllers/__init__.py src/tests/test_security_access_subscription_flow.py src/tests/__init__.py
git commit -m "feat: add security access bff controller with route management"
```

---

### Task 14: BFF Controller — 訂閱三件組 + templates/plans 端點

**Files:**
- Modify: `src/controllers/security_access.py`
- Test: `src/tests/test_security_access_subscription_flow.py`（追加）

- [ ] **Step 1: 寫失敗測試** — 追加：

```python
class TestSecurityAccessPlanEndpoints(TransactionCase):
    """templates / plans / skip-trial-estimate 端點."""

    def setUp(self):
        super().setUp()
        self.controller = SecurityAccessController()
        self.plan = self.env['sh.subscription.plan'].create({
            'name': 'SA Plans EP', 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 100, 'sh_no_of_billing_cycle': -1,
            'sh_trial': True, 'sh_trial_duration': 7, 'sh_trial_unit': 'day',
            'sa_max_routes': 3,
        })
        self.template = self.env[
            'woow_paas_platform.security_access_template'].create({
                'name': 'SA Plans Tmpl',
                'subscription_plan_ids': [(6, 0, [self.plan.id])],
            })

    def test_list_templates(self):
        with _patch_request(self.env):
            res = self.controller.api_sa_templates()
        self.assertTrue(res['success'])
        names = [t['name'] for t in res['data']]
        self.assertIn('SA Plans Tmpl', names)

    def test_template_plans_include_quota(self):
        with _patch_request(self.env):
            res = self.controller.api_sa_template_plans(self.template.id)
        self.assertTrue(res['success'])
        plan_data = res['data'][0]
        self.assertEqual(plan_data['max_routes'], 3)
        self.assertTrue(plan_data['has_trial'])

    def test_template_without_plans_hard_fails(self):
        empty = self.env[
            'woow_paas_platform.security_access_template'].create({
                'name': 'Empty Tmpl'})
        with _patch_request(self.env):
            res = self.controller.api_sa_template_plans(empty.id)
        self.assertFalse(res['success'])
```

- [ ] **Step 2: 跑測試確認失敗**（method 不存在）

- [ ] **Step 3: 實作** — `security_access.py` 加四個端點（鏡像 `smart_home.py` 的 templates/plans/skip-trial/change-plan，差異只在 model 名與 quota 欄位）：

```python
    # ==================== Templates / Plans (platform-level) ============

    @route(
        "/api/security-access/templates",
        auth="user", methods=["POST"], type="json",
    )
    def api_sa_templates(self, **kwargs: Any) -> dict[str, Any]:
        """Catalog data — sudo justified (mirrors smart_home templates)."""
        Template = request.env[
            "woow_paas_platform.security_access_template"].sudo()
        templates = Template.search([("is_active", "=", True)])
        return {"success": True, "data": [
            {"id": t.id, "name": t.name, "slug": t.slug,
             "description": t.description}
            for t in templates
        ]}

    @route(
        "/api/security-access/templates/<int:template_id>/plans",
        auth="user", methods=["POST"], type="json",
    )
    def api_sa_template_plans(self, template_id: int,
                              **kwargs: Any) -> dict[str, Any]:
        Template = request.env[
            "woow_paas_platform.security_access_template"].sudo()
        template = Template.browse(template_id)
        if not template.exists() or not template.is_active:
            return {"success": False,
                    "error": "Security Access template not found or inactive"}
        if not template.subscription_plan_ids:
            return {"success": False,
                    "error": "Template has no subscription plans configured"}
        return {"success": True, "data": [
            {
                "id": p.id,
                "name": p.name,
                "price": p.sh_plan_price,
                "duration": p.sh_duration,
                "unit": p.sh_unit,
                "has_trial": p.sh_trial,
                "trial_duration": p.sh_trial_duration if p.sh_trial else 0,
                "trial_unit": p.sh_trial_unit if p.sh_trial else None,
                "max_routes": p.sa_max_routes,
                "max_access_policies": p.sa_max_access_policies,
                "max_custom_domains": p.sa_max_custom_domains,
                "max_email_rules": p.sa_max_email_rules,
            }
            for p in template.subscription_plan_ids
        ]}

    # ==================== Subscription trio ====================

    @route(
        "/api/workspaces/<int:workspace_id>/security-accesses/<int:sa_id>/skip-trial-estimate",
        auth="user", methods=["POST"], type="json",
    )
    def api_sa_skip_trial_estimate(self, workspace_id: int, sa_id: int,
                                   **kwargs: Any) -> dict[str, Any]:
        workspace = self._get_workspace_or_error(workspace_id)
        if not workspace:
            return {"success": False,
                    "error": "Workspace not found or access denied"}
        access = workspace.check_user_access(request.env.user)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {"success": False, "error": "Insufficient permissions"}
        sa = self._get_sa_or_error(sa_id, workspace)
        if not sa:
            return {"success": False, "error": "Security access not found"}
        try:
            return {"success": True, "data": sa._skip_trial_estimate()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @route(
        "/api/workspaces/<int:workspace_id>/security-accesses/<int:sa_id>/skip-trial",
        auth="user", methods=["POST"], type="json",
    )
    def api_sa_skip_trial(self, workspace_id: int, sa_id: int,
                          **kwargs: Any) -> dict[str, Any]:
        workspace = self._get_workspace_or_error(workspace_id)
        if not workspace:
            return {"success": False,
                    "error": "Workspace not found or access denied"}
        access = workspace.check_user_access(request.env.user)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {"success": False, "error": "Insufficient permissions"}
        sa = self._get_sa_or_error(sa_id, workspace)
        if not sa:
            return {"success": False, "error": "Security access not found"}
        try:
            invoice_info = sa._skip_trial()
        except Exception as e:
            return {"success": False, "error": str(e)}
        return {"success": True, "data": {
            "invoice": invoice_info,
            "subscription": sa._get_subscription_info(),
        }}

    @route(
        "/api/workspaces/<int:workspace_id>/security-accesses/<int:sa_id>/change-plan",
        auth="user", methods=["POST"], type="json",
    )
    def api_sa_change_plan(self, workspace_id: int, sa_id: int,
                           plan_id: int | None = None,
                           **kwargs: Any) -> dict[str, Any]:
        """完全鏡像 smart_home.api_smarthome_change_plan 的流程與 sudo 理由：
        refund 計算 → 退 wallet → close 舊訂閱 → 開新訂閱。"""
        if not plan_id:
            return {"success": False, "error": "plan_id is required"}
        workspace = self._get_workspace_or_error(workspace_id)
        if not workspace:
            return {"success": False,
                    "error": "Workspace not found or access denied"}
        sa = self._get_sa_or_error(sa_id, workspace)
        if not sa:
            return {"success": False, "error": "Security access not found"}
        access = workspace.check_user_access(request.env.user)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {"success": False, "error": "Insufficient permissions"}

        Plan = request.env["sh.subscription.plan"].sudo()
        plan = Plan.browse(plan_id)
        if not plan.exists():
            return {"success": False, "error": "Plan not found"}

        old_sub = sa.sudo().subscription_id
        if not old_sub or old_sub.state != "in_progress":
            return {"success": False, "error": "No active subscription"}
        if not old_sub.sh_trial:
            if not old_sub.sh_end_date or not old_sub.sh_start_date:
                return {"success": False,
                        "error": "Subscription dates are incomplete"}

        refund = old_sub._compute_unused_refund()
        partner = old_sub.sh_partner_id
        if refund > 0:
            partner.sudo().add_wallet_points(
                amount=refund,
                description="Plan change refund: %s"
                            % old_sub.sh_subscription_plan_id.name,
            )
            partner.invalidate_recordset(['wallet_balance'])
        old_sub.sudo().write({
            "state": "close",
            "sh_reason": "Plan changed to: %s" % plan.name,
        })
        sa._create_and_confirm_subscription(partner, plan)
        return {"success": True, "data": {
            "refund_amount": refund,
            "new_plan": plan.name,
            "subscription": sa._get_subscription_info(),
        }}
```

- [ ] **Step 4: 跑測試確認通過**

Run: `./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform:TestSecurityAccessPlanEndpoints --stop-after-init -u woow_paas_platform`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/controllers/security_access.py src/tests/test_security_access_subscription_flow.py
git commit -m "feat: add security access subscription and plan endpoints"
```

---

### Task 15: HttpCase cross-user negative 測試（endpoint-level ACL 守線）

ORM 綠 ≠ endpoint secure（`orm-vs-endpoint-acl-test-coverage` pattern）：每個 list/get endpoint 配 cross-user negative。

**Files:**
- Test: `src/tests/test_security_access_endpoints.py`（新檔）
- Modify: `src/tests/__init__.py`

- [ ] **Step 1: 寫測試** — 建檔（鏡像 `test_project_detail_endpoints.py` 的 `_new_user` + HttpCase `_rpc` 風格；先讀該檔抄 `_rpc` helper 的實作）：

```python
import json
import uuid

from odoo.tests.common import HttpCase, tagged


def _new_user(env, login, name=None):
    return env['res.users'].sudo().create({
        'name': name or login,
        'login': login,
        'password': login,
        'email': f'{login}@test.example',
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })


@tagged('post_install', '-at_install')
class TestSecurityAccessEndpoints(HttpCase):
    """Cross-user negative：非 workspace member 打 endpoint 必須被拒。"""

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:6]
        self.alice = _new_user(self.env, f'sa_ep_alice_{suffix}')
        self.mallory = _new_user(self.env, f'sa_ep_mallory_{suffix}')
        self.workspace = self.env['woow_paas_platform.workspace'].sudo().create({
            'name': f'SA EP WS {suffix}', 'owner_id': self.alice.id,
        })
        Access = self.env['woow_paas_platform.workspace_access'].sudo()
        if not Access.search([('workspace_id', '=', self.workspace.id),
                              ('user_id', '=', self.alice.id)]):
            Access.create({'workspace_id': self.workspace.id,
                           'user_id': self.alice.id, 'role': 'owner'})
        self.sa = self.env['woow_paas_platform.security_access'].sudo().create({
            'name': 'EP Gateway', 'workspace_id': self.workspace.id,
        })

    def _rpc(self, url, params=None):
        response = self.url_open(
            url,
            data=json.dumps({'jsonrpc': '2.0', 'method': 'call',
                             'params': params or {}}),
            headers={'Content-Type': 'application/json'},
        )
        body = response.json()
        return body.get('result') or {}

    def test_member_can_list(self):
        self.authenticate(self.alice.login, self.alice.login)
        res = self._rpc(
            f'/api/workspaces/{self.workspace.id}/security-accesses',
            {'action': 'list'})
        self.assertTrue(res.get('success'))
        self.assertEqual(res.get('count'), 1)

    def test_non_member_list_denied(self):
        self.authenticate(self.mallory.login, self.mallory.login)
        res = self._rpc(
            f'/api/workspaces/{self.workspace.id}/security-accesses',
            {'action': 'list'})
        self.assertFalse(res.get('success'))

    def test_non_member_get_denied(self):
        self.authenticate(self.mallory.login, self.mallory.login)
        res = self._rpc(
            f'/api/workspaces/{self.workspace.id}'
            f'/security-accesses/{self.sa.id}',
            {'action': 'get'})
        self.assertFalse(res.get('success'))

    def test_non_member_routes_list_denied(self):
        self.authenticate(self.mallory.login, self.mallory.login)
        res = self._rpc(
            f'/api/workspaces/{self.workspace.id}'
            f'/security-accesses/{self.sa.id}/routes',
            {'action': 'list'})
        self.assertFalse(res.get('success'))

    def test_non_member_delete_denied_and_record_survives(self):
        """寫路徑 negative：刪除被拒且 record 仍在（防注入面）。"""
        self.authenticate(self.mallory.login, self.mallory.login)
        res = self._rpc(
            f'/api/workspaces/{self.workspace.id}'
            f'/security-accesses/{self.sa.id}',
            {'action': 'delete'})
        self.assertFalse(res.get('success'))
        self.assertTrue(self.sa.exists())

    def test_member_but_not_admin_cannot_mutate(self):
        """一般 member（非 owner/admin role）可讀不可變更。"""
        suffix = uuid.uuid4().hex[:6]
        carol = _new_user(self.env, f'sa_ep_carol_{suffix}')
        self.env['woow_paas_platform.workspace_access'].sudo().create({
            'workspace_id': self.workspace.id,
            'user_id': carol.id, 'role': 'member',
        })
        self.authenticate(carol.login, carol.login)
        read = self._rpc(
            f'/api/workspaces/{self.workspace.id}/security-accesses',
            {'action': 'list'})
        self.assertTrue(read.get('success'))
        mutate = self._rpc(
            f'/api/workspaces/{self.workspace.id}'
            f'/security-accesses/{self.sa.id}',
            {'action': 'delete'})
        self.assertFalse(mutate.get('success'))
```

`src/tests/__init__.py` 加 `from . import test_security_access_endpoints`

注意：`role: 'member'` 的合法值要先 grep `workspace_access.py` 的 role selection 確認（若是 `'viewer'`/`'member'` 以實際值為準）。

- [ ] **Step 2: 跑測試**

Run: `./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform:TestSecurityAccessEndpoints --stop-after-init -u woow_paas_platform`
Expected: PASS（controller 與 ir.rule 已在 Task 11/13 落地；任何 FAIL = 真 bug，修到綠）

- [ ] **Step 3: Commit**

```bash
git add src/tests/test_security_access_endpoints.py src/tests/__init__.py
git commit -m "test: add cross-user negative coverage for security access endpoints"
```

---

### Task 16: HA Component API（`/api/security-access/*`）+ OAuth scope

**Files:**
- Modify: `src/controllers/ha_api.py`
- Modify: `src/hooks.py`（post_init 確保既有 client 的新 scope — oauth_clients.xml 是 `noupdate="1"`，改 XML 不會套用到既有 DB）
- Modify: `src/data/oauth_clients.xml`（新裝環境的 seed 同步）
- Test: `src/tests/test_ha_api_security_access.py`（新檔）
- Modify: `src/tests/__init__.py`

- [ ] **Step 1: 寫失敗測試** — 建 `src/tests/test_ha_api_security_access.py`（先讀 `src/tests/test_ha_api.py` 鏡像其 token 建立 helper；以下假設可直接建 oauth_token record）：

```python
from odoo.tests.common import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestHaApiSecurityAccess(HttpCase):
    """HA component 串接端點：scope 分級 + workspace 隔離。"""

    def setUp(self):
        super().setUp()
        import uuid as _uuid
        suffix = _uuid.uuid4().hex[:6]
        User = self.env['res.users'].sudo()
        self.user = User.create({
            'name': f'HA SA User {suffix}',
            'login': f'ha_sa_user_{suffix}',
            'email': f'ha_sa_{suffix}@test.example',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.outsider = User.create({
            'name': f'HA SA Outsider {suffix}',
            'login': f'ha_sa_out_{suffix}',
            'email': f'ha_sa_out_{suffix}@test.example',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.workspace = self.env['woow_paas_platform.workspace'].sudo().create(
            {'name': f'HA SA WS {suffix}', 'owner_id': self.user.id})
        Access = self.env['woow_paas_platform.workspace_access'].sudo()
        if not Access.search([('workspace_id', '=', self.workspace.id),
                              ('user_id', '=', self.user.id)]):
            Access.create({'workspace_id': self.workspace.id,
                           'user_id': self.user.id, 'role': 'owner'})
        self.sa = self.env['woow_paas_platform.security_access'].sudo().create(
            {'name': 'HA Gateway', 'workspace_id': self.workspace.id})
        self.sa.sudo().write({
            'state': 'active', 'tunnel_id': 'tun-ha',
            'tunnel_token': 'tok-ha-secret',
        })
        self.client_rec = self.env.ref(
            'woow_paas_platform.oauth_client_ha_smart_home')

    def _make_token(self, user, scope):
        return self.env['woow_paas_platform.oauth_token'].sudo().create({
            'client_id': self.client_rec.id,
            'user_id': user.id,
            'scope': scope,
        })

    def _get(self, url, token):
        return self.url_open(
            url, headers={'Authorization': f'Bearer {token.access_token}'})

    def test_list_accesses_with_read_scope(self):
        token = self._make_token(self.user, 'security-access:read')
        resp = self._get(
            f'/api/security-access/workspaces/{self.workspace.id}/accesses',
            token)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data['accesses']), 1)
        self.assertEqual(data['accesses'][0]['name'], 'HA Gateway')
        # 列表不可外洩 token
        self.assertNotIn('tunnel_token', str(data))

    def test_tunnel_token_requires_tunnel_scope(self):
        read_only = self._make_token(self.user, 'security-access:read')
        resp = self._get(
            f'/api/security-access/accesses/{self.sa.id}/tunnel-token',
            read_only)
        self.assertEqual(resp.status_code, 403)

    def test_tunnel_token_with_correct_scope(self):
        token = self._make_token(
            self.user, 'security-access:read security-access:tunnel')
        resp = self._get(
            f'/api/security-access/accesses/{self.sa.id}/tunnel-token',
            token)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['tunnel_token'], 'tok-ha-secret')
        self.assertEqual(data['tunnel_id'], 'tun-ha')

    def test_outsider_cannot_read_foreign_sa(self):
        token = self._make_token(
            self.outsider, 'security-access:read security-access:tunnel')
        resp = self._get(
            f'/api/security-access/accesses/{self.sa.id}/tunnel-token',
            token)
        self.assertEqual(resp.status_code, 403)

    def test_ha_client_allows_sa_scopes(self):
        """G-H3 守線：public client 必須被授權新 scope，否則 refresh fail。"""
        allowed = self.client_rec.get_allowed_scopes()
        self.assertIn('security-access:read', allowed)
        self.assertIn('security-access:tunnel', allowed)
```

`src/tests/__init__.py` 加 `from . import test_ha_api_security_access`

注意：oauth_token 的必填欄位（expires 等）以 `src/tests/test_ha_api.py` 既有寫法為準，依實際 model 調整 `_make_token`。

- [ ] **Step 2: 跑測試確認失敗**（endpoint 404 / scope 測試 FAIL）

- [ ] **Step 3: 實作 endpoints** — `src/controllers/ha_api.py` 在 class 內（SmartHome endpoints 之後）加：

```python
    # ==================== Security Access (mirror of smarthome) ========

    @route(
        '/api/security-access/workspaces',
        type='http', auth='none', methods=['GET'], csrf=False,
    )
    def sa_list_workspaces(self, **kwargs: Any) -> Response:
        """Mirror of /api/smarthome/workspaces. Scope: workspace:read."""
        result = _authenticate(required_scopes=['workspace:read'])
        if isinstance(result, Response):
            return result
        user, _token = result
        workspaces = _get_user_workspaces(user)
        return _json_response({'workspaces': [
            {'id': ws.id, 'name': ws.name, 'slug': ws.slug or ''}
            for ws in workspaces
        ]})

    @route(
        '/api/security-access/workspaces/<int:workspace_id>/accesses',
        type='http', auth='none', methods=['GET'], csrf=False,
    )
    def sa_list_accesses(self, workspace_id: int, **kwargs: Any) -> Response:
        """List security accesses in a workspace.

        Scope: security-access:read. Returns plan + next payment for the
        HA sensor (FR-9). tunnel_token 絕不在列表/detail 出現 —— 只走
        tunnel-token 專用端點（更高 scope）。
        """
        result = _authenticate(required_scopes=['security-access:read'])
        if isinstance(result, Response):
            return result
        user, _token = result
        workspace = _check_workspace_access(user, workspace_id)
        if not workspace:
            return _json_error('Forbidden', 'No access to this workspace', 403)
        records = request.env['woow_paas_platform.security_access'].sudo().search([
            ('workspace_id', '=', workspace.id),
        ])
        data = []
        for sa in records:
            sub_info = sa._get_subscription_info()
            data.append({
                'id': sa.id,
                'name': sa.name,
                'canonical_name': sa.canonical_name or '',
                'state': sa.state,
                'primary_hostname': sa.primary_hostname or '',
                'tunnel_status': sa.tunnel_status or 'disconnected',
                'plan_name': sub_info['plan_name'] if sub_info else None,
                'next_payment': sub_info['next_payment'] if sub_info else None,
            })
        return _json_response({'accesses': data})

    @route(
        '/api/security-access/accesses/<int:sa_id>',
        type='http', auth='none', methods=['GET'], csrf=False,
    )
    def sa_get_access(self, sa_id: int, **kwargs: Any) -> Response:
        """Full detail. Scope: security-access:read."""
        result = _authenticate(required_scopes=['security-access:read'])
        if isinstance(result, Response):
            return result
        user, _token = result
        sa = self._get_sa_with_access(user, sa_id)
        if isinstance(sa, Response):
            return sa
        return _json_response({'access': sa.to_dict()})

    @route(
        '/api/security-access/accesses/<int:sa_id>/tunnel-token',
        type='http', auth='none', methods=['GET'], csrf=False,
    )
    def sa_get_tunnel_token(self, sa_id: int, **kwargs: Any) -> Response:
        """Tunnel credentials. Scope: security-access:tunnel (elevated)."""
        result = _authenticate(required_scopes=['security-access:tunnel'])
        if isinstance(result, Response):
            return result
        user, _token = result
        sa = self._get_sa_with_access(user, sa_id)
        if isinstance(sa, Response):
            return sa
        return _json_response({
            'tunnel_token': sa.tunnel_token or '',
            'tunnel_id': sa.tunnel_id or '',
            'primary_hostname': sa.primary_hostname or '',
        })

    @route(
        '/api/security-access/accesses/<int:sa_id>/status',
        type='http', auth='none', methods=['GET'], csrf=False,
    )
    def sa_get_status(self, sa_id: int, **kwargs: Any) -> Response:
        """Refresh + return connector status. Scope: security-access:read."""
        result = _authenticate(required_scopes=['security-access:read'])
        if isinstance(result, Response):
            return result
        user, _token = result
        sa = self._get_sa_with_access(user, sa_id)
        if isinstance(sa, Response):
            return sa
        try:
            sa.action_refresh_status()
        except Exception:
            _logger.exception(
                "Failed to refresh status for Security Access id=%s", sa_id)
        return _json_response({'access': sa.to_dict()})

    def _get_sa_with_access(self, user: Any, sa_id: int) -> Any | Response:
        """Look up a security access and verify workspace access."""
        sa = request.env['woow_paas_platform.security_access'].sudo().browse(
            sa_id).exists()
        if not sa:
            return _json_error('Not Found', 'Security access not found', 404)
        workspace = _check_workspace_access(user, sa.workspace_id.id)
        if not workspace:
            return _json_error(
                'Forbidden',
                'No access to the workspace containing this security access',
                403,
            )
        return sa
```

**sudo 說明**（兩處 `.sudo()` 的 narrow-widening 理由，與 smarthome HA API 既有寫法一致）：HA API 是 `auth='none'` + OAuth bearer，`request.env.user` 是 public user 無 ACL；`_check_workspace_access` 已先以 token user 驗 workspace membership，sudo 只在驗過之後讀該 workspace 範圍內的 record。`to_dict()` 不含 `tunnel_token`（field-mask 另擋 ORM 面）。

- [ ] **Step 4: scope 授權** — 兩處：

(a) `src/data/oauth_clients.xml`（新裝環境）scopes 改為：

```xml
            <field name="scopes">smarthome:read smarthome:tunnel workspace:read security-access:read security-access:tunnel</field>
```

(b) `src/hooks.py`（既有環境 — noupdate=1 不會套用 XML 改值）：找到 `post_init_hook`，加呼叫與函式：

```python
_HA_CLIENT_REQUIRED_SCOPES = (
    'security-access:read',
    'security-access:tunnel',
)


def _ensure_ha_client_sa_scopes(env):
    """oauth_clients.xml is noupdate=1, so adding scopes to the seed XML
    does not reach existing databases. Idempotently merge the Security
    Access scopes into the HA public client (G-H3: missing client scopes
    break token refresh for the HA component)."""
    client = env.ref(
        'woow_paas_platform.oauth_client_ha_smart_home',
        raise_if_not_found=False,
    )
    if not client:
        return
    granted = set((client.scopes or '').split())
    missing = [s for s in _HA_CLIENT_REQUIRED_SCOPES if s not in granted]
    if missing:
        client.scopes = ' '.join(sorted(granted | set(missing)))
```

並在 `post_init_hook(env)` 函式 body 末尾加一行：

```python
    _ensure_ha_client_sa_scopes(env)
```

- [ ] **Step 5: 跑測試確認通過**

Run: `./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform:TestHaApiSecurityAccess --stop-after-init -u woow_paas_platform`
Expected: PASS。另跑 `--test-tags /woow_paas_platform:TestHaApi`（既有 smarthome HA API）確認不回歸。

注意：post_init_hook 只在 install 跑；upgrade 路徑（`-u`）要驗證 scope merge 是否生效 — 若測試 `test_ha_client_allows_sa_scopes` 在 upgrade 環境 FAIL，把 `_ensure_ha_client_sa_scopes` 也掛到 manifest 的 `post_load` 或改用 migration script `migrations/<version>/post-migrate.py`（鏡像 repo 既有 migrations/ 寫法）。

- [ ] **Step 6: Commit**

```bash
git add src/controllers/ha_api.py src/data/oauth_clients.xml src/hooks.py src/tests/test_ha_api_security_access.py src/tests/__init__.py
git commit -m "feat: add security access ha api endpoints with scoped oauth"
```

---

### Task 17: 前端 — API client + 路由 + Workspace 頁整合（卡片 / 列表 / 建立 modal）

**Files:**
- Create: `src/static/src/paas/services/security_access.js`
- Modify: `src/static/src/paas/core/router.js`
- Modify: `src/static/src/paas/pages/workspace/WorkspaceDetailPage.js`
- Modify: `src/static/src/paas/pages/workspace/WorkspaceDetailPage.xml`
- Create: `src/static/src/paas/components/modal/CreateSecurityAccessModal.js`
- Create: `src/static/src/paas/components/modal/CreateSecurityAccessModal.xml`

前端無 JS 單測框架 — 驗證走 Step 6 的手動瀏覽器檢查 + merge 後 e2e（spec §7）。

- [ ] **Step 1: API client** — 建 `src/static/src/paas/services/security_access.js`（jsonRpc import 路徑鏡像 `services/cloud_service.js` 開頭）：

```javascript
/** @odoo-module **/

import { jsonRpc } from "../core/network";

export const securityAccessService = {
  async fetchList(workspaceId) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses`,
        { action: "list" },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error || "Failed to fetch" };
    } catch (err) {
      return { success: false, error: err.message || "Network error" };
    }
  },

  async create(workspaceId, name, planId, templateId) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses`,
        { action: "create", name, plan_id: planId, template_id: templateId },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  async fetchDetail(workspaceId, saId) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}`,
        { action: "get" },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  async update(workspaceId, saId, vals) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}`,
        { action: "update", ...vals },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  async provision(workspaceId, saId) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}`,
        { action: "provision" },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  async remove(workspaceId, saId) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}`,
        { action: "delete" },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  async createRoute(workspaceId, saId, vals) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}/routes`,
        { action: "create", ...vals },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  async updateRoute(workspaceId, saId, routeId, vals) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}/routes`,
        { action: "update", route_id: routeId, ...vals },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  async deleteRoute(workspaceId, saId, routeId) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}/routes`,
        { action: "delete", route_id: routeId },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  async checkRoutePrefix(prefix) {
    try {
      const result = await jsonRpc(
        "/api/security-access/routes/check", { prefix },
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  async fetchTemplates() {
    try {
      const result = await jsonRpc("/api/security-access/templates", {});
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  async fetchPlans(templateId) {
    try {
      const result = await jsonRpc(
        `/api/security-access/templates/${templateId}/plans`, {},
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  async getSkipTrialEstimate(workspaceId, saId) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}/skip-trial-estimate`,
        {},
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },

  async skipTrial(workspaceId, saId) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}/skip-trial`,
        {},
      );
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) {
      return { success: false, error: err.message };
    }
  },
};
```

注意：`jsonRpc` 的實際 import 路徑以 `services/cloud_service.js` 第一行為準（可能是 `../core/network` 或其他），照抄。

- [ ] **Step 2: 路由** — `src/static/src/paas/core/router.js` 的 routes 陣列加兩條（放在 smarthome-detail 旁）：

```javascript
{ path: "workspace/:id/security-access/:saId/:tab", name: "Security Access Detail", routeName: "security-access-detail", pattern: /^workspace\/(\d+)\/security-access\/(\d+)\/(overview|configuration|application|access|email|domains)$/ },
```

並在 `parseRoute()` 的 pattern-match 區塊加上參數對映（鏡像 smarthome-detail 的 match[2] 處理）：

```javascript
if (route.routeName === 'security-access-detail') {
    this.params.saId = match[2];
    this.params.tab = match[3];
}
```

（list 不需獨立路由 — SA 列表整合在 WorkspaceDetailPage，與 Smart Home 同模式。）

- [ ] **Step 3: 建立 modal** — 建 `src/static/src/paas/components/modal/CreateSecurityAccessModal.js`（複用 PlanSelector；結構鏡像既有 CreateSmartHomeModal — 先讀該檔對齊 WoowButton/WoowIcon import 路徑與 modal 外框 class）：

```javascript
/** @odoo-module **/

import { Component, useState, onWillStart } from "@odoo/owl";
import { WoowButton } from "../common/WoowButton";
import { WoowIcon } from "../common/WoowIcon";
import { PlanSelector } from "../subscription/PlanSelector";
import { securityAccessService } from "../../services/security_access";

export class CreateSecurityAccessModal extends Component {
  static template = "woow_paas_platform.CreateSecurityAccessModal";
  static components = { WoowButton, WoowIcon, PlanSelector };
  static props = {
    workspaceId: { type: Number },
    onClose: { type: Function },
    onCreated: { type: Function },
  };

  setup() {
    this.state = useState({
      name: "",
      templates: [],
      templateId: null,
      plans: [],
      selectedPlanId: null,
      loading: false,
      error: null,
    });
    onWillStart(async () => {
      const res = await securityAccessService.fetchTemplates();
      if (res.success && res.data.length) {
        this.state.templates = res.data;
        this.state.templateId = res.data[0].id;
        await this.loadPlans();
      } else {
        this.state.error = res.error || "No Security Access template available";
      }
    });
  }

  async loadPlans() {
    const res = await securityAccessService.fetchPlans(this.state.templateId);
    if (res.success) {
      this.state.plans = res.data;
      this.state.selectedPlanId = res.data.length ? res.data[0].id : null;
    } else {
      this.state.error = res.error;
    }
  }

  onSelectPlan(planId) {
    this.state.selectedPlanId = planId;
  }

  async confirm() {
    if (!this.state.name.trim()) {
      this.state.error = "Please enter a name";
      return;
    }
    if (!this.state.selectedPlanId) {
      this.state.error = "Please select a plan";
      return;
    }
    this.state.loading = true;
    const res = await securityAccessService.create(
      this.props.workspaceId,
      this.state.name.trim(),
      this.state.selectedPlanId,
      this.state.templateId,
    );
    this.state.loading = false;
    if (res.success) {
      this.props.onCreated(res.data);
      this.props.onClose();
    } else {
      this.state.error = res.error;
    }
  }
}
```

建 `CreateSecurityAccessModal.xml`（外框 class 鏡像既有 create modal 的 template — 讀 CreateSmartHomeModal.xml 對齊 `o_woow_modal*` class 命名）：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<templates xml:space="preserve">
  <t t-name="woow_paas_platform.CreateSecurityAccessModal">
    <div class="o_woow_modal_backdrop" t-on-click="props.onClose">
      <div class="o_woow_modal" t-on-click.stop="">
        <div class="o_woow_modal_header">
          <h2>Create Security Access</h2>
          <button class="o_woow_modal_close" t-on-click="props.onClose">
            <WoowIcon name="'close'" size="20"/>
          </button>
        </div>
        <div class="o_woow_modal_body">
          <div t-if="state.error" class="o_woow_alert o_woow_alert_error">
            <t t-esc="state.error"/>
          </div>
          <label class="o_woow_field_label">Name</label>
          <input type="text" class="o_woow_input" t-model="state.name"
                 placeholder="e.g. secure-gateway"/>
          <label class="o_woow_field_label">Plan</label>
          <PlanSelector plans="state.plans"
                        selectedPlanId="state.selectedPlanId"
                        onSelect.bind="onSelectPlan"/>
        </div>
        <div class="o_woow_modal_footer">
          <WoowButton variant="'secondary'" onClick="props.onClose">Cancel</WoowButton>
          <WoowButton variant="'primary'" onClick.bind="confirm"
                      disabled="state.loading">
            <t t-if="state.loading">Creating...</t>
            <t t-else="">Create</t>
          </WoowButton>
        </div>
      </div>
    </div>
  </t>
</templates>
```

- [ ] **Step 4: WorkspaceDetailPage 整合** — 三處修改（讀檔對齊 Smart Home 的同位置寫法）：

(a) `WorkspaceDetailPage.js`：
- import `securityAccessService` 與 `CreateSecurityAccessModal`；components 註冊 `CreateSecurityAccessModal`
- state 加 `securityAccesses: []`、`showCreateSecurityAccessModal: false`
- `serviceTypes` 陣列（Explore More Services 資料源）加一個元素（放在 Cloud Services 與 Smart Home 之間，對齊 dashboard mockup 順序）：

```javascript
{
  name: "Security Access",
  description: "Setup Zero Trust Tunnels.",
  icon: "shield",
  color: "green",
  action: "security-access",
},
```

- XML 的 Get Started 分支需要 action handler：加 method

```javascript
openCreateSecurityAccessModal() {
  this.state.showCreateSecurityAccessModal = true;
}

onSecurityAccessCreated(sa) {
  this.loadSecurityAccesses();
  // 建立後直接進 detail 看 provisioning 進度
  this.env.router.navigate(
    `workspace/${this.workspaceId}/security-access/${sa.id}/overview`);
}

async loadSecurityAccesses() {
  const res = await securityAccessService.fetchList(this.workspaceId);
  if (res.success) {
    this.state.securityAccesses = res.data;
  }
}

openSecurityAccess(sa) {
  this.env.router.navigate(
    `workspace/${this.workspaceId}/security-access/${sa.id}/overview`);
}
```

- 在頁面載入流程（`onWillStart` 或既有 `loadSmartHomes` 同處）呼叫 `this.loadSecurityAccesses()`
- `this.workspaceId` 與 `this.env.router.navigate` 的實際取得方式照檔內 Smart Home 既有寫法對齊（可能是 `this.props.workspaceId` / `router.navigate` import）。

(b) `WorkspaceDetailPage.xml`：
- Explore More Services 的 `t-foreach` 卡片內，Get Started 按鈕分支加：

```xml
<t t-elif="service.action === 'security-access'">
    <WoowButton variant="'primary'" size="'sm'" onClick.bind="openCreateSecurityAccessModal">
        Get Started
        <WoowIcon name="'arrow_forward'" size="16"/>
    </WoowButton>
</t>
```

- Smart Home 列表區塊之後，加 SA 列表區塊（卡片佈局鏡像 Smart Home 列表區塊的結構與 class；逐欄顯示 name、primary_hostname、StatusBadge state、routes 數）：

```xml
<!-- Security Access Section -->
<WoowCard shadow="true" t-if="state.securityAccesses.length">
    <div class="o_woow_section_header">
        <WoowIcon name="'shield'" size="24"/>
        <h2>Security Access</h2>
    </div>
    <div class="o_woow_service_list">
        <t t-foreach="state.securityAccesses" t-as="sa" t-key="sa.id">
            <div class="o_woow_service_row" t-on-click="() => this.openSecurityAccess(sa)">
                <div class="o_woow_service_row_main">
                    <span class="o_woow_service_row_name"><t t-esc="sa.name"/></span>
                    <span class="o_woow_service_row_sub"><t t-esc="sa.primary_hostname"/></span>
                </div>
                <StatusBadge status="sa.state"/>
            </div>
        </t>
    </div>
</WoowCard>

<!-- Create Security Access Modal -->
<CreateSecurityAccessModal t-if="state.showCreateSecurityAccessModal"
    workspaceId="workspaceId"
    onClose="() => state.showCreateSecurityAccessModal = false"
    onCreated.bind="onSecurityAccessCreated"/>
```

（`o_woow_section_header` / `o_woow_service_list` / `o_woow_service_row` 等 class 名以該檔 Smart Home 區塊實際 class 為準照抄，不發明新 class。）

- [ ] **Step 5: Commit**

```bash
git add src/static/src/paas/services/security_access.js src/static/src/paas/core/router.js src/static/src/paas/components/modal/CreateSecurityAccessModal.* src/static/src/paas/pages/workspace/WorkspaceDetailPage.*
git commit -m "feat: add security access frontend service router and workspace integration"
```

- [ ] **Step 6: 手動驗證（k8s sandbox）**

```bash
./.claude/skills/k8s-dev-sandbox/scripts/create.sh --context docker-desktop
# 開瀏覽器到 sandbox /woow，確認：
# 1. Workspace 頁 Explore More Services 出現 Security Access 卡（綠盾）
# 2. Get Started → modal 開啟、方案列表載入（Entry/Pro × 月/年）
# 3. 建立後導向 detail 頁（Task 18 完成前 404 屬預期）
```

---

### Task 18: 前端 — SecurityAccessDetailPage（6 tabs，3 個啟用）

**Files:**
- Create: `src/static/src/paas/pages/security-access/SecurityAccessDetailPage.js`
- Create: `src/static/src/paas/pages/security-access/SecurityAccessDetailPage.xml`
- Create: `src/static/src/paas/pages/security-access/SecurityAccessDetailPage.scss`
- Modify: 頁面註冊處（讀 `router.js` 或 App root，看 `smarthome-detail` routeName 怎麼映射到 SmartHomeDetailPage component，照同一機制註冊 `security-access-detail` → SecurityAccessDetailPage）

- [ ] **Step 1: Page JS** — 建 `SecurityAccessDetailPage.js`：

```javascript
/** @odoo-module **/

import { Component, useState, onWillStart, onWillUnmount } from "@odoo/owl";
import { WoowCard } from "../../components/common/WoowCard";
import { WoowButton } from "../../components/common/WoowButton";
import { WoowIcon } from "../../components/common/WoowIcon";
import { StatusBadge } from "../../components/common/StatusBadge";
import { SkipTrialModal } from "../../components/modal/SkipTrialModal";
import { securityAccessService } from "../../services/security_access";

const TABS = [
  { key: "overview", label: "Overview", icon: "info" },
  { key: "configuration", label: "Configuration", icon: "settings" },
  { key: "application", label: "Application", icon: "route" },
  { key: "access", label: "Access", icon: "shield", comingSoon: true },
  { key: "email", label: "Email", icon: "mail", comingSoon: true },
  { key: "domains", label: "Domains", icon: "language", comingSoon: true },
];

export class SecurityAccessDetailPage extends Component {
  static template = "woow_paas_platform.SecurityAccessDetailPage";
  static components = {
    WoowCard, WoowButton, WoowIcon, StatusBadge, SkipTrialModal,
  };
  static props = {
    workspaceId: { type: Number },
    saId: { type: Number },
    tab: { type: String, optional: true },
  };

  setup() {
    this.tabs = TABS;
    this.state = useState({
      sa: null,
      loading: true,
      error: null,
      activeTab: this.props.tab || "overview",
      editingName: false,
      nameDraft: "",
      // Application tab — add route form
      newRoutePrefix: "",
      newRouteServiceUrl: "",
      prefixCheck: null, // null | {available, reason}
      prefixChecking: false,
      showSkipTrialModal: false,
      showDeleteConfirm: false,
    });
    this.pollingInterval = null;
    this.prefixCheckTimer = null;
    onWillStart(() => this.loadDetail());
    onWillUnmount(() => {
      this.stopPolling();
      clearTimeout(this.prefixCheckTimer);
    });
  }

  async loadDetail() {
    const res = await securityAccessService.fetchDetail(
      this.props.workspaceId, this.props.saId);
    this.state.loading = false;
    if (res.success) {
      this.state.sa = res.data;
      this.startPollingIfNeeded();
    } else {
      this.state.error = res.error;
    }
  }

  // ---- polling（鏡像 SmartHomeDetailPage） ----
  startPollingIfNeeded() {
    const pendingStates = ["pending", "provisioning", "deleting"];
    if (this.state.sa && pendingStates.includes(this.state.sa.state)) {
      this.stopPolling();
      this.pollingInterval = setInterval(() => this.loadDetail(), 5000);
    } else {
      this.stopPolling();
    }
  }

  stopPolling() {
    if (this.pollingInterval) {
      clearInterval(this.pollingInterval);
      this.pollingInterval = null;
    }
  }

  // ---- tabs（tab 為路徑段，對齊 cloud service 規則） ----
  switchTab(tabKey) {
    this.state.activeTab = tabKey;
    window.location.hash = `#/workspace/${this.props.workspaceId}` +
      `/security-access/${this.props.saId}/${tabKey}`;
  }

  // ---- header actions ----
  startEditName() {
    this.state.nameDraft = this.state.sa.name;
    this.state.editingName = true;
  }

  async saveName() {
    const res = await securityAccessService.update(
      this.props.workspaceId, this.props.saId,
      { name: this.state.nameDraft.trim() });
    if (res.success) {
      this.state.sa = res.data;
      this.state.editingName = false;
    } else {
      this.state.error = res.error;
    }
  }

  async deleteSa() {
    const res = await securityAccessService.remove(
      this.props.workspaceId, this.props.saId);
    if (res.success) {
      window.location.hash = `#/workspace/${this.props.workspaceId}`;
    } else {
      this.state.error = res.error;
      this.state.showDeleteConfirm = false;
    }
  }

  async provision() {
    const res = await securityAccessService.provision(
      this.props.workspaceId, this.props.saId);
    if (res.success) {
      this.state.sa = res.data;
      this.startPollingIfNeeded();
    } else {
      this.state.error = res.error;
    }
  }

  copyToken() {
    // tunnel token 只給 system group 看（field-mask），前端顯示
    // Configuration tab 的接入教學以 primary_hostname 與 docker 指令為主；
    // token 由 HA component / 後台管道取得。此按鈕複製 hostname。
    navigator.clipboard.writeText(this.state.sa.primary_hostname);
  }

  // ---- Application tab：routes ----
  onPrefixInput(ev) {
    this.state.newRoutePrefix = ev.target.value.trim().toLowerCase();
    this.state.prefixCheck = null;
    clearTimeout(this.prefixCheckTimer);
    if (this.state.newRoutePrefix.length >= 3) {
      // debounce 500ms — 即時 availability 反饋（spec：設定前可反饋）
      this.prefixCheckTimer = setTimeout(() => this.checkPrefix(), 500);
    }
  }

  async checkPrefix() {
    this.state.prefixChecking = true;
    const res = await securityAccessService.checkRoutePrefix(
      this.state.newRoutePrefix);
    this.state.prefixChecking = false;
    if (res.success) {
      this.state.prefixCheck = res.data;
    }
  }

  async addRoute() {
    if (!this.state.newRoutePrefix) {
      return;
    }
    const vals = { subdomain_prefix: this.state.newRoutePrefix };
    if (this.state.newRouteServiceUrl) {
      vals.service_url = this.state.newRouteServiceUrl;
    }
    const res = await securityAccessService.createRoute(
      this.props.workspaceId, this.props.saId, vals);
    if (res.success) {
      this.state.newRoutePrefix = "";
      this.state.newRouteServiceUrl = "";
      this.state.prefixCheck = null;
      await this.loadDetail();
    } else {
      this.state.error = res.error;
    }
  }

  async removeRoute(route) {
    const res = await securityAccessService.deleteRoute(
      this.props.workspaceId, this.props.saId, route.id);
    if (res.success) {
      await this.loadDetail();
    } else {
      this.state.error = res.error;
    }
  }

  // ---- SkipTrialModal 接線（鏡像 SmartHomeDetailPage） ----
  loadSkipTrialEstimate = () => {
    return securityAccessService.getSkipTrialEstimate(
      this.props.workspaceId, this.props.saId);
  };

  confirmSkipTrial = () => {
    return securityAccessService.skipTrial(
      this.props.workspaceId, this.props.saId);
  };

  onTrialSkipped = () => {
    this.state.showSkipTrialModal = false;
    this.loadDetail();
  };
}
```

- [ ] **Step 2: Page XML** — 建 `SecurityAccessDetailPage.xml`（依 mockup：header 區 + scrollable tab bar + tab 內容；Coming Soon tab 顯示佔位）：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<templates xml:space="preserve">
  <t t-name="woow_paas_platform.SecurityAccessDetailPage">
    <div class="o_woow_sa_detail">
      <div t-if="state.loading" class="o_woow_sa_detail__loading">Loading...</div>
      <div t-elif="state.error" class="o_woow_alert o_woow_alert_error">
        <t t-esc="state.error"/>
      </div>
      <t t-elif="state.sa">
        <!-- Header（mockup：icon + nickname + badge + hostname + delete） -->
        <div class="o_woow_sa_detail__header">
          <div class="o_woow_sa_detail__icon">
            <WoowIcon name="'verified_user'" size="32"/>
          </div>
          <div class="o_woow_sa_detail__title">
            <t t-if="!state.editingName">
              <h1 t-on-click="startEditName"><t t-esc="state.sa.name"/></h1>
            </t>
            <t t-else="">
              <!-- OWL 不支援 .enter key modifier（Vue 語法會 OwlError 整頁
                   crash）— 用 t-on-keydown handler 內讀 ev.key -->
              <input type="text" class="o_woow_input" t-model="state.nameDraft"
                     t-on-keydown="onNameKeydown" t-on-blur="saveName"/>
            </t>
            <StatusBadge status="state.sa.state"/>
            <div class="o_woow_sa_detail__hostname">
              <WoowIcon name="'language'" size="14"/>
              <a t-att-href="'https://' + state.sa.primary_hostname"
                 target="_blank"><t t-esc="state.sa.primary_hostname"/></a>
            </div>
          </div>
          <div class="o_woow_sa_detail__actions">
            <WoowButton t-if="state.sa.state === 'pending' or state.sa.state === 'error'"
                        variant="'primary'" onClick.bind="provision">Provision</WoowButton>
            <WoowButton variant="'danger-outline'"
                        onClick="() => state.showDeleteConfirm = true">
              <WoowIcon name="'delete'" size="16"/>
            </WoowButton>
          </div>
        </div>

        <!-- Tab bar（橫向可捲動，對齊 mockup） -->
        <div class="o_woow_sa_detail__tabs">
          <t t-foreach="tabs" t-as="tab" t-key="tab.key">
            <button t-att-class="'o_woow_sa_detail__tab' +
                      (state.activeTab === tab.key ? ' o_woow_sa_detail__tab--active' : '')"
                    t-on-click="() => this.switchTab(tab.key)">
              <WoowIcon name="tab.icon" size="16"/>
              <!-- 注意：OWL component 的 props 本身就是 expression，
                   不可用 t-att- 前綴（會 OwlError 整頁 crash） -->
              <t t-esc="tab.label"/>
              <span t-if="tab.comingSoon" class="o_woow_sa_detail__tab_dot"/>
            </button>
          </t>
        </div>

        <!-- Overview -->
        <div t-if="state.activeTab === 'overview'" class="o_woow_sa_detail__body">
          <WoowCard shadow="true">
            <h3>Status</h3>
            <div class="o_woow_sa_detail__grid">
              <div><label>State</label><StatusBadge status="state.sa.state"/></div>
              <div><label>Connector</label><StatusBadge status="state.sa.tunnel_status"/></div>
              <div><label>Hostname</label><t t-esc="state.sa.primary_hostname"/></div>
            </div>
          </WoowCard>
          <WoowCard shadow="true" t-if="state.sa.subscription">
            <h3>Subscription</h3>
            <div class="o_woow_sa_detail__grid">
              <div><label>Plan</label><t t-esc="state.sa.subscription.plan_name"/></div>
              <div><label>Next payment</label><t t-esc="state.sa.subscription.next_payment"/></div>
              <div t-if="state.sa.subscription.is_trial">
                <label>Trial ends</label>
                <t t-esc="state.sa.subscription.trial_end_date"/>
                <WoowButton variant="'primary'" size="'sm'"
                            onClick="() => state.showSkipTrialModal = true">
                  Skip Trial
                </WoowButton>
              </div>
            </div>
          </WoowCard>
        </div>

        <!-- Configuration（record 自身設定 + 接入教學） -->
        <div t-if="state.activeTab === 'configuration'" class="o_woow_sa_detail__body">
          <WoowCard shadow="true">
            <h3>Connector Setup</h3>
            <p>Run the Cloudflare Tunnel connector on your host, then point
               your reverse proxy (e.g. Nginx Proxy Manager) at port 80.
               All application routes forward to it with the Host header
               preserved.</p>
            <div class="o_woow_sa_detail__code">
              <code>docker run cloudflare/cloudflared:latest tunnel run --token &lt;TOKEN&gt;</code>
            </div>
            <p class="o_woow_sa_detail__hint">
              Get the tunnel token from your Home Assistant integration or
              ask your workspace owner. See the NPM setup guide for the
              reverse-proxy walkthrough.
            </p>
          </WoowCard>
          <WoowCard shadow="true">
            <h3>Identity</h3>
            <div class="o_woow_sa_detail__grid">
              <div><label>Canonical name</label><t t-esc="state.sa.canonical_name"/></div>
              <div><label>Created</label><t t-esc="state.sa.created_at"/></div>
            </div>
          </WoowCard>
        </div>

        <!-- Application（routes 管理） -->
        <div t-if="state.activeTab === 'application'" class="o_woow_sa_detail__body">
          <WoowCard shadow="true">
            <h3>Application Routes</h3>
            <p>Each route maps a public subdomain to your host. Traffic is
               forwarded to the service URL with the Host header set.</p>
            <table class="o_woow_sa_detail__routes">
              <thead>
                <tr><th>Hostname</th><th>Service</th><th>Host header</th><th></th></tr>
              </thead>
              <tbody>
                <t t-foreach="state.sa.routes" t-as="route" t-key="route.id">
                  <tr>
                    <td>
                      <t t-esc="route.hostname"/>
                      <span t-if="route.is_primary" class="o_woow_badge">Primary</span>
                    </td>
                    <td><t t-esc="route.service_url"/></td>
                    <td><t t-esc="route.http_host_header ? 'Yes' : 'No'"/></td>
                    <td>
                      <WoowButton t-if="!route.is_primary" variant="'danger-outline'"
                                  size="'sm'" onClick="() => this.removeRoute(route)">
                        <WoowIcon name="'delete'" size="14"/>
                      </WoowButton>
                    </td>
                  </tr>
                </t>
              </tbody>
            </table>
            <!-- Add route：prefix 即時 availability 反饋 -->
            <div class="o_woow_sa_detail__add_route">
              <input type="text" class="o_woow_input"
                     placeholder="subdomain prefix (e.g. waltek-ha)"
                     t-att-value="state.newRoutePrefix"
                     t-on-input="onPrefixInput"/>
              <input type="text" class="o_woow_input"
                     placeholder="service URL (default from platform)"
                     t-model="state.newRouteServiceUrl"/>
              <WoowButton variant="'primary'" onClick.bind="addRoute"
                          disabled="state.prefixCheck and !state.prefixCheck.available">
                Add Route
              </WoowButton>
            </div>
            <div t-if="state.prefixChecking" class="o_woow_sa_detail__hint">Checking availability...</div>
            <div t-elif="state.prefixCheck and state.prefixCheck.available"
                 class="o_woow_sa_detail__hint o_woow_sa_detail__hint--ok">
              ✓ Available
            </div>
            <div t-elif="state.prefixCheck and !state.prefixCheck.available"
                 class="o_woow_sa_detail__hint o_woow_sa_detail__hint--error">
              ✗ Not available: <t t-esc="state.prefixCheck.reason"/>
            </div>
          </WoowCard>
        </div>

        <!-- Coming Soon tabs (P2-P4) -->
        <div t-if="state.activeTab === 'access' or state.activeTab === 'email' or state.activeTab === 'domains'"
             class="o_woow_sa_detail__body">
          <WoowCard shadow="true">
            <div class="o_woow_sa_detail__coming_soon">
              <WoowIcon name="'construction'" size="48"/>
              <h3>Coming Soon</h3>
              <p>This feature is on the roadmap and will be enabled in an
                 upcoming release.</p>
            </div>
          </WoowCard>
        </div>

        <!-- Modals -->
        <SkipTrialModal t-if="state.showSkipTrialModal"
            service="state.sa"
            loadEstimate="loadSkipTrialEstimate"
            confirmSkip="confirmSkipTrial"
            onClose="() => state.showSkipTrialModal = false"
            onSkipped="onTrialSkipped"/>

        <div t-if="state.showDeleteConfirm" class="o_woow_modal_backdrop">
          <div class="o_woow_modal">
            <div class="o_woow_modal_header"><h2>Delete Security Access?</h2></div>
            <div class="o_woow_modal_body">
              <p>This permanently deletes the tunnel, all application routes
                 and DNS records on Cloudflare. Your connector will be
                 disconnected immediately. This cannot be undone.</p>
            </div>
            <div class="o_woow_modal_footer">
              <WoowButton variant="'secondary'"
                          onClick="() => state.showDeleteConfirm = false">Cancel</WoowButton>
              <WoowButton variant="'danger'" onClick.bind="deleteSa">Delete</WoowButton>
            </div>
          </div>
        </div>
      </t>
    </div>
  </t>
</templates>
```

- [ ] **Step 3: SCSS** — 建 `SecurityAccessDetailPage.scss`（`.o_woow_sa_` 前綴 + 既有變數；assets bundle 的 `pages/**/*.scss` glob 自動收錄）：

```scss
// Security Access Detail Page — mirrors service detail layout
.o_woow_sa_detail {
  &__header {
    display: flex;
    align-items: flex-start;
    gap: 1rem;
    margin-bottom: 1.5rem;
  }

  &__icon {
    width: 56px;
    height: 56px;
    border-radius: $woow-radius-default;
    background: rgba(34, 197, 94, 0.12); // 綠盾（dashboard 卡同色系）
    color: #16a34a;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  &__title {
    flex: 1;
    h1 {
      font-family: $woow-font-display;
      margin: 0 0 0.25rem;
      cursor: pointer;
    }
  }

  &__hostname {
    display: flex;
    align-items: center;
    gap: 0.375rem;
    color: $woow-primary;
    font-size: 0.875rem;
  }

  &__actions {
    display: flex;
    gap: 0.5rem;
  }

  &__tabs {
    display: flex;
    gap: 0.25rem;
    overflow-x: auto;
    border-bottom: 1px solid $woow-border-light;
    margin-bottom: 1.5rem;
  }

  &__tab {
    display: flex;
    align-items: center;
    gap: 0.375rem;
    padding: 0.625rem 1rem;
    border: none;
    background: none;
    color: $woow-text-secondary;
    white-space: nowrap;
    cursor: pointer;

    &--active {
      color: $woow-primary;
      border-bottom: 2px solid $woow-primary;
    }
  }

  &__tab_dot {
    width: 6px;
    height: 6px;
    border-radius: $woow-radius-full;
    background: $woow-primary;
  }

  &__body {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  &__grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 1rem;

    label {
      display: block;
      color: $woow-text-secondary;
      font-size: 0.75rem;
      margin-bottom: 0.25rem;
    }
  }

  &__routes {
    width: 100%;
    border-collapse: collapse;

    th, td {
      text-align: left;
      padding: 0.625rem 0.5rem;
      border-bottom: 1px solid $woow-border-light;
    }
  }

  &__add_route {
    display: flex;
    gap: 0.5rem;
    margin-top: 1rem;
  }

  &__code {
    background: $woow-background-dark;
    color: #e2e8f0;
    border-radius: $woow-radius-default;
    padding: 0.75rem 1rem;
    overflow-x: auto;
    margin: 0.75rem 0;
  }

  &__hint {
    color: $woow-text-secondary;
    font-size: 0.8125rem;
    margin-top: 0.5rem;

    &--ok { color: #16a34a; }
    &--error { color: #dc2626; }
  }

  &__coming_soon {
    text-align: center;
    padding: 3rem 1rem;
    color: $woow-text-secondary;
  }
}
```

- [ ] **Step 4: 頁面註冊** — 讀 `router.js` / App root 找 `smarthome-detail` → `SmartHomeDetailPage` 的映射機制（route → component 的 switch 或 mapping 物件），照同一機制加：`security-access-detail` → `SecurityAccessDetailPage`，props 對映 `workspaceId: parseInt(params.id)`、`saId: parseInt(params.saId)`、`tab: params.tab`。

- [ ] **Step 5: 手動驗證（k8s sandbox）**

```bash
# sandbox 已在 Task 17 Step 6 建立；更新模組後瀏覽器驗證：
# 1. 建立 SA → detail 頁 Overview 顯示 provisioning → active（輪詢）
# 2. Application tab：輸入 prefix 即時顯示 ✓/✗、Add Route 成功、刪除 route
# 3. Configuration tab：教學文案與 canonical name
# 4. Access/Email/Domains tab：Coming Soon 佔位
# 5. UI 風格與 Smart Home / Cloud Service detail 一致
```

- [ ] **Step 6: Commit**

```bash
git add src/static/src/paas/pages/security-access/
git commit -m "feat: add security access detail page with six tabs"
```

---

### Task 19: HA component 串接文件

**Files:**
- Create: `docs/reference/api/security-access-ha-integration.md`

- [ ] **Step 1: 寫文件**（frontmatter 風格照 `docs/reference/api/` 既有檔案；內容大綱如下，各 endpoint 含完整 request/response 範例 — response shape 直接以 Task 16 實作為準）：

```markdown
# Security Access — HA Component 串接 API

對象：`woow_paas_smart_home` HomeAssistant custom component 的未來擴展
（component 將支援從 Smart Home 或 Security Access 選擇服務來源）。
本文件是 PaaS 端 contract；**不修改 component 原始碼**。

## 認證

OAuth 2.0 Bearer token（public client `woow-ha-smart-home`、PKCE）。
新增 scopes：
- `security-access:read` — 讀取 SA 服務清單/狀態/方案資訊
- `security-access:tunnel` — 取得 tunnel token（高權限）

## Endpoints（與 /api/smarthome/* 對照）

| Security Access | Smart Home 對應 | Scope |
|---|---|---|
| GET /api/security-access/workspaces | GET /api/smarthome/workspaces | workspace:read |
| GET /api/security-access/workspaces/{ws}/accesses | …/workspaces/{ws}/homes | security-access:read |
| GET /api/security-access/accesses/{id} | …/homes/{id} | security-access:read |
| GET /api/security-access/accesses/{id}/tunnel-token | …/homes/{id}/tunnel-token | security-access:tunnel |
| GET /api/security-access/accesses/{id}/status | …/homes/{id}/status | security-access:read |

（每個 endpoint 之後附 curl 範例 + 實際 JSON response 範例 —
照 Task 16 的實作輸出撰寫，含 FR-9 sensor 需要的 plan_name / next_payment 欄位）

## Component 擴展建議（未來實作參考）

1. config_flow 增加 service-type 選擇步驟（Smart Home / Security Access）
2. config entry 的 unique_id 須含 service type：
   `f"{workspace_id}_{service_type}_{service_id}"` —— 否則同 workspace
   同時配 SH+SA 會相撞（G-H4）
3. cloudflared_manager 無需改動：SA 回傳的同樣是 tunnel token
4. Sensor 可顯示：state、tunnel_status、plan_name、next_payment（FR-9）
```

- [ ] **Step 2: lychee link check**（docs CI 是 broken-link warning；本檔連結需有效）

Run: `grep -o '\[.*\](.*\.md)' docs/reference/api/security-access-ha-integration.md`（手動確認相對路徑存在）

- [ ] **Step 3: Commit**

```bash
git add docs/reference/api/security-access-ha-integration.md
git commit -m "docs: add security access ha component integration api reference"
```

---

### Task 20: 收尾 — 全套件回歸 + spec 狀態更新

- [ ] **Step 1: Odoo 全模組測試**

Run: `./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform --stop-after-init -u woow_paas_platform`
Expected: 全 PASS（特別注意 smart home / cloud service / subscription 既有測試不回歸）

- [ ] **Step 2: Operator 全 unit suite**

Run: `cd extra/paas-operator && pytest tests/ -v -m "not integration"`
Expected: 全 PASS

- [ ] **Step 3: Operator integration tests（真 K8s，CLAUDE.md 規範：動了 extra/paas-operator/ 必跑）**

```bash
cd extra/paas-operator
kubectl config use-context docker-desktop
HELM_BINARY=$(which helm) pytest -m integration tests/integration/ -v
```
Expected: 全 PASS（~10s；CF 相關新 endpoint 不在 integration 範圍 — 它們是 mock-tested + stg smoke）

- [ ] **Step 4: 更新 spec 狀態** — `docs/superpowers/specs/2026-06-06-security-access-subscription-design.md` 頂部狀態行改為：

```
> 狀態：P1 implemented（plan: docs/superpowers/plans/2026-06-06-security-access-p1-mainline.md）；P2-P4 待開。
```

- [ ] **Step 5: Commit + 對抗式審查入口**

```bash
git add docs/superpowers/specs/2026-06-06-security-access-subscription-design.md
git commit -m "docs: mark security access spec p1 as implemented"
```

P1 全部完成後（merge 前）：
1. 跑 `superpowers:requesting-code-review` 或 `/code-review`（對抗式審查：ACL sudo grep audit — 對照 `orm-vs-endpoint-acl-test-coverage` 的 34 處 sudo checklist 模式，逐一檢查本 PR 新增的每個 `.sudo()` 有 narrow-widening 註解與理由）
2. silent-failure hunter 掃 operator 錯誤路徑（CF API 失敗是否都 fail loud）
3. PR → develop（PR 描述 zh-tw）

---

## Self-Review 紀錄（plan 完成時逐項檢查）

1. **Spec coverage**：
   - 訂閱/方案（spec §3 + quota）→ Task 6, 12, 13, 14
   - tunnel 同名 canonical（§1 命名）→ Task 1, 8
   - Application Route + 預設 service_url + Host header + 設定前反饋（§1, §2, §6）→ Task 2-5, 8, 13, 17, 18
   - 503 凍結/恢復/cron（§3）→ Task 8, 9, 10
   - 刪除清理鏈（§3）→ Task 8, 9
   - 對稱 ir.rule + 不 sudo list + HttpCase negative（§1 ACL）→ Task 11, 13, 15
   - 後台 menu/list/form + Settings 預設反向代理（§6）→ Task 12
   - HA API + scope + 文件（§5）→ Task 16, 19
   - 前端 workspace-scoped 路由 + 6 tab（3 啟用）+ dashboard 卡（§4）→ Task 17, 18
   - P1 不含：Access 實際接線（is_protected 欄位先存）、Domains、Email — 留 P2-P4 ✓
2. **Placeholder scan**：無 TBD/TODO；「鏡像 X 檔」皆附帶具體程式碼或明確讀檔對齊指示（import 路徑/class 名等環境相依細節）。
3. **型別/命名一致性**：`make_security_access_subdomain` / `sync_tunnel_routes` / `check_route_availability` / `configure_tunnel_ingress` / `reconcile_tunnel_dns` / model 名 `woow_paas_platform.security_access(_route|_template)` / scope `security-access:read|tunnel` — 各 task 間一致。

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-06-security-access-p1-mainline.md`. Two execution options:

**1. Subagent-Driven (recommended)** — superpowers:subagent-driven-development：每 task 派 fresh subagent + 兩段式審查，本 session 控管進度

**2. Inline Execution** — superpowers:executing-plans：本 session 批次執行，checkpoint 審查

Which approach?

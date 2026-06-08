# Security Access P2 — Access Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 為 Security Access 加上 Cloudflare Access（Zero Trust）存取控制：每個 SA 一個 Access Application（涵蓋其受保護 hostname）、可選 Login Methods（Google + Email OTP）、Access Policies CRUD（allow/deny × email_domain / email_list / ip_range）、逐 route 受保護開關、session duration。

**Architecture:** 沿用 P1 的**宣告式 full-state sync** pattern——SA model 蒐集 desired Access state（enabled / login methods / session_duration / 受保護 hostname destinations / policies）→ 推到 operator 新增的單一宣告式 endpoint，operator reconcile 成一個 per-SA Access Application + 一組 reusable policies。operator 動態列 account IdP 把 login method → idp_id。所有 Cloudflare 操作經 operator（與 P1 一致），無新 operator config knob（account_id 已有）→ image-only 部署。

**Tech Stack:** Odoo 18（Python/OWL/QWeb/SCSS）、FastAPI（paas-operator）、Cloudflare Access API（apps + reusable policies + identity_providers）。

**Spec:** `docs/superpowers/specs/2026-06-06-security-access-subscription-design.md` §1（access_policy 子模型、主模型 access 欄位）/ §2（operator Access endpoints）/ §4（Access tab UI）。

**P1 已就緒可複用：** `route.is_protected` 欄位（P1 已建、inert）、`sa_max_access_policies` quota 欄位（P1 已建、未 enforce）、SA 宣告式 sync pattern（`_build_route_payload` / `_sync_routes_to_operator`）、BFF 三層 gate（workspace → role → `_check_operation_allowed`）、前端 Access tab Coming Soon 佔位、`o_woow_checkbox` / `o_woow_modal_*` 既有元件。

---

## ⚠️ 前置條件（開工前確認）

| 項目 | 狀態 | 說明 |
|---|---|---|
| token `Access: Apps and Policies (Edit)` | ✅ 已有 | 管 Access App + Policy |
| token `Access: Organizations, Identity Providers and Groups (Read)` | **⏳ 待加（使用者）** | operator `GET /access/identity_providers` 列 IdP 取 id；**沒加會在 login method 同步時 403**。stg E2E 前必須加。**不擋 plan + unit/mock build**（mock 不打真 API） |
| 帳號 IdP：Google + onetimepin | ✅ 已設 | login_google → type=`google` 的 idp id；login_email_otp → type=`onetimepin` 的 idp id |
| operator 新 config knob | ❌ 不需要 | Access 用既有 `account_id` + `api_token`（account scope）→ **image-only 部署**（chart 不動，同 P1 主線） |

**設計決策（已與使用者拍板）：**
- **Login Methods = 功能性 toggle**（可獨立開關 Google / Email OTP）；operator 動態列 IdP 取 id（option C）。
- **Cloudflare 物件模型**：每個 SA **一個** Access Application（`domain` = primary hostname、`destinations[]` = 所有 `is_protected=True` 的 route hostname）+ 一組 **reusable** policies（`POST /accounts/{id}/access/policies`）在 app 的 `policies[]` 以 `{id, precedence}` 引用。刪 SA → 刪 app（app-scoped 不用）+ 顯式刪該 SA 的 reusable policies。
- **session_duration 設在 App 層**（Go duration 字串如 `"8h"`），滿足「每 hostname 預設有效期」。
- **decision 值**：`allow` / `deny`（`deny`=封鎖；**不是 `block`**，CF 規範值）。
- **本次只做 Login Methods = Google + Email OTP**；GitHub / IP allow-list-as-login-method 不做（IP 以 policy 的 `ip_range` rule 表達，屬 Policies CRUD）。

---

## File Structure

```
新增：
  extra/paas-operator/src/api/access.py                    # Access router（IdP list + 宣告式 app sync）
  extra/paas-operator/tests/test_access_sync.py            # operator Access service + endpoint 測試
  src/models/security_access_access_policy.py              # access_policy 子模型
  src/static/src/paas/components/modal/AddPolicyModal.js   # Add/Edit Policy modal
  src/static/src/paas/components/modal/AddPolicyModal.xml

修改：
  extra/paas-operator/src/services/cloudflare.py           # + Access 區段（list_identity_providers / sync_access_app / _delete_access_app / reusable policy CRUD）
  extra/paas-operator/src/models/schemas.py                # + Access schemas
  extra/paas-operator/src/main.py                          # + include_router(access.router)
  src/services/paas_operator.py                            # + sync_access_app() / delete_access_app() / list_identity_providers()
  src/models/security_access.py                            # 主模型 + access 欄位、_build_access_payload / _sync_access_to_operator、lifecycle cleanup 接 Access、to_dict
  src/models/__init__.py                                   # + security_access_access_policy import
  src/controllers/security_access.py                       # + policy CRUD / access-config / route is_protected update endpoints
  src/security/ir.model.access.csv                         # + access_policy 2 行
  src/security/ir_rules.xml                                # + access_policy 對稱 ir.rule
  src/static/src/paas/services/security_access.js          # + policy CRUD / updateAccessConfig 方法
  src/static/src/paas/pages/security-access/SecurityAccessDetailPage.js   # Access tab state + handlers、Application tab is_protected
  src/static/src/paas/pages/security-access/SecurityAccessDetailPage.xml  # Access tab 實作（取代 Coming Soon）+ Application is_protected 列
  src/static/src/paas/pages/security-access/SecurityAccessDetailPage.scss # Access tab 樣式
  src/tests/test_security_access_access_policy.py          # 新檔：model + 宣告式 sync 測試
  src/tests/test_security_access_endpoints.py              # 追加 policy CRUD HttpCase cross-user negative
  src/tests/__init__.py                                    # + import
  docs/superpowers/specs/2026-06-06-security-access-subscription-design.md  # 收尾標 P2 implemented
```

**依賴順序**：operator（Task 1-4）→ Odoo client（Task 5）→ model（Task 6-9）→ ACL/quota（Task 10）→ BFF（Task 11-12）→ 前端（Task 13-14）→ HttpCase（Task 15）→ 收尾（Task 16）。

**測試指令**：
- operator：`cd extra/paas-operator && /tmp/sa-op-venv/bin/python -m pytest tests/<file> -v`（venv 沿用 P1；不在就 `python3.11 -m venv /tmp/sa-op-venv && pip install -r requirements.txt`）
- Odoo：k8s sandbox `dev-sandbox-feat-security-access`（docker-desktop，hostPath 掛本 Floor）→ `./.claude/skills/k8s-dev-sandbox/scripts/test.sh feat-security-access --module woow_paas_platform`

**Commit 規範**：英文 `{type}: {message}`，無 AI footer。worktree 開工先驗 base（`git rev-parse feat/security-access` 比對 HEAD，不等且 tree 乾淨則 `git reset --hard`）。

---

### Task 1: operator schemas — Access app/policy/idp pydantic models

**Files:**
- Modify: `extra/paas-operator/src/models/schemas.py`
- Test: `extra/paas-operator/tests/test_access_sync.py`（先建檔，本 task 只驗 schema import）

- [ ] **Step 1: 寫 schemas** — `schemas.py` 末尾加（先讀檔頭確認 `from pydantic import BaseModel, Field` 與 `from typing import List, Optional` 已 import；list 用 `List[...]` 對齊既有風格）：

```python
class AccessPolicySpec(BaseModel):
    """One desired Access policy (reusable) on an SA's Access App."""

    name: str = Field(..., description="Policy display name")
    decision: str = Field(..., description="allow | deny")
    rule_type: str = Field(
        ..., description="email_domain | email_list | ip_range"
    )
    rule_value: str = Field(
        ...,
        description="domain (no @) / comma-separated emails / CIDR, per rule_type",
    )
    precedence: int = Field(1, description="Evaluation order; lower first")


class AccessSyncRequest(BaseModel):
    """Declarative full-state sync of one SA's Cloudflare Access config.

    enabled=False → operator tears down the app + its reusable policies.
    """

    enabled: bool = Field(..., description="Master Access protection toggle")
    canonical_name: str = Field(
        ..., description="SA canonical name; stamped on app/policy names"
    )
    app_name: str = Field(..., description="Access Application display name")
    primary_hostname: str = Field(
        ..., description="App primary domain (SA primary hostname)"
    )
    protected_hostnames: List[str] = Field(
        default_factory=list,
        description="Hostnames to protect (app destinations); subset of routes",
    )
    session_duration: str = Field(
        "24h", description='Go duration string, e.g. "8h" / "24h"'
    )
    login_idp_types: List[str] = Field(
        default_factory=list,
        description='IdP types to allow, e.g. ["google", "onetimepin"]; '
                    "empty = all available",
    )
    policies: List[AccessPolicySpec] = Field(default_factory=list)


class AccessSyncResponse(BaseModel):
    app_id: Optional[str] = Field(None, description="CF Access App id (None if disabled/torn down)")
    policy_ids: List[str] = Field(default_factory=list)
    allowed_idps: List[str] = Field(
        default_factory=list, description="Resolved IdP ids set on the app"
    )


class IdentityProviderInfo(BaseModel):
    id: str
    name: str
    type: str


class IdentityProvidersResponse(BaseModel):
    identity_providers: List[IdentityProviderInfo]
```

- [ ] **Step 2: 建測試檔頭 + schema import 測試** — 建 `extra/paas-operator/tests/test_access_sync.py`：

```python
"""Tests for Cloudflare Access (Zero Trust) sync on Security Access."""
from unittest.mock import AsyncMock, patch

import pytest

from src.services.cloudflare import CloudflareService, CloudflareException
from src.models.schemas import (
    AccessSyncRequest,
    AccessPolicySpec,
)


def test_access_schemas_importable():
    req = AccessSyncRequest(
        enabled=True, canonical_name="paas-sa-x-y", app_name="x",
        primary_hostname="paas-sa-x-y.woowtech.io",
        protected_hostnames=["paas-sa-x-y.woowtech.io"],
        session_duration="8h", login_idp_types=["google", "onetimepin"],
        policies=[AccessPolicySpec(name="p1", decision="allow",
                                   rule_type="email_domain",
                                   rule_value="acme.com", precedence=1)],
    )
    assert req.enabled is True
    assert req.policies[0].decision == "allow"


@pytest.fixture
def cf_service():
    svc = CloudflareService()
    svc.api_token = "test-token"
    svc.account_id = "test-account"
    svc.zone_id = "test-zone"
    svc.domain = "woowtech.io"
    svc.enabled = True
    return svc
```

- [ ] **Step 3: 跑測試確認 schema 可 import**

Run: `cd extra/paas-operator && /tmp/sa-op-venv/bin/python -m pytest tests/test_access_sync.py::test_access_schemas_importable -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add extra/paas-operator/src/models/schemas.py extra/paas-operator/tests/test_access_sync.py
git commit -m "feat: add cloudflare access sync schemas to operator"
```

---

### Task 2: operator CloudflareService — list IdPs + reusable policy CRUD

**Files:**
- Modify: `extra/paas-operator/src/services/cloudflare.py`
- Test: `extra/paas-operator/tests/test_access_sync.py`（追加）

- [ ] **Step 1: 寫失敗測試** — 追加：

```python
class TestListIdentityProviders:
    @pytest.mark.asyncio
    async def test_lists_and_filters_by_type(self, cf_service):
        payload = {"success": True, "result": [
            {"id": "idp-g", "name": "Google", "type": "google"},
            {"id": "idp-otp", "name": "One-time PIN", "type": "onetimepin"},
            {"id": "idp-gh", "name": "GH", "type": "github"},
        ]}

        class FakeResponse:
            status_code = 200
            @staticmethod
            def json():
                return payload

        with patch("httpx.AsyncClient.get",
                   new=AsyncMock(return_value=FakeResponse())):
            idps = await cf_service.list_identity_providers()
        by_type = {i["type"]: i["id"] for i in idps}
        assert by_type["google"] == "idp-g"
        assert by_type["onetimepin"] == "idp-otp"

    @pytest.mark.asyncio
    async def test_raises_on_403(self, cf_service):
        class FakeResponse:
            status_code = 403
            text = "forbidden"
            @staticmethod
            def json():
                return {"success": False}

        with patch("httpx.AsyncClient.get",
                   new=AsyncMock(return_value=FakeResponse())):
            with pytest.raises(CloudflareException):
                await cf_service.list_identity_providers()


class TestReusablePolicyCrud:
    @pytest.mark.asyncio
    async def test_create_reusable_policy_email_domain(self, cf_service):
        captured = {}

        class FakeResponse:
            status_code = 201
            @staticmethod
            def json():
                return {"success": True, "result": {"id": "pol-1"}}

        async def fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

        with patch("httpx.AsyncClient.post",
                   new=AsyncMock(side_effect=fake_post)):
            pid = await cf_service.create_reusable_policy(
                name="paas-sa-x-y: corp", decision="allow",
                include=[{"email_domain": {"domain": "acme.com"}}])
        assert pid == "pol-1"
        assert captured["json"]["decision"] == "allow"
        assert captured["json"]["include"] == [
            {"email_domain": {"domain": "acme.com"}}]
        assert captured["json"]["reusable"] is True
        assert "/access/policies" in captured["url"]

    @pytest.mark.asyncio
    async def test_delete_reusable_policy(self, cf_service):
        class FakeResponse:
            status_code = 200
            @staticmethod
            def json():
                return {"success": True, "result": {"id": "pol-1"}}

        with patch("httpx.AsyncClient.delete",
                   new=AsyncMock(return_value=FakeResponse())):
            ok = await cf_service.delete_reusable_policy("pol-1")
        assert ok is True
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd extra/paas-operator && /tmp/sa-op-venv/bin/python -m pytest tests/test_access_sync.py::TestListIdentityProviders tests/test_access_sync.py::TestReusablePolicyCrud -v`
Expected: FAIL — no attribute `list_identity_providers`

- [ ] **Step 3: 實作** — `cloudflare.py`，在檔末（既有 Tunnel 區段後）加一個新區段：

```python
    # ==================== Cloudflare Access (Zero Trust) ====================

    @property
    def _access_base(self) -> str:
        return f"{self.BASE_URL}/accounts/{self.account_id}/access"

    async def list_identity_providers(self) -> List[dict]:
        """List the account's Access identity providers.

        Returns list of {id, name, type}. Needs token scope
        'Access: Organizations, Identity Providers and Groups (Read)' —
        a 403 here means that scope is missing.
        """
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self._access_base}/identity_providers",
                headers=self._headers,
                timeout=30.0,
            )
            if response.status_code != 200:
                raise CloudflareException(
                    f"Failed to list identity providers: {response.text}",
                    status_code=response.status_code,
                )
            data = response.json()
            if not data.get("success"):
                raise CloudflareException(
                    f"Cloudflare API error: {data.get('errors', 'Unknown')}"
                )
            return data.get("result", [])

    async def create_reusable_policy(
        self,
        name: str,
        decision: str,
        include: List[dict],
        exclude: Optional[List[dict]] = None,
        require: Optional[List[dict]] = None,
    ) -> str:
        """Create a reusable Access policy; return its id."""
        payload = {
            "name": name,
            "decision": decision,
            "include": include,
            "exclude": exclude or [],
            "require": require or [],
            "reusable": True,
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._access_base}/policies",
                headers=self._headers,
                json=payload,
                timeout=30.0,
            )
            if response.status_code not in (200, 201):
                raise CloudflareException(
                    f"Failed to create access policy: {response.text}",
                    status_code=response.status_code,
                )
            data = response.json()
            if not data.get("success"):
                raise CloudflareException(
                    f"Cloudflare API error: {data.get('errors', 'Unknown')}"
                )
            return data["result"]["id"]

    async def delete_reusable_policy(self, policy_id: str) -> bool:
        """Delete a reusable Access policy by id."""
        async with httpx.AsyncClient() as client:
            response = await client.delete(
                f"{self._access_base}/policies/{policy_id}",
                headers=self._headers,
                timeout=30.0,
            )
            if response.status_code != 200:
                raise CloudflareException(
                    f"Failed to delete access policy {policy_id}: {response.text}",
                    status_code=response.status_code,
                )
            return True
```

注意：`List` / `Optional` 已在檔頭 `from typing import` import（P1 已用）。`self.BASE_URL` 與 `httpx` 已存在。

- [ ] **Step 4: 跑測試確認通過**（同 Step 2）Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add extra/paas-operator/src/services/cloudflare.py extra/paas-operator/tests/test_access_sync.py
git commit -m "feat: add access idp listing and reusable policy crud to operator"
```

---

### Task 3: operator CloudflareService — declarative `sync_access_app` + teardown

宣告式：給「完整 desired Access state」→ operator reconcile 出一個 Access App（destinations + allowed_idps + session_duration）+ 重建該 SA 的 reusable policies 並掛上 app。`enabled=False` → 拆掉 app + policies。冪等。

**Files:**
- Modify: `extra/paas-operator/src/services/cloudflare.py`
- Test: `extra/paas-operator/tests/test_access_sync.py`（追加）

- [ ] **Step 1: 寫失敗測試** — 追加：

```python
class TestSyncAccessApp:
    @pytest.mark.asyncio
    async def test_enabled_creates_app_and_policies(self, cf_service):
        calls = {"app_put": None, "policy_posts": [], "list_apps": True}

        async def fake_get(url, headers=None, timeout=None, params=None):
            class R:
                status_code = 200
                @staticmethod
                def json():
                    # identity_providers list (type→id) and app list (none yet)
                    if "identity_providers" in url:
                        return {"success": True, "result": [
                            {"id": "idp-g", "name": "G", "type": "google"},
                            {"id": "idp-otp", "name": "O", "type": "onetimepin"},
                        ]}
                    return {"success": True, "result": []}
            return R()

        async def fake_post(url, headers=None, json=None, timeout=None):
            class R:
                status_code = 201
                @staticmethod
                def json():
                    if "/policies" in url:
                        return {"success": True, "result": {"id": "pol-new"}}
                    return {"success": True, "result": {"id": "app-new"}}
            if "/policies" in url:
                calls["policy_posts"].append(json)
            return R()

        async def fake_put(url, headers=None, json=None, timeout=None):
            calls["app_put"] = json
            class R:
                status_code = 200
                @staticmethod
                def json():
                    return {"success": True, "result": {"id": "app-existing"}}
            return R()

        with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)), \
             patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=fake_post)), \
             patch("httpx.AsyncClient.put", new=AsyncMock(side_effect=fake_put)), \
             patch("httpx.AsyncClient.delete", new=AsyncMock(return_value=type(
                 "R", (), {"status_code": 200,
                           "json": staticmethod(lambda: {"success": True,
                                                         "result": {"id": "x"}})})())):
            result = await cf_service.sync_access_app(
                canonical_name="paas-sa-x-y",
                app_name="my-gw",
                primary_hostname="paas-sa-x-y.woowtech.io",
                protected_hostnames=["paas-sa-x-y.woowtech.io",
                                     "waltek-ha.woowtech.io"],
                session_duration="8h",
                login_idp_types=["google", "onetimepin"],
                policies=[{"name": "corp", "decision": "allow",
                           "rule_type": "email_domain", "rule_value": "acme.com",
                           "precedence": 1}],
                existing_app_id=None,
                existing_policy_ids=[],
            )
        # allowed_idps resolved from types
        assert set(result["allowed_idps"]) == {"idp-g", "idp-otp"}
        # one reusable policy created
        assert len(calls["policy_posts"]) == 1
        assert calls["policy_posts"][0]["include"] == [
            {"email_domain": {"domain": "acme.com"}}]
        assert result["app_id"]
        assert result["policy_ids"] == ["pol-new"]

    @pytest.mark.asyncio
    async def test_disabled_tears_down(self, cf_service):
        deleted = {"app": [], "policies": []}

        async def fake_delete(url, headers=None, timeout=None):
            if "/apps/" in url:
                deleted["app"].append(url)
            elif "/policies/" in url:
                deleted["policies"].append(url)
            class R:
                status_code = 200
                @staticmethod
                def json():
                    return {"success": True, "result": {"id": "x"}}
            return R()

        with patch("httpx.AsyncClient.delete",
                   new=AsyncMock(side_effect=fake_delete)):
            result = await cf_service.sync_access_app(
                canonical_name="paas-sa-x-y", app_name="my-gw",
                primary_hostname="paas-sa-x-y.woowtech.io",
                protected_hostnames=[], session_duration="8h",
                login_idp_types=[], policies=[], enabled=False,
                existing_app_id="app-old", existing_policy_ids=["pol-a", "pol-b"])
        assert len(deleted["app"]) == 1
        assert len(deleted["policies"]) == 2
        assert result["app_id"] is None
        assert result["policy_ids"] == []
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd extra/paas-operator && /tmp/sa-op-venv/bin/python -m pytest tests/test_access_sync.py::TestSyncAccessApp -v`
Expected: FAIL — no attribute `sync_access_app`

- [ ] **Step 3: 實作** — `cloudflare.py` 的 Access 區段加（在 Task 2 方法之後）：

```python
    async def _resolve_idp_ids(self, idp_types: List[str]) -> List[str]:
        """Map IdP type strings (google/onetimepin) to account IdP ids.

        Empty input → empty list (= app shows all available IdPs). A type
        with no matching IdP on the account is skipped (logged), not fatal.
        """
        if not idp_types:
            return []
        idps = await self.list_identity_providers()
        by_type = {}
        for idp in idps:
            by_type.setdefault(idp.get("type"), idp.get("id"))
        resolved = []
        for t in idp_types:
            idp_id = by_type.get(t)
            if idp_id:
                resolved.append(idp_id)
            else:
                logger.warning(
                    "Access: login method '%s' has no IdP on the account; "
                    "skipping (configure it in Zero Trust → Settings → "
                    "Authentication).", t,
                )
        return resolved

    async def _delete_access_app(self, app_id: str) -> None:
        async with httpx.AsyncClient() as client:
            response = await client.delete(
                f"{self._access_base}/apps/{app_id}",
                headers=self._headers, timeout=30.0,
            )
            if response.status_code not in (200, 404):
                raise CloudflareException(
                    f"Failed to delete access app {app_id}: {response.text}",
                    status_code=response.status_code,
                )

    async def sync_access_app(
        self,
        canonical_name: str,
        app_name: str,
        primary_hostname: str,
        protected_hostnames: List[str],
        session_duration: str,
        login_idp_types: List[str],
        policies: List[dict],
        enabled: bool = True,
        existing_app_id: Optional[str] = None,
        existing_policy_ids: Optional[List[str]] = None,
    ) -> dict:
        """Declaratively reconcile one SA's Cloudflare Access config.

        Strategy (simple + idempotent): always delete the SA's previous
        reusable policies (existing_policy_ids) then recreate from the desired
        list; create-or-update the single per-SA Access App referencing them.
        enabled=False → tear down app + policies, return empty.

        Returns {app_id, policy_ids, allowed_idps}.
        """
        existing_policy_ids = existing_policy_ids or []

        # Teardown path: disabled or no protected hostnames → remove everything.
        if not enabled or not protected_hostnames:
            if existing_app_id:
                await self._delete_access_app(existing_app_id)
            for pid in existing_policy_ids:
                try:
                    await self.delete_reusable_policy(pid)
                except CloudflareException as e:
                    logger.warning("Access teardown: policy %s delete failed: %s",
                                   pid, e.message)
            return {"app_id": None, "policy_ids": [], "allowed_idps": []}

        # Recreate reusable policies from desired state (delete-old-then-create).
        for pid in existing_policy_ids:
            try:
                await self.delete_reusable_policy(pid)
            except CloudflareException as e:
                logger.warning("Access resync: stale policy %s delete failed: %s",
                               pid, e.message)
        new_policy_ids = []
        for idx, p in enumerate(policies):
            include = self._build_access_rule(p["rule_type"], p["rule_value"])
            pid = await self.create_reusable_policy(
                name=f"{canonical_name}: {p['name']}",
                decision=p["decision"],
                include=include,
            )
            new_policy_ids.append((pid, p.get("precedence", idx + 1)))

        allowed_idps = await self._resolve_idp_ids(login_idp_types)

        app_payload = {
            "name": app_name,
            "type": "self_hosted",
            "domain": primary_hostname,
            "destinations": [{"type": "public", "uri": h}
                             for h in protected_hostnames],
            "session_duration": session_duration,
            "allowed_idps": allowed_idps,
            "auto_redirect_to_identity": False,
            "app_launcher_visible": False,
            "policies": [{"id": pid, "precedence": prec}
                         for pid, prec in new_policy_ids],
        }

        async with httpx.AsyncClient() as client:
            if existing_app_id:
                response = await client.put(
                    f"{self._access_base}/apps/{existing_app_id}",
                    headers=self._headers, json=app_payload, timeout=30.0,
                )
            else:
                response = await client.post(
                    f"{self._access_base}/apps",
                    headers=self._headers, json=app_payload, timeout=30.0,
                )
            if response.status_code not in (200, 201):
                raise CloudflareException(
                    f"Failed to sync access app: {response.text}",
                    status_code=response.status_code,
                )
            data = response.json()
            if not data.get("success"):
                raise CloudflareException(
                    f"Cloudflare API error: {data.get('errors', 'Unknown')}"
                )
            app_id = data["result"]["id"]

        return {
            "app_id": app_id,
            "policy_ids": [pid for pid, _ in new_policy_ids],
            "allowed_idps": allowed_idps,
        }

    @staticmethod
    def _build_access_rule(rule_type: str, rule_value: str) -> List[dict]:
        """Map (rule_type, rule_value) to a Cloudflare Access include rule list."""
        if rule_type == "email_domain":
            return [{"email_domain": {"domain": rule_value.lstrip("@").strip()}}]
        if rule_type == "email_list":
            emails = [e.strip() for e in rule_value.split(",") if e.strip()]
            return [{"email": {"email": e}} for e in emails]
        if rule_type == "ip_range":
            return [{"ip": {"ip": rule_value.strip()}}]
        raise CloudflareException(f"Unknown access rule_type: {rule_type}")
```

- [ ] **Step 4: 跑測試確認通過**（同 Step 2）Expected: 全 PASS

- [ ] **Step 5: 跑既有 operator suite 確認不回歸**

Run: `cd extra/paas-operator && /tmp/sa-op-venv/bin/python -m pytest tests/ -m "not integration"`
Expected: 全 PASS（P1 的 154 + 本 task 新增）

- [ ] **Step 6: Commit**

```bash
git add extra/paas-operator/src/services/cloudflare.py extra/paas-operator/tests/test_access_sync.py
git commit -m "feat: add declarative access app sync to operator cloudflare service"
```

---

### Task 4: operator API — Access router（IdP list + app sync endpoint）

**Files:**
- Create: `extra/paas-operator/src/api/access.py`
- Modify: `extra/paas-operator/src/main.py`
- Test: `extra/paas-operator/tests/test_access_sync.py`（追加 endpoint 測試）

- [ ] **Step 1: 寫失敗測試** — 追加（client fixture 鏡像 test_tunnel_routes_sync.py 的 `sync_patch("src.main.settings")`）：

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


class TestAccessEndpoints:
    @sync_patch("src.api.access.cloudflare_service")
    def test_sync_access_happy_path(self, mock_cf, client):
        mock_cf.api_token = "t"
        mock_cf.account_id = "a"
        mock_cf.sync_access_app = AsyncMock(return_value={
            "app_id": "app-1", "policy_ids": ["pol-1"],
            "allowed_idps": ["idp-g", "idp-otp"]})
        resp = client.put("/api/access/sync", json={
            "enabled": True, "canonical_name": "paas-sa-x-y", "app_name": "gw",
            "primary_hostname": "paas-sa-x-y.woowtech.io",
            "protected_hostnames": ["paas-sa-x-y.woowtech.io"],
            "session_duration": "8h", "login_idp_types": ["google", "onetimepin"],
            "policies": [{"name": "corp", "decision": "allow",
                          "rule_type": "email_domain", "rule_value": "acme.com",
                          "precedence": 1}],
        })
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["app_id"] == "app-1"
        assert data["policy_ids"] == ["pol-1"]
        call = mock_cf.sync_access_app.call_args
        assert call.kwargs["login_idp_types"] == ["google", "onetimepin"]

    @sync_patch("src.api.access.cloudflare_service")
    def test_sync_requires_credentials(self, mock_cf, client):
        mock_cf.api_token = None
        mock_cf.account_id = None
        resp = client.put("/api/access/sync", json={
            "enabled": True, "canonical_name": "x", "app_name": "x",
            "primary_hostname": "x.woowtech.io", "protected_hostnames": [],
            "session_duration": "8h", "login_idp_types": [], "policies": []})
        assert resp.status_code == 400

    @sync_patch("src.api.access.cloudflare_service")
    def test_list_idps(self, mock_cf, client):
        mock_cf.api_token = "t"
        mock_cf.account_id = "a"
        mock_cf.list_identity_providers = AsyncMock(return_value=[
            {"id": "idp-g", "name": "Google", "type": "google"},
            {"id": "idp-otp", "name": "OTP", "type": "onetimepin"}])
        resp = client.get("/api/access/identity-providers")
        assert resp.status_code == 200
        types = [i["type"] for i in resp.json()["identity_providers"]]
        assert "google" in types and "onetimepin" in types
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd extra/paas-operator && /tmp/sa-op-venv/bin/python -m pytest tests/test_access_sync.py::TestAccessEndpoints -v`
Expected: FAIL — 404（router 未註冊）

- [ ] **Step 3: 實作 router** — 建 `extra/paas-operator/src/api/access.py`：

```python
"""API endpoints for Cloudflare Access (Zero Trust) on Security Access."""
import logging

from fastapi import APIRouter, HTTPException, status

from src.models.schemas import (
    AccessSyncRequest,
    AccessSyncResponse,
    IdentityProviderInfo,
    IdentityProvidersResponse,
)
from src.services.cloudflare import CloudflareException, CloudflareService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/access", tags=["access"])

cloudflare_service = CloudflareService()


def _require_credentials() -> None:
    if not cloudflare_service.api_token or not cloudflare_service.account_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cloudflare API token and account ID are required for Access",
        )


@router.get(
    "/identity-providers",
    response_model=IdentityProvidersResponse,
    summary="List account Access identity providers",
)
async def list_identity_providers():
    """List IdPs so the caller can see available login methods.

    403 from Cloudflare here means the token lacks
    'Access: Organizations, Identity Providers and Groups (Read)'.
    """
    _require_credentials()
    try:
        idps = await cloudflare_service.list_identity_providers()
    except CloudflareException as e:
        logger.error("Failed to list identity providers: %s", e.message)
        raise HTTPException(
            status_code=e.status_code or status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list identity providers: {e.message}",
        )
    return IdentityProvidersResponse(identity_providers=[
        IdentityProviderInfo(id=i.get("id", ""), name=i.get("name", ""),
                             type=i.get("type", ""))
        for i in idps
    ])


@router.put(
    "/sync",
    response_model=AccessSyncResponse,
    summary="Declaratively sync one SA's Cloudflare Access config",
)
async def sync_access(request: AccessSyncRequest):
    """Reconcile the per-SA Access Application + reusable policies."""
    _require_credentials()
    try:
        result = await cloudflare_service.sync_access_app(
            canonical_name=request.canonical_name,
            app_name=request.app_name,
            primary_hostname=request.primary_hostname,
            protected_hostnames=request.protected_hostnames,
            session_duration=request.session_duration,
            login_idp_types=request.login_idp_types,
            policies=[p.model_dump() for p in request.policies],
            enabled=request.enabled,
            existing_app_id=getattr(request, "existing_app_id", None),
            existing_policy_ids=getattr(request, "existing_policy_ids", None),
        )
    except CloudflareException as e:
        logger.error("Failed to sync access: %s", e.message)
        raise HTTPException(
            status_code=e.status_code or status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to sync access: {e.message}",
        )
    return AccessSyncResponse(**result)
```

**注意**：`existing_app_id` / `existing_policy_ids` 需讓 Odoo 傳入（reconcile 要知道舊的去刪/更新）。把這兩欄加進 `AccessSyncRequest`（Task 1 的 schema 補兩行）：

```python
    existing_app_id: Optional[str] = Field(None)
    existing_policy_ids: Optional[List[str]] = Field(None)
```

（若 Task 1 已 commit，本 step 順手補上並一起 commit。）

- [ ] **Step 4: 註冊 router** — `extra/paas-operator/src/main.py` 的 include_router 區（agent_runtimes 後）加：

```python
from src.api import access  # 與其他 from src.api import ... 並列
...
app.include_router(access.router)
```

（先讀 main.py 頭部確認 router import 形式，對齊。）

- [ ] **Step 5: 跑測試確認通過 + 全套不回歸**

Run: `cd extra/paas-operator && /tmp/sa-op-venv/bin/python -m pytest tests/test_access_sync.py -v && /tmp/sa-op-venv/bin/python -m pytest tests/ -m "not integration"`
Expected: 全 PASS

- [ ] **Step 6: Commit**

```bash
git add extra/paas-operator/src/api/access.py extra/paas-operator/src/main.py extra/paas-operator/src/models/schemas.py extra/paas-operator/tests/test_access_sync.py
git commit -m "feat: add operator access router with idp list and app sync endpoints"
```

---

### Task 5: Odoo operator client — access methods

**Files:**
- Modify: `src/services/paas_operator.py`
- Test: `src/tests/test_paas_operator.py`（追加；mock `_request` 風格鏡像 P1 的 TestSecurityAccessClientMethods）

- [ ] **Step 1: 寫失敗測試** — 追加：

```python
class TestSecurityAccessAccessClientMethods(TransactionCase):
    """sync_access_app / list_identity_providers HTTP contract."""

    def _make_client(self):
        from odoo.addons.woow_paas_platform.services.paas_operator import (
            PaaSOperatorClient,
        )
        return PaaSOperatorClient(base_url='http://op:8000', api_key='k')

    def test_sync_access_contract(self):
        client = self._make_client()
        with patch.object(client, '_request') as mock_req:
            mock_req.return_value = {
                'app_id': 'app-1', 'policy_ids': ['pol-1'],
                'allowed_idps': ['idp-g']}
            result = client.sync_access_app({
                'enabled': True, 'canonical_name': 'paas-sa-x-y',
                'app_name': 'gw', 'primary_hostname': 'paas-sa-x-y.woowtech.io',
                'protected_hostnames': ['paas-sa-x-y.woowtech.io'],
                'session_duration': '8h', 'login_idp_types': ['google'],
                'policies': [], 'existing_app_id': None,
                'existing_policy_ids': []})
        mock_req.assert_called_once_with(
            'PUT', '/api/access/sync', data={
                'enabled': True, 'canonical_name': 'paas-sa-x-y',
                'app_name': 'gw', 'primary_hostname': 'paas-sa-x-y.woowtech.io',
                'protected_hostnames': ['paas-sa-x-y.woowtech.io'],
                'session_duration': '8h', 'login_idp_types': ['google'],
                'policies': [], 'existing_app_id': None,
                'existing_policy_ids': []},
            timeout=120)
        self.assertEqual(result['app_id'], 'app-1')

    def test_list_identity_providers_contract(self):
        client = self._make_client()
        with patch.object(client, '_request') as mock_req:
            mock_req.return_value = {'identity_providers': [
                {'id': 'idp-g', 'name': 'G', 'type': 'google'}]}
            result = client.list_identity_providers()
        mock_req.assert_called_once_with(
            'GET', '/api/access/identity-providers')
        self.assertEqual(result['identity_providers'][0]['type'], 'google')
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `./.claude/skills/k8s-dev-sandbox/scripts/test.sh feat-security-access --module woow_paas_platform`（找 TestSecurityAccessAccessClientMethods FAIL — no attribute `sync_access_app`）

- [ ] **Step 3: 實作** — `src/services/paas_operator.py`，加在 `sync_tunnel_routes` 後：

```python
    def sync_access_app(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Declaratively sync one SA's Cloudflare Access config.

        payload keys: enabled, canonical_name, app_name, primary_hostname,
        protected_hostnames, session_duration, login_idp_types, policies,
        existing_app_id, existing_policy_ids. Returns {app_id, policy_ids,
        allowed_idps}. Idempotent.
        """
        return self._request(
            'PUT', '/api/access/sync', data=payload,
            timeout=HELM_OPERATION_TIMEOUT,
        )

    def list_identity_providers(self) -> Dict[str, Any]:
        """List account Access IdPs ({identity_providers: [{id,name,type}]})."""
        return self._request('GET', '/api/access/identity-providers')
```

- [ ] **Step 4: 跑測試確認通過**（同 Step 2，sandbox）Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/services/paas_operator.py src/tests/test_paas_operator.py
git commit -m "feat: add access app sync and idp list client methods"
```

---

### Task 6: 主模型 access 欄位 + access_policy 子模型

**Files:**
- Modify: `src/models/security_access.py`（主模型加欄位）
- Create: `src/models/security_access_access_policy.py`
- Modify: `src/models/__init__.py`
- Test: `src/tests/test_security_access_access_policy.py`（新檔）
- Modify: `src/tests/__init__.py`

- [ ] **Step 1: 寫失敗測試** — 建 `src/tests/test_security_access_access_policy.py`：

```python
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase

MOCK_GET_CLIENT = (
    'odoo.addons.woow_paas_platform.models.security_access.'
    'get_paas_operator_client'
)


class TestAccessPolicyModel(TransactionCase):
    """access_policy 子模型 + 主模型 access 欄位."""

    def setUp(self):
        super().setUp()
        self.SA = self.env['woow_paas_platform.security_access']
        self.Policy = self.env['woow_paas_platform.security_access_access_policy']
        self.workspace = self.env['woow_paas_platform.workspace'].create(
            {'name': 'AP WS'})

    def _make_sa(self):
        return self.SA.create({'name': 'AP GW', 'workspace_id': self.workspace.id})

    def test_main_model_access_fields_default(self):
        sa = self._make_sa()
        self.assertFalse(sa.access_enabled)
        self.assertFalse(sa.login_google)
        self.assertFalse(sa.login_email_otp)
        self.assertEqual(sa.session_duration, '24h')

    def test_create_policy(self):
        sa = self._make_sa()
        pol = self.Policy.create({
            'security_access_id': sa.id, 'name': 'corp',
            'decision': 'allow', 'rule_type': 'email_domain',
            'rule_value': 'acme.com', 'sequence': 1})
        self.assertIn(pol, sa.access_policy_ids)

    def test_policy_rule_value_validation(self):
        sa = self._make_sa()
        # email_domain 不可含 @
        with self.assertRaises(ValidationError):
            self.Policy.create({
                'security_access_id': sa.id, 'name': 'bad',
                'decision': 'allow', 'rule_type': 'email_domain',
                'rule_value': '@acme.com'})
        # ip_range 必須是 CIDR
        with self.assertRaises(ValidationError):
            self.Policy.create({
                'security_access_id': sa.id, 'name': 'bad2',
                'decision': 'allow', 'rule_type': 'ip_range',
                'rule_value': 'not-an-ip'})

    def test_policy_quota_enforced(self):
        sa = self._make_sa()
        plan = self.env['sh.subscription.plan'].create({
            'name': 'AP Plan', 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 100, 'sh_no_of_billing_cycle': -1,
            'sa_max_access_policies': 1})
        sub = self.env['sh.subscription.subscription'].create({
            'sh_partner_id': self.env.user.partner_id.id, 'state': 'in_progress'})
        sub.sh_subscription_plan_id = plan.id
        sa.subscription_id = sub.id
        self.Policy.create({
            'security_access_id': sa.id, 'name': 'p1', 'decision': 'allow',
            'rule_type': 'email_domain', 'rule_value': 'a.com'})
        with self.assertRaises(ValidationError):
            self.Policy.create({
                'security_access_id': sa.id, 'name': 'p2', 'decision': 'allow',
                'rule_type': 'email_domain', 'rule_value': 'b.com'})
```

`src/tests/__init__.py` 加 `from . import test_security_access_access_policy`

- [ ] **Step 2: 跑測試確認失敗**（sandbox；access_enabled 欄位與 model 不存在）

- [ ] **Step 3: 實作主模型欄位** — `src/models/security_access.py`，在主模型的 `# Access（服務層共用設定）` 區（P1 spec 設計但未實作，現補）。找 `route_ids = fields.One2many(` 之前插入：

```python
    # Access Control（P2；服務層共用設定）
    access_enabled = fields.Boolean(
        string='Access Protection Enabled',
        default=False,
        help='Master toggle: gate protected routes behind Cloudflare Access.',
    )
    session_duration = fields.Selection(
        selection=[
            ('30m', '30 minutes'), ('1h', '1 hour'), ('8h', '8 hours'),
            ('24h', '24 hours'), ('168h', '7 days'),
        ],
        string='Default Session Duration',
        default='24h',
        help='How long users stay authenticated before re-login.',
    )
    login_google = fields.Boolean(string='Login: Google', default=False)
    login_email_otp = fields.Boolean(string='Login: Email OTP', default=False)
    access_app_id = fields.Char(
        string='CF Access App ID', copy=False,
        help='Cloudflare Access Application id for this SA (set on sync).',
    )
    access_policy_ids = fields.One2many(
        comodel_name='woow_paas_platform.security_access_access_policy',
        inverse_name='security_access_id',
        string='Access Policies',
    )
```

- [ ] **Step 4: 實作 access_policy 子模型** — 建 `src/models/security_access_access_policy.py`：

```python
import ipaddress
import re

from odoo import api, fields, models
from odoo.exceptions import ValidationError

_DOMAIN_RE = re.compile(r'^[a-z0-9.-]+\.[a-z]{2,}$', re.IGNORECASE)


class SecurityAccessAccessPolicy(models.Model):
    """One reusable Cloudflare Access policy on an SA (service-layer shared).

    Mirrors the route sub-model: rows are the desired-state, the parent SA
    pushes the full set declaratively to the operator (no per-row CF call).
    cf_policy_id stores the Cloudflare reusable-policy id after sync.
    """

    _name = 'woow_paas_platform.security_access_access_policy'
    _description = 'Security Access — Access Policy'
    _order = 'sequence, id'

    security_access_id = fields.Many2one(
        comodel_name='woow_paas_platform.security_access',
        string='Security Access', required=True, ondelete='cascade', index=True,
    )
    name = fields.Char(string='Name', required=True)
    decision = fields.Selection(
        selection=[('allow', 'Allow'), ('deny', 'Block')],
        string='Decision', default='allow', required=True,
        help='Allow grants access; Block denies. Block rules win over allow.',
    )
    rule_type = fields.Selection(
        selection=[
            ('email_domain', 'Email domain'),
            ('email_list', 'Email list'),
            ('ip_range', 'IP range (CIDR)'),
        ],
        string='Rule Type', default='email_domain', required=True,
    )
    rule_value = fields.Char(
        string='Rule Value', required=True,
        help='Email domain (no @) / comma-separated emails / CIDR, per type.',
    )
    sequence = fields.Integer(string='Evaluation Order', default=10)
    cf_policy_id = fields.Char(string='CF Policy ID', copy=False)

    @api.constrains('rule_type', 'rule_value')
    def _check_rule_value(self):
        for pol in self:
            val = (pol.rule_value or '').strip()
            if pol.rule_type == 'email_domain':
                if val.startswith('@') or not _DOMAIN_RE.match(val):
                    raise ValidationError(
                        "Email domain must be a bare domain (no @), e.g. "
                        "acme.com — got %r" % pol.rule_value)
            elif pol.rule_type == 'email_list':
                emails = [e.strip() for e in val.split(',') if e.strip()]
                if not emails or any('@' not in e for e in emails):
                    raise ValidationError(
                        "Email list must be comma-separated email addresses.")
            elif pol.rule_type == 'ip_range':
                try:
                    ipaddress.ip_network(val, strict=False)
                except ValueError:
                    raise ValidationError(
                        "IP range must be a valid CIDR, e.g. 203.0.113.0/24 — "
                        "got %r" % pol.rule_value)

    @api.constrains('security_access_id')
    def _check_policy_quota(self):
        for pol in self:
            sa = pol.security_access_id
            sub = sa.sudo().subscription_id
            if not sub or not sub.sh_subscription_plan_id:
                continue
            quota = sub.sh_subscription_plan_id.sa_max_access_policies
            count = len(sa.access_policy_ids)
            if count > quota:
                raise ValidationError(
                    "Access policy quota exceeded: plan '%s' allows %d "
                    "polic(ies)." % (sub.sh_subscription_plan_id.name, quota))

    @api.model_create_multi
    def create(self, vals_list):
        policies = super().create(vals_list)
        for sa in policies.mapped('security_access_id'):
            if sa.access_enabled and sa.state == 'active' and sa.tunnel_id:
                sa._sync_access_to_operator()
        return policies

    def write(self, vals):
        result = super().write(vals)
        if any(k in vals for k in ('name', 'decision', 'rule_type',
                                   'rule_value', 'sequence')):
            for sa in self.mapped('security_access_id'):
                if sa.access_enabled and sa.state == 'active' and sa.tunnel_id:
                    sa._sync_access_to_operator()
        return result

    def unlink(self):
        affected = self.mapped('security_access_id')
        result = super().unlink()
        for sa in affected:
            if sa.exists() and sa.access_enabled and sa.state == 'active' \
                    and sa.tunnel_id:
                sa._sync_access_to_operator()
        return result

    def to_dict(self):
        self.ensure_one()
        return {
            'id': self.id, 'name': self.name, 'decision': self.decision,
            'rule_type': self.rule_type, 'rule_value': self.rule_value,
            'sequence': self.sequence,
        }
```

`src/models/__init__.py` 加 `from . import security_access_access_policy`（在 security_access 後）。

- [ ] **Step 5: 跑測試確認通過**（sandbox，需 `-u` 載入新 model）Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/models/security_access.py src/models/security_access_access_policy.py src/models/__init__.py src/tests/test_security_access_access_policy.py src/tests/__init__.py
git commit -m "feat: add access control fields and access policy submodel"
```

---

### Task 7: 主模型 `_sync_access_to_operator` 宣告式 + is_protected 接線

**Files:**
- Modify: `src/models/security_access.py`
- Test: `src/tests/test_security_access_access_policy.py`（追加）

- [ ] **Step 1: 寫失敗測試** — 追加：

```python
class TestAccessSync(TransactionCase):
    def setUp(self):
        super().setUp()
        self.SA = self.env['woow_paas_platform.security_access']
        self.Route = self.env['woow_paas_platform.security_access_route']
        self.Policy = self.env[
            'woow_paas_platform.security_access_access_policy']
        self.workspace = self.env['woow_paas_platform.workspace'].create(
            {'name': 'AS WS'})

    def _active_sa(self, mock_client):
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            sa = self.SA.create({'name': 'AS GW',
                                 'workspace_id': self.workspace.id})
            sa.write({'state': 'active', 'tunnel_id': 'tun-as'})
            self.Route.create({'security_access_id': sa.id,
                               'subdomain_prefix': sa.canonical_name,
                               'is_primary': True, 'is_protected': True})
        return sa

    def _mock(self):
        m = MagicMock()
        m.sync_tunnel_routes.return_value = {'routes': []}
        m.sync_access_app.return_value = {
            'app_id': 'app-1', 'policy_ids': ['pol-1'],
            'allowed_idps': ['idp-g', 'idp-otp']}
        return m

    def test_enable_access_syncs_app(self):
        mc = self._mock()
        sa = self._active_sa(mc)
        with patch(MOCK_GET_CLIENT, return_value=mc):
            sa.write({'access_enabled': True, 'login_google': True,
                      'login_email_otp': True, 'session_duration': '8h'})
            self.Policy.create({
                'security_access_id': sa.id, 'name': 'corp', 'decision': 'allow',
                'rule_type': 'email_domain', 'rule_value': 'acme.com'})
        payload = mc.sync_access_app.call_args.args[0]
        self.assertTrue(payload['enabled'])
        self.assertEqual(sorted(payload['login_idp_types']),
                         ['google', 'onetimepin'])
        self.assertEqual(payload['session_duration'], '8h')
        # 只有 is_protected 的 route 進 protected_hostnames
        self.assertEqual(payload['protected_hostnames'],
                         [f"{sa.canonical_name}.{sa._get_paas_domain()}"])
        self.assertEqual(payload['policies'][0]['rule_value'], 'acme.com')
        # app_id / policy_ids 寫回
        self.assertEqual(sa.access_app_id, 'app-1')

    def test_protected_route_toggle_resyncs(self):
        mc = self._mock()
        sa = self._active_sa(mc)
        with patch(MOCK_GET_CLIENT, return_value=mc):
            sa.write({'access_enabled': True})
            mc.sync_access_app.reset_mock()
            r2 = self.Route.create({'security_access_id': sa.id,
                                    'subdomain_prefix': 'as-extra',
                                    'is_protected': False})
            # 改 is_protected 觸發 access re-sync
            r2.write({'is_protected': True})
        self.assertTrue(mc.sync_access_app.called)
        payload = mc.sync_access_app.call_args.args[0]
        hosts = payload['protected_hostnames']
        self.assertIn(f"as-extra.{sa._get_paas_domain()}", hosts)

    def test_disable_access_tears_down(self):
        mc = self._mock()
        sa = self._active_sa(mc)
        with patch(MOCK_GET_CLIENT, return_value=mc):
            sa.write({'access_enabled': True})
            sa.write({'access_app_id': 'app-1'})
            mc.sync_access_app.reset_mock()
            sa.write({'access_enabled': False})
        payload = mc.sync_access_app.call_args.args[0]
        self.assertFalse(payload['enabled'])
        self.assertEqual(payload['existing_app_id'], 'app-1')
```

- [ ] **Step 2: 跑測試確認失敗**（sandbox；無 `_sync_access_to_operator`）

- [ ] **Step 3: 實作** — `src/models/security_access.py`，加在 `_sync_routes_to_operator` 後：

```python
    def _build_access_payload(self):
        """Desired-state payload for operator's /api/access/sync."""
        self.ensure_one()
        domain = self._get_paas_domain()
        login_idp_types = []
        if self.login_google:
            login_idp_types.append('google')
        if self.login_email_otp:
            login_idp_types.append('onetimepin')
        protected = [
            f"{r.subdomain_prefix}.{domain}"
            for r in self.route_ids.filtered('is_protected')
        ]
        return {
            'enabled': bool(self.access_enabled),
            'canonical_name': self.canonical_name,
            'app_name': self.name or self.canonical_name,
            'primary_hostname': self.primary_hostname,
            'protected_hostnames': protected,
            'session_duration': self.session_duration or '24h',
            'login_idp_types': login_idp_types,
            'policies': [{
                'name': p.name, 'decision': p.decision,
                'rule_type': p.rule_type, 'rule_value': p.rule_value,
                'precedence': p.sequence,
            } for p in self.access_policy_ids.sorted(key=lambda x: (x.sequence, x.id))],
            'existing_app_id': self.access_app_id or None,
            'existing_policy_ids': [
                p.cf_policy_id for p in self.access_policy_ids if p.cf_policy_id
            ],
        }

    def _sync_access_to_operator(self):
        """Push the full desired Access state to the operator (idempotent)."""
        self.ensure_one()
        if not self.tunnel_id:
            return
        client = self._get_operator_client()
        result = client.sync_access_app(self._build_access_payload())
        # write back CF ids
        self.access_app_id = result.get('app_id') or False
        returned = result.get('policy_ids') or []
        # map returned policy ids back onto policy rows in order
        policies = self.access_policy_ids.sorted(key=lambda x: (x.sequence, x.id))
        for pol, cf_id in zip(policies, returned):
            if pol.cf_policy_id != cf_id:
                pol.cf_policy_id = cf_id
        if not result.get('app_id'):
            # torn down → clear stale cf ids
            for pol in self.access_policy_ids:
                if pol.cf_policy_id:
                    pol.cf_policy_id = False
```

並在主模型 `write()` 末尾（既有 identity-guard 之後、return 之前；先讀現有 `write` 確認結構）加 access 觸發：

```python
        result = super().write(vals)
        if any(k in vals for k in ('access_enabled', 'login_google',
                                   'login_email_otp', 'session_duration')):
            for record in self:
                if record.state == 'active' and record.tunnel_id:
                    record._sync_access_to_operator()
        return result
```

route 子模型的 `write()` 已在 P1 對 `service_url`/`http_host_header` re-sync routes；**追加 is_protected 觸發 access re-sync**（`SecurityAccessRoute.write`）：

```python
        if 'is_protected' in vals:
            for sa in self.mapped('security_access_id'):
                if sa.access_enabled and sa.state == 'active' and sa.tunnel_id:
                    sa._sync_access_to_operator()
```

route 的 `create` / `unlink` 也要在 access_enabled 時 re-sync（受保護 hostname 集合變了）——在既有 route create/unlink 的 route-sync 後加同樣的 access re-sync 條件呼叫。

- [ ] **Step 4: 跑測試確認通過**（sandbox）Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/models/security_access.py src/tests/test_security_access_access_policy.py
git commit -m "feat: wire declarative access sync to operator on config and route changes"
```

---

### Task 8: Lifecycle — suspend/resume/cleanup 對 Access

**Files:**
- Modify: `src/models/security_access.py`
- Test: `src/tests/test_security_access_access_policy.py`（追加）

- [ ] **Step 1: 寫失敗測試** — 追加：

```python
class TestAccessLifecycle(TransactionCase):
    def setUp(self):
        super().setUp()
        self.SA = self.env['woow_paas_platform.security_access']
        self.Route = self.env['woow_paas_platform.security_access_route']
        self.workspace = self.env['woow_paas_platform.workspace'].create(
            {'name': 'AL WS'})

    def _mock(self):
        m = MagicMock()
        m.sync_tunnel_routes.return_value = {'routes': []}
        m.sync_access_app.return_value = {'app_id': None, 'policy_ids': [],
                                          'allowed_idps': []}
        return m

    def test_cleanup_tears_down_access(self):
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            sa = self.SA.create({'name': 'AL GW',
                                 'workspace_id': self.workspace.id})
            sa.write({'state': 'active', 'tunnel_id': 'tun-al',
                      'access_enabled': True, 'access_app_id': 'app-al'})
            self.Route.create({'security_access_id': sa.id,
                               'subdomain_prefix': sa.canonical_name,
                               'is_primary': True})
            mc.sync_access_app.reset_mock()
            sa._do_cleanup()
        # cleanup 應呼叫 access teardown（enabled=False payload）
        self.assertTrue(mc.sync_access_app.called)
        payload = mc.sync_access_app.call_args.args[0]
        self.assertFalse(payload['enabled'])
        self.assertEqual(payload['existing_app_id'], 'app-al')
```

- [ ] **Step 2: 跑測試確認失敗**（sandbox；_do_cleanup 還沒接 access teardown）

- [ ] **Step 3: 實作** — `src/models/security_access.py` 的 `_cleanup_cloudflare`（P1 已存在，刪 tunnel）加 Access teardown（在刪 tunnel 之前）：

```python
        # P2: tear down Cloudflare Access (app + reusable policies) first.
        if self.access_app_id and self.tunnel_id:
            try:
                client = self._get_operator_client()
                payload = self._build_access_payload()
                payload['enabled'] = False
                client.sync_access_app(payload)
                self.access_app_id = False
            except Exception as e:
                _logger.warning("[SA] access teardown failed sa=%s: %s",
                                self.id, e)
```

注意 suspend：P1 的 `_do_suspend` 把 route 全 503 已擋住流量，Access app 可留著（resume 時自然恢復）；**P2 不在 suspend 拆 Access**（避免 resume 還要重建），只在 cleanup/disable 拆。to_dict 也要加 access 欄位（Task 9）。

- [ ] **Step 4: 跑測試確認通過**（sandbox）Expected: PASS。另跑既有 `TestSecurityAccessLifecycleHooks` 確認不回歸。

- [ ] **Step 5: Commit**

```bash
git add src/models/security_access.py src/tests/test_security_access_access_policy.py
git commit -m "feat: tear down cloudflare access on security access cleanup"
```

---

### Task 9: to_dict 加 access 欄位 + ACL（access_policy）

**Files:**
- Modify: `src/models/security_access.py`（to_dict）
- Modify: `src/security/ir.model.access.csv`、`src/security/ir_rules.xml`
- Test: `src/tests/test_security_access_access_policy.py`（追加 ACL）

- [ ] **Step 1: 寫失敗測試（ACL）** — 追加：

```python
class TestAccessPolicyAcl(TransactionCase):
    def setUp(self):
        super().setUp()
        User = self.env['res.users']
        self.alice = User.create({
            'name': 'AP Alice', 'login': 'ap_acl_alice',
            'email': 'ap_acl_alice@test.example',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])]})
        self.mallory = User.create({
            'name': 'AP Mallory', 'login': 'ap_acl_mallory',
            'email': 'ap_acl_mallory@test.example',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])]})
        self.ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'AP ACL WS', 'owner_id': self.alice.id})
        Access = self.env['woow_paas_platform.workspace_access']
        if not Access.search([('workspace_id', '=', self.ws.id),
                              ('user_id', '=', self.alice.id)]):
            Access.create({'workspace_id': self.ws.id,
                           'user_id': self.alice.id, 'role': 'owner'})
        self.sa = self.env['woow_paas_platform.security_access'].create({
            'name': 'AP ACL GW', 'workspace_id': self.ws.id})
        self.pol = self.env[
            'woow_paas_platform.security_access_access_policy'].create({
                'security_access_id': self.sa.id, 'name': 'p',
                'decision': 'allow', 'rule_type': 'email_domain',
                'rule_value': 'a.com'})

    def test_member_can_read_policy(self):
        v = self.env['woow_paas_platform.security_access_access_policy'
                     ].with_user(self.alice).search([('id', '=', self.pol.id)])
        self.assertEqual(len(v), 1)

    def test_non_member_cannot_read_policy(self):
        v = self.env['woow_paas_platform.security_access_access_policy'
                     ].with_user(self.mallory).search([('id', '=', self.pol.id)])
        self.assertEqual(len(v), 0)
```

- [ ] **Step 2: 跑測試確認失敗**（sandbox；無 ir.rule = mallory 看得到 / 無 csv = AccessError）

- [ ] **Step 3: to_dict** — `security_access.py` 的 `to_dict()` return dict 加 access 區（在 `'routes': ...` 後）：

```python
            'access_enabled': self.access_enabled,
            'session_duration': self.session_duration,
            'login_google': self.login_google,
            'login_email_otp': self.login_email_otp,
            'access_policies': [p.to_dict() for p in self.access_policy_ids],
```

- [ ] **Step 4: ACL csv** — `src/security/ir.model.access.csv` 加（鏡像 route 兩行）：

```csv
access_security_access_access_policy_user,woow_paas_platform.security_access_access_policy.user,model_woow_paas_platform_security_access_access_policy,base.group_user,1,1,1,1
access_security_access_access_policy_admin,woow_paas_platform.security_access_access_policy.admin,model_woow_paas_platform_security_access_access_policy,base.group_system,1,1,1,1
```

- [ ] **Step 5: ir.rule** — `src/security/ir_rules.xml` 的 noupdate="0" 區（`security_access_route_rule_workspace_member` 後）加：

```xml
        <!-- SecurityAccessAccessPolicy: walk parent SA's workspace. -->
        <record id="security_access_access_policy_rule_workspace_member" model="ir.rule">
            <field name="name">SecurityAccessAccessPolicy: workspace member access only</field>
            <field name="model_id" ref="model_woow_paas_platform_security_access_access_policy"/>
            <field name="domain_force">[('security_access_id.workspace_id.access_ids.user_id', '=', user.id)]</field>
            <field name="groups" eval="[(4, ref('base.group_user'))]"/>
            <field name="perm_read" eval="True"/>
            <field name="perm_write" eval="True"/>
            <field name="perm_create" eval="True"/>
            <field name="perm_unlink" eval="True"/>
        </record>
```

- [ ] **Step 6: 跑測試確認通過**（sandbox `-u`）Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/models/security_access.py src/security/ir.model.access.csv src/security/ir_rules.xml src/tests/test_security_access_access_policy.py
git commit -m "feat: expose access fields in to_dict and add access policy acl"
```

---

### Task 10: BFF — access-config + policy CRUD + route is_protected endpoints

**Files:**
- Modify: `src/controllers/security_access.py`
- Test: `src/tests/test_security_access_subscription_flow.py`（追加 controller flow）

- [ ] **Step 1: 寫失敗測試** — 追加（鏡像既有 `_patch_request` + MOCK_GET_CLIENT 雙 namespace pattern；本檔已有 MOCK_GET_CLIENT + MOCK_GET_CLIENT_MODEL）：

```python
class TestSecurityAccessAccessControl(TransactionCase):
    """access-config / policy CRUD controller helpers."""

    def setUp(self):
        super().setUp()
        self.controller = SecurityAccessController()
        self.SA = self.env['woow_paas_platform.security_access']
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'AC Ctrl WS', 'owner_id': self.env.user.id})
        Access = self.env['woow_paas_platform.workspace_access']
        if not Access.search([('workspace_id', '=', self.workspace.id),
                              ('user_id', '=', self.env.user.id)]):
            Access.create({'workspace_id': self.workspace.id,
                           'user_id': self.env.user.id, 'role': 'owner'})

    def _active_sa(self, mc):
        with patch(MOCK_GET_CLIENT, return_value=mc), \
                patch(MOCK_GET_CLIENT_MODEL, return_value=mc):
            sa = self.SA.create({'name': 'AC GW',
                                 'workspace_id': self.workspace.id})
            sa.write({'state': 'active', 'tunnel_id': 'tun-ac'})
            self.env['woow_paas_platform.security_access_route'].create({
                'security_access_id': sa.id,
                'subdomain_prefix': sa.canonical_name, 'is_primary': True})
        return sa

    def _mock(self):
        mc = MagicMock()
        mc.sync_tunnel_routes.return_value = {'routes': []}
        mc.sync_access_app.return_value = {'app_id': 'app-1',
                                           'policy_ids': ['pol-1'],
                                           'allowed_idps': ['idp-g']}
        return mc

    def test_update_access_config(self):
        mc = self._mock()
        sa = self._active_sa(mc)
        with _patch_request(self.env), \
                patch(MOCK_GET_CLIENT, return_value=mc), \
                patch(MOCK_GET_CLIENT_MODEL, return_value=mc):
            res = self.controller._update_access_config(sa, {
                'access_enabled': True, 'login_google': True,
                'login_email_otp': True, 'session_duration': '8h'})
        self.assertTrue(res['success'], res.get('error'))
        self.assertTrue(sa.access_enabled)
        self.assertEqual(sa.session_duration, '8h')

    def test_create_policy(self):
        mc = self._mock()
        sa = self._active_sa(mc)
        with _patch_request(self.env), \
                patch(MOCK_GET_CLIENT, return_value=mc), \
                patch(MOCK_GET_CLIENT_MODEL, return_value=mc):
            sa.write({'access_enabled': True})
            res = self.controller._create_policy(sa, {
                'name': 'corp', 'decision': 'allow',
                'rule_type': 'email_domain', 'rule_value': 'acme.com'})
        self.assertTrue(res['success'], res.get('error'))
        self.assertEqual(len(sa.access_policy_ids), 1)

    def test_create_policy_invalid_rule_rejected(self):
        mc = self._mock()
        sa = self._active_sa(mc)
        with _patch_request(self.env), \
                patch(MOCK_GET_CLIENT, return_value=mc), \
                patch(MOCK_GET_CLIENT_MODEL, return_value=mc):
            res = self.controller._create_policy(sa, {
                'name': 'bad', 'decision': 'allow',
                'rule_type': 'ip_range', 'rule_value': 'nope'})
        self.assertFalse(res['success'])
```

- [ ] **Step 2: 跑測試確認失敗**（sandbox；無 `_update_access_config` / `_create_policy`）

- [ ] **Step 3: 實作 endpoints + helpers** — `src/controllers/security_access.py`，在 detail endpoint 的 action dispatch 加新 action（`api_security_access_detail` 的 mutation 區，`update` 後加 `update_access`），並加 policy CRUD route + helpers：

```python
    # ==================== Access Control ====================

    @route(
        "/api/workspaces/<int:workspace_id>/security-accesses/<int:sa_id>/access-config",
        auth="user", methods=["POST"], type="json",
    )
    def api_sa_access_config(self, workspace_id, sa_id, **kwargs):
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
        try:
            sa._check_operation_allowed(operation='write')
        except Exception as e:
            return {"success": False, "error": str(e)}
        return self._update_access_config(sa, kwargs)

    @route(
        "/api/workspaces/<int:workspace_id>/security-accesses/<int:sa_id>/access-policies",
        auth="user", methods=["POST"], type="json",
    )
    def api_sa_access_policies(self, workspace_id, sa_id, action="list",
                               **kwargs):
        workspace = self._get_workspace_or_error(workspace_id)
        if not workspace:
            return {"success": False,
                    "error": "Workspace not found or access denied"}
        sa = self._get_sa_or_error(sa_id, workspace)
        if not sa:
            return {"success": False, "error": "Security access not found"}

        if action == "list":
            return {"success": True,
                    "data": [p.to_dict() for p in sa.access_policy_ids]}

        access = workspace.check_user_access(request.env.user)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {"success": False, "error": "Insufficient permissions"}
        try:
            sa._check_operation_allowed(operation='write')
        except Exception as e:
            return {"success": False, "error": str(e)}

        if action == "create":
            return self._create_policy(sa, kwargs)
        elif action == "update":
            return self._update_policy(sa, kwargs.get("policy_id"), kwargs)
        elif action == "delete":
            return self._delete_policy(sa, kwargs.get("policy_id"))
        return {"success": False, "error": f"Unknown action: {action}"}

    # ---- helpers ----

    def _update_access_config(self, sa, kwargs):
        vals = {}
        for f in ("access_enabled", "login_google", "login_email_otp"):
            if f in kwargs:
                vals[f] = bool(kwargs[f])
        if kwargs.get("session_duration"):
            vals["session_duration"] = kwargs["session_duration"]
        try:
            if vals:
                sa.write(vals)
            return {"success": True, "data": sa.to_dict()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _create_policy(self, sa, kwargs):
        if not kwargs.get("name"):
            return {"success": False, "error": "name is required"}
        # two-layer quota
        sub = sa.sudo().subscription_id
        if sub and sub.sh_subscription_plan_id:
            quota = sub.sh_subscription_plan_id.sa_max_access_policies
            if len(sa.access_policy_ids) >= quota:
                return {"success": False,
                        "error": "Access policy quota exceeded: plan allows "
                                 "%d polic(ies)" % quota}
        Policy = request.env[
            "woow_paas_platform.security_access_access_policy"]
        try:
            pol = Policy.create({
                "security_access_id": sa.id,
                "name": kwargs["name"],
                "decision": kwargs.get("decision", "allow"),
                "rule_type": kwargs.get("rule_type", "email_domain"),
                "rule_value": kwargs.get("rule_value", ""),
                "sequence": kwargs.get("sequence", 10),
            })
            return {"success": True, "data": pol.to_dict()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _update_policy(self, sa, policy_id, kwargs):
        pol = sa.access_policy_ids.filtered(lambda p: p.id == policy_id)
        if not pol:
            return {"success": False, "error": "Policy not found"}
        vals = {}
        for f in ("name", "decision", "rule_type", "rule_value", "sequence"):
            if f in kwargs:
                vals[f] = kwargs[f]
        try:
            if vals:
                pol.write(vals)
            return {"success": True, "data": pol.to_dict()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _delete_policy(self, sa, policy_id):
        pol = sa.access_policy_ids.filtered(lambda p: p.id == policy_id)
        if not pol:
            return {"success": False, "error": "Policy not found"}
        try:
            pol.unlink()
            return {"success": True, "data": {"message": "Policy deleted"}}
        except Exception as e:
            return {"success": False, "error": str(e)}
```

route `is_protected` 已可由 P1 的 `_update_route`（`api_security_access_routes` action=update）處理——確認 `_update_route` 的 vals 接受 `is_protected`（P1 plan Task 13 的 `_update_route` 已含 `is_protected`，若無則補一行 `if "is_protected" in kwargs: vals["is_protected"] = bool(kwargs["is_protected"])`）。

- [ ] **Step 4: 跑測試確認通過**（sandbox）Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/controllers/security_access.py src/tests/test_security_access_subscription_flow.py
git commit -m "feat: add access config and policy crud bff endpoints"
```

---

### Task 11: HttpCase cross-user negative（access endpoints）

**Files:**
- Modify: `src/tests/test_security_access_endpoints.py`（追加）

- [ ] **Step 1: 寫測試** — 追加（鏡像既有 `_rpc` + `_new_user`）：

```python
@tagged('post_install', '-at_install')
class TestSecurityAccessAccessEndpoints(HttpCase):
    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:6]
        self.alice = _new_user(self.env, f'sa_ac_alice_{suffix}')
        self.mallory = _new_user(self.env, f'sa_ac_mallory_{suffix}')
        self.workspace = self.env['woow_paas_platform.workspace'].sudo().create({
            'name': f'SA AC WS {suffix}', 'owner_id': self.alice.id})
        Access = self.env['woow_paas_platform.workspace_access'].sudo()
        if not Access.search([('workspace_id', '=', self.workspace.id),
                              ('user_id', '=', self.alice.id)]):
            Access.create({'workspace_id': self.workspace.id,
                           'user_id': self.alice.id, 'role': 'owner'})
        self.sa = self.env['woow_paas_platform.security_access'].sudo().create({
            'name': 'AC EP GW', 'workspace_id': self.workspace.id})
        self.pol = self.env[
            'woow_paas_platform.security_access_access_policy'].sudo().create({
                'security_access_id': self.sa.id, 'name': 'p',
                'decision': 'allow', 'rule_type': 'email_domain',
                'rule_value': 'a.com'})

    def _rpc(self, url, params=None):
        r = self.url_open(url, data=json.dumps({'jsonrpc': '2.0',
                          'method': 'call', 'params': params or {}}),
                          headers={'Content-Type': 'application/json'})
        return r.json().get('result') or {}

    def test_non_member_policy_list_denied(self):
        self.authenticate(self.mallory.login, self.mallory.login)
        res = self._rpc(
            f'/api/workspaces/{self.workspace.id}'
            f'/security-accesses/{self.sa.id}/access-policies',
            {'action': 'list'})
        self.assertFalse(res.get('success'))

    def test_non_member_policy_create_denied_and_none_made(self):
        before = self.env[
            'woow_paas_platform.security_access_access_policy'].sudo(
            ).search_count([('security_access_id', '=', self.sa.id)])
        self.authenticate(self.mallory.login, self.mallory.login)
        res = self._rpc(
            f'/api/workspaces/{self.workspace.id}'
            f'/security-accesses/{self.sa.id}/access-policies',
            {'action': 'create', 'name': 'inject', 'decision': 'allow',
             'rule_type': 'email_domain', 'rule_value': 'evil.com'})
        self.assertFalse(res.get('success'))
        after = self.env[
            'woow_paas_platform.security_access_access_policy'].sudo(
            ).search_count([('security_access_id', '=', self.sa.id)])
        self.assertEqual(after, before)

    def test_non_member_access_config_denied(self):
        self.authenticate(self.mallory.login, self.mallory.login)
        res = self._rpc(
            f'/api/workspaces/{self.workspace.id}'
            f'/security-accesses/{self.sa.id}/access-config',
            {'access_enabled': True})
        self.assertFalse(res.get('success'))
        self.assertFalse(self.sa.sudo().access_enabled)
```

`json` / `uuid` / `HttpCase` / `tagged` / `_new_user` 在該檔 P1 已 import。

- [ ] **Step 2: 跑測試**（sandbox）Expected: PASS（controller + ir.rule 已落地）

- [ ] **Step 3: Commit**

```bash
git add src/tests/test_security_access_endpoints.py
git commit -m "test: add cross-user negatives for access control endpoints"
```

---

### Task 12: 前端 — service client + Access tab state/handlers

**Files:**
- Modify: `src/static/src/paas/services/security_access.js`
- Modify: `src/static/src/paas/pages/security-access/SecurityAccessDetailPage.js`

- [ ] **Step 1: service client 方法** — `security_access.js` 加（鏡像既有 `updateRoute` 的 envelope）：

```javascript
  async updateAccessConfig(workspaceId, saId, vals) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}/access-config`,
        vals);
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) { return { success: false, error: err.message }; }
  },

  async createPolicy(workspaceId, saId, vals) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}/access-policies`,
        { action: "create", ...vals });
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) { return { success: false, error: err.message }; }
  },

  async updatePolicy(workspaceId, saId, policyId, vals) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}/access-policies`,
        { action: "update", policy_id: policyId, ...vals });
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) { return { success: false, error: err.message }; }
  },

  async deletePolicy(workspaceId, saId, policyId) {
    try {
      const result = await jsonRpc(
        `/api/workspaces/${workspaceId}/security-accesses/${saId}/access-policies`,
        { action: "delete", policy_id: policyId });
      return result.success
        ? { success: true, data: result.data }
        : { success: false, error: result.error };
    } catch (err) { return { success: false, error: err.message }; }
  },
```

- [ ] **Step 2: DetailPage state + handlers** — `SecurityAccessDetailPage.js`：

(a) `TABS` 的 access 項 `comingSoon: true` 改 `false`：

```javascript
  { key: "access", label: "Access", icon: "shield" },
```

(b) import AddPolicyModal（Task 13 會建）+ 註冊 component：

```javascript
import { AddPolicyModal } from "../../components/modal/AddPolicyModal";
// static components = { ..., AddPolicyModal };
```

(c) state 加（在 useState 物件內）：

```javascript
      showPolicyModal: false,
      editingPolicy: null,
```

（`access_enabled` / `login_*` / `session_duration` / `access_policies` 直接讀 `state.sa.*`，loadDetail 已帶回。）

(d) handlers（加在 class 內）：

```javascript
  async saveAccessConfig(vals) {
    const res = await securityAccessService.updateAccessConfig(
      this.props.workspaceId, this.props.saId, vals);
    if (res.success) { this.state.sa = res.data; }
    else { this.state.error = res.error; }
  }

  onMasterToggle(ev) {
    this.saveAccessConfig({ access_enabled: ev.target.checked });
  }
  onGoogleToggle(ev) {
    this.saveAccessConfig({ login_google: ev.target.checked });
  }
  onEmailOtpToggle(ev) {
    this.saveAccessConfig({ login_email_otp: ev.target.checked });
  }
  onSessionDurationChange(ev) {
    this.saveAccessConfig({ session_duration: ev.target.value });
  }

  openAddPolicy() {
    this.state.editingPolicy = null;
    this.state.showPolicyModal = true;
  }
  openEditPolicy(policy) {
    this.state.editingPolicy = policy;
    this.state.showPolicyModal = true;
  }
  async onPolicySaved() {
    this.state.showPolicyModal = false;
    await this.loadDetail();
  }
  async deletePolicy(policy) {
    const res = await securityAccessService.deletePolicy(
      this.props.workspaceId, this.props.saId, policy.id);
    if (res.success) { await this.loadDetail(); }
    else { this.state.error = res.error; }
  }

  async toggleRouteProtected(route, ev) {
    const res = await securityAccessService.updateRoute(
      this.props.workspaceId, this.props.saId, route.id,
      { is_protected: ev.target.checked });
    if (res.success) { await this.loadDetail(); }
    else { this.state.error = res.error; }
  }
```

- [ ] **Step 3: 驗證**（`node --check` 若可用，否則肉眼對齊既有 handler）

- [ ] **Step 4: Commit**

```bash
git add src/static/src/paas/services/security_access.js src/static/src/paas/pages/security-access/SecurityAccessDetailPage.js
git commit -m "feat: add access tab service methods and handlers"
```

---

### Task 13: 前端 — Access tab XML/SCSS + AddPolicyModal + Application is_protected

**Files:**
- Modify: `SecurityAccessDetailPage.xml`、`SecurityAccessDetailPage.scss`
- Create: `components/modal/AddPolicyModal.js` + `.xml`

- [ ] **Step 1: Access tab XML** — `SecurityAccessDetailPage.xml`，把 Coming Soon 的 `t-if` 條件由 `access or email or domains` 改成只 `email or domains`，並在其前插入 access tab（依 mockup：master toggle 卡 + Login Methods 卡 + Access Policies 卡 + session duration）：

```xml
        <!-- Access (P2) -->
        <div t-if="state.activeTab === 'access'" class="o_woow_sa_detail__body">
          <WoowCard shadow="true">
            <div class="o_woow_sa_access__master">
              <div>
                <h3>Cloudflare Access protection</h3>
                <p>Visitors to <t t-esc="state.sa.primary_hostname"/> must
                   authenticate before protected routes load.</p>
              </div>
              <label class="o_woow_checkbox_group">
                <input type="checkbox" class="o_woow_checkbox"
                       t-att-checked="state.sa.access_enabled"
                       t-on-change="onMasterToggle"/>
              </label>
            </div>
          </WoowCard>

          <WoowCard shadow="true" t-if="state.sa.access_enabled">
            <h3>Login Methods</h3>
            <p>How visitors prove who they are (managed at account level).</p>
            <div class="o_woow_checkbox_group">
              <input type="checkbox" class="o_woow_checkbox" id="li_google"
                     t-att-checked="state.sa.login_google"
                     t-on-change="onGoogleToggle"/>
              <label for="li_google" class="o_woow_checkbox_label">Google</label>
            </div>
            <div class="o_woow_checkbox_group">
              <input type="checkbox" class="o_woow_checkbox" id="li_otp"
                     t-att-checked="state.sa.login_email_otp"
                     t-on-change="onEmailOtpToggle"/>
              <label for="li_otp" class="o_woow_checkbox_label">Email OTP</label>
            </div>
          </WoowCard>

          <WoowCard shadow="true" t-if="state.sa.access_enabled">
            <div class="o_woow_sa_access__policy_header">
              <h3>Access Policies</h3>
              <WoowButton variant="'primary'" size="'sm'"
                          onClick.bind="openAddPolicy">+ Add Policy</WoowButton>
            </div>
            <p>Rules are evaluated top to bottom. Block rules win over allow.</p>
            <table class="o_woow_sa_detail__routes">
              <tbody>
                <t t-foreach="state.sa.access_policies" t-as="pol" t-key="pol.id">
                  <tr>
                    <td>
                      <t t-esc="pol.name"/>
                      <span t-att-class="'o_woow_badge ' + (pol.decision === 'deny' ? 'o_woow_badge_gray' : 'o_woow_badge_green')">
                        <t t-esc="pol.decision === 'deny' ? 'Block' : 'Allow'"/>
                      </span>
                    </td>
                    <td><t t-esc="pol.rule_type"/>: <t t-esc="pol.rule_value"/></td>
                    <td>
                      <WoowButton variant="'secondary'" size="'sm'"
                                  onClick="() => this.openEditPolicy(pol)">Edit</WoowButton>
                      <WoowButton variant="'danger'" size="'sm'"
                                  onClick="() => this.deletePolicy(pol)">
                        <WoowIcon name="'delete'" size="14"/>
                      </WoowButton>
                    </td>
                  </tr>
                </t>
              </tbody>
            </table>
          </WoowCard>

          <WoowCard shadow="true" t-if="state.sa.access_enabled">
            <h3>Default session duration</h3>
            <p>How long users stay authenticated before re-login.</p>
            <select class="o_woow_input" t-on-change="onSessionDurationChange">
              <option value="30m" t-att-selected="state.sa.session_duration === '30m'">30 minutes</option>
              <option value="1h" t-att-selected="state.sa.session_duration === '1h'">1 hour</option>
              <option value="8h" t-att-selected="state.sa.session_duration === '8h'">8 hours</option>
              <option value="24h" t-att-selected="state.sa.session_duration === '24h'">24 hours</option>
              <option value="168h" t-att-selected="state.sa.session_duration === '168h'">7 days</option>
            </select>
          </WoowCard>
        </div>
```

並在檔末 modal 區（SkipTrialModal 旁）加：

```xml
        <AddPolicyModal t-if="state.showPolicyModal"
            workspaceId="props.workspaceId" saId="props.saId"
            policy="state.editingPolicy"
            onClose="() => state.showPolicyModal = false"
            onSaved.bind="onPolicySaved"/>
```

Application tab 的 route 表 `<thead>` 在 `Host header` 前加 `<th>Protected</th>`，`<tbody>` 對應加一格 checkbox：

```xml
                    <td>
                      <input type="checkbox" class="o_woow_checkbox"
                             t-att-checked="route.is_protected"
                             t-on-change="(ev) => this.toggleRouteProtected(route, ev)"/>
                    </td>
```

- [ ] **Step 2: AddPolicyModal** — 建 `components/modal/AddPolicyModal.js`（鏡像 CreateSecurityAccessModal 結構）：

```javascript
/** @odoo-module **/

import { Component, useState } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { securityAccessService } from "../../services/security_access";

export class AddPolicyModal extends Component {
  static template = "woow_paas_platform.AddPolicyModal";
  static components = { WoowButton, WoowIcon };
  static props = {
    workspaceId: { type: Number },
    saId: { type: Number },
    policy: { type: [Object, { value: null }], optional: true },
    onClose: { type: Function },
    onSaved: { type: Function },
  };

  setup() {
    const p = this.props.policy;
    this.state = useState({
      name: p ? p.name : "",
      decision: p ? p.decision : "allow",
      rule_type: p ? p.rule_type : "email_domain",
      rule_value: p ? p.rule_value : "",
      saving: false,
      error: null,
    });
  }

  get isEdit() { return !!this.props.policy; }

  async confirm() {
    if (!this.state.name.trim() || !this.state.rule_value.trim()) {
      this.state.error = "Name and rule value are required";
      return;
    }
    this.state.saving = true;
    const vals = {
      name: this.state.name.trim(), decision: this.state.decision,
      rule_type: this.state.rule_type, rule_value: this.state.rule_value.trim(),
    };
    const res = this.isEdit
      ? await securityAccessService.updatePolicy(
          this.props.workspaceId, this.props.saId, this.props.policy.id, vals)
      : await securityAccessService.createPolicy(
          this.props.workspaceId, this.props.saId, vals);
    this.state.saving = false;
    if (res.success) { this.props.onSaved(); }
    else { this.state.error = res.error; }
  }
}
```

建 `AddPolicyModal.xml`（鏡像 CreateSecurityAccessModal.xml 的 `o_woow_modal_*` 結構）：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<templates xml:space="preserve">
  <t t-name="woow_paas_platform.AddPolicyModal">
    <div class="o_woow_modal_backdrop" t-on-click="props.onClose">
      <div class="o_woow_modal" t-on-click.stop="">
        <div class="o_woow_modal_header">
          <h2><t t-esc="isEdit ? 'Edit Access Policy' : 'Add Access Policy'"/></h2>
          <button class="o_woow_modal_close" t-on-click="props.onClose">
            <WoowIcon name="'close'" size="20"/>
          </button>
        </div>
        <div class="o_woow_modal_body">
          <div t-if="state.error" class="o_woow_error_message">
            <WoowIcon name="'error'" size="16"/><span t-esc="state.error"/>
          </div>
          <div class="o_woow_form_group">
            <label>Name</label>
            <input type="text" class="o_woow_input" t-att-value="state.name"
                   t-on-input="(ev) => state.name = ev.target.value"
                   placeholder="e.g. Internal staff"/>
          </div>
          <div class="o_woow_form_group">
            <label>Decision</label>
            <select class="o_woow_input"
                    t-on-change="(ev) => state.decision = ev.target.value">
              <option value="allow" t-att-selected="state.decision === 'allow'">Allow</option>
              <option value="deny" t-att-selected="state.decision === 'deny'">Block</option>
            </select>
          </div>
          <div class="o_woow_form_group">
            <label>Rule type</label>
            <select class="o_woow_input"
                    t-on-change="(ev) => state.rule_type = ev.target.value">
              <option value="email_domain" t-att-selected="state.rule_type === 'email_domain'">Email domain</option>
              <option value="email_list" t-att-selected="state.rule_type === 'email_list'">Email list</option>
              <option value="ip_range" t-att-selected="state.rule_type === 'ip_range'">IP range (CIDR)</option>
            </select>
          </div>
          <div class="o_woow_form_group">
            <label>Rule value</label>
            <input type="text" class="o_woow_input" t-att-value="state.rule_value"
                   t-on-input="(ev) => state.rule_value = ev.target.value"
                   placeholder="acme.com / a@x.com,b@x.com / 203.0.113.0/24"/>
          </div>
        </div>
        <div class="o_woow_modal_footer">
          <WoowButton variant="'secondary'" onClick="props.onClose">Cancel</WoowButton>
          <WoowButton variant="'primary'" onClick.bind="confirm"
                      disabled="state.saving">
            <t t-if="state.saving">Saving...</t>
            <t t-else=""><t t-esc="isEdit ? 'Save' : 'Add Policy'"/></t>
          </WoowButton>
        </div>
      </div>
    </div>
  </t>
</templates>
```

- [ ] **Step 3: SCSS** — `SecurityAccessDetailPage.scss` 加（`.o_woow_sa_detail` 內或新 block，沿用既有變數；`o_woow_badge_ok`/`o_woow_badge_error` 若不存在用既有 badge 類，先 grep 確認）：

```scss
.o_woow_sa_access {
  &__master {
    display: flex; align-items: center; justify-content: space-between; gap: 1rem;
  }
  &__policy_header {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 0.5rem;
  }
}
```

- [ ] **Step 4: 手動驗證（sandbox headed）**

更新模組後瀏覽器：Access tab 出現（非 Coming Soon）→ master toggle 開 → Login Methods + Policies + session duration 卡出現 → Add Policy modal → 建 policy 顯示在列表 → Application tab 每列有 Protected checkbox。**OWL 陷阱檢查**：本 task 的 `t-att-checked` / `t-att-selected` / `t-att-value` 用在原生 DOM element（非 component）合法；`onClick.bind` / `() => this.x()` 都是既有 pattern。

- [ ] **Step 5: Commit**

```bash
git add src/static/src/paas/pages/security-access/ src/static/src/paas/components/modal/AddPolicyModal.*
git commit -m "feat: implement access tab ui with policy crud and route protection"
```

---

### Task 14: 收尾 — 全套回歸 + spec 狀態

- [ ] **Step 1: Odoo 全模組測試**

Run: `./.claude/skills/k8s-dev-sandbox/scripts/test.sh feat-security-access --module woow_paas_platform`
Expected: 0 failed 0 error（含 P1 全部 + P2 新增；既有 SA/smart home/cloud service 不回歸）

- [ ] **Step 2: Operator 全 unit suite**

Run: `cd extra/paas-operator && /tmp/sa-op-venv/bin/python -m pytest tests/ -m "not integration"`
Expected: 全 PASS

- [ ] **Step 3: 更新 spec 狀態** — `docs/superpowers/specs/2026-06-06-security-access-subscription-design.md` 頂部狀態行改：

```
> 狀態：P1 + P2(Access) implemented；P3 Domains / P4 Email 待開。
```

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-06-06-security-access-subscription-design.md
git commit -m "docs: mark security access p2 access control implemented"
```

P2 完成後（merge 前）對抗式審查重點：
1. ACL：access_policy 的 ir.rule + 每個 policy/access-config endpoint 的 HttpCase cross-user negative（本 plan Task 11 已含；審查補齊 update/delete 變體）
2. silent-failure：operator Access 錯誤路徑（IdP 403 要 fail-loud 不靜默）、`_resolve_idp_ids` 缺 IdP 的 warning-skip 是否會讓 login method 靜默失效（需明確回報給 caller）
3. correctness：`sync_access_app` 的 delete-old-then-create policy 在 partial-failure 下的孤兒 policy 風險（對照 P1 的 two-stage provision 冪等教訓 [[two-stage-remote-provision-idempotency]]）
4. mock 雙 namespace（[[unittest-mock-dual-namespace-patch]]）+ su-env guard（[[su-env-exempt-guard-false-green]]）家族掃描

---

## Self-Review 紀錄

1. **Spec coverage**：
   - §1 access_policy 子模型 + 主模型 access 欄位 → Task 6
   - §1 quota sa_max_access_policies enforce → Task 6（constraint）+ Task 10（controller）
   - §1 對稱 ir.rule → Task 9
   - §2 operator Access endpoints（apps + policies + idp）→ Task 1-4
   - §4 Access tab（master toggle / login methods / policies CRUD / session duration）→ Task 12-13
   - 逐 route is_protected 接線 → Task 7（model sync）+ Task 10（BFF）+ Task 13（UI）
   - 503 凍結保留 Access（不拆）/ cleanup 拆 Access → Task 8
   - HttpCase cross-user negative → Task 11
2. **Placeholder scan**：無 TBD/TODO；「讀檔對齊」處皆環境相依細節（既有 class 名 / write 結構），附明確指示。
3. **型別一致**：`sync_access_app`（operator method / client method / endpoint payload）/ `_sync_access_to_operator` / `_build_access_payload` / `access_policy` model 名 / `login_idp_types`（`google` / `onetimepin`）/ decision（`allow` / `deny`）跨 task 一致。

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-07-security-access-p2-access-control.md`. Two execution options:

**1. Subagent-Driven (recommended)** — 每 task fresh subagent + 兩段式審查
**2. Inline Execution** — 本 session 批次 + checkpoint

Which approach?

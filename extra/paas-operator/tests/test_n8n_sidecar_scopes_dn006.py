"""DATA-N8N-006-1：mcp-sidecar 的 n8n API key 只授與 MCP 工具用得到的 scope。

商用驗收（n8n 250）：sidecar key 帶 GET /rest/api-keys/scopes 的全部 88 個 scope，含
user:create／delete／changeRole、role:manage、ldap／saml／oidc:manage、securitySettings:manage、
communityPackage:install、sourceControl:pull、eventBusDestination:*——sidecar 被攻破就等於 n8n
實例管理權。

修法是 allowlist（見 ``n8n_init._SIDECAR_SCOPE_PREFIXES``），REST 路徑（首次開通）與 exec 腳本路徑
（owner 已存在，#1534）兩處都套，兩份清單用本檔對帳。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from src.services import n8n_init
from src.services.n8n_init import sidecar_scopes
from tests.test_init_error_kind_1263 import _http, _n8n_k8s, _post_init_n8n

_JS = Path(n8n_init.__file__).parent / "n8n_scripts" / "ensure_api_key.js"

#: 商用驗收實測的 sidecar 全權限清單（evidence DATA-N8N-006/raw/s3-api-keys-metadata.json，88 個）。
SCOPES_88 = (
    "communityPackage:install communityPackage:list communityPackage:uninstall communityPackage:update "
    "credential:create credential:delete credential:list credential:move credential:read credential:update "
    "dataTable:create dataTable:delete dataTable:list dataTable:read dataTable:update "
    "eventBusDestination:create eventBusDestination:delete eventBusDestination:list "
    "eventBusDestination:read eventBusDestination:test eventBusDestination:update "
    "folder:create folder:delete folder:list folder:read folder:update insights:read "
    "ldap:manage ldap:sync oidc:manage otel:manage project:create project:delete project:export "
    "project:list project:update role:manage saml:manage securityAudit:generate securitySettings:manage "
    "sourceControl:pull tag:create tag:delete tag:list tag:read tag:update "
    "user:changeRole user:create user:delete user:list user:read "
    "variable:create variable:delete variable:list variable:update "
    "workflow:create workflow:delete workflow:export workflow:import workflow:list workflow:move "
    "workflow:read workflow:update workflowTags:update workflowTags:list executionTags:update "
    "executionTags:list workflow:activate workflow:deactivate execution:delete execution:read "
    "execution:retry execution:stop execution:list testRun:read testRun:list testRun:create "
    "testRun:cancel dataTableRow:create dataTableRow:read dataTableRow:update dataTableRow:delete "
    "dataTableRow:upsert dataTableColumn:create dataTableColumn:read dataTableColumn:update "
    "dataTableColumn:delete project:manageMembers"
).split()

#: 實例管理類，無論如何不能授與 sidecar。
ADMIN_SCOPES = [
    "user:create", "user:delete", "user:changeRole", "user:list", "user:read", "role:manage",
    "ldap:manage", "ldap:sync", "saml:manage", "oidc:manage", "otel:manage",
    "securitySettings:manage", "communityPackage:install", "communityPackage:uninstall",
    "communityPackage:update", "communityPackage:list", "sourceControl:pull",
    "eventBusDestination:create", "eventBusDestination:delete", "eventBusDestination:update",
    "eventBusDestination:test", "project:create", "project:delete", "project:update",
    "project:export", "project:manageMembers", "credentialTypePolicy:manage",
]

#: MCP 28 支工具實際會用到的類別（workflow、execution、credential、datatable、folder、audit…）。
NEEDED_SCOPES = [
    "workflow:list", "workflow:read", "workflow:create", "workflow:update", "workflow:delete",
    "workflow:activate", "workflow:deactivate", "execution:list", "execution:read",
    "execution:retry", "credential:create", "credential:read", "credential:delete",
    "dataTable:list", "dataTableRow:create", "dataTableColumn:read", "folder:list",
    "tag:list", "workflowTags:update", "testRun:create", "securityAudit:generate",
    "insights:read", "variable:list", "project:list",
]


def test_88_scope_fixture_is_the_audited_set():
    assert len(SCOPES_88) == 88 and len(set(SCOPES_88)) == 88


def test_admin_scopes_are_never_granted():
    granted = set(sidecar_scopes(SCOPES_88 + ["credentialTypePolicy:manage"]))
    assert not granted & set(ADMIN_SCOPES), sorted(granted & set(ADMIN_SCOPES))


def test_tools_still_get_the_scopes_they_need():
    granted = set(sidecar_scopes(SCOPES_88 + NEEDED_SCOPES))
    assert set(NEEDED_SCOPES) <= granted, sorted(set(NEEDED_SCOPES) - granted)


def test_allowlist_not_denylist_new_scopes_are_not_granted_by_default():
    """n8n 將來新增的管理類 scope 預設不授與，需要時再明確加進 allowlist。"""
    assert sidecar_scopes(["workflow:read", "tenantAdmin:manage", "billing:read"]) == ["workflow:read"]


def test_order_is_kept_and_duplicates_dropped_and_junk_ignored():
    assert sidecar_scopes(["tag:list", "workflow:read", "tag:list", None, 5]) == ["tag:list", "workflow:read"]
    assert sidecar_scopes(None) == [] and sidecar_scopes([]) == []


def test_audited_88_shrinks_to_the_policy_subset():
    granted = sidecar_scopes(SCOPES_88)
    assert 0 < len(granted) <= len(SCOPES_88) - 25  # 實際：88 → 60，拿掉 28 個管理類
    assert not any(s.startswith(("user:", "role:", "ldap:", "saml:", "oidc:", "otel:",
                                 "communityPackage:", "sourceControl:", "eventBusDestination:",
                                 "securitySettings:")) for s in granted)


# ---- REST 路徑：首次開通真的只送過濾後的 scope --------------------------------


def _posted_key_body(k8s):
    for call in k8s.exec_in_pod.call_args_list:
        script = call.kwargs.get("command", call.args[3] if len(call.args) > 3 else [""])[-1]
        if 'method:"POST"' in script and 'path:"/rest/api-keys"' in script:
            literal = re.search(r"const d=(\".*?\");const req", script, re.S).group(1)
            return json.loads(json.loads(literal))
    raise AssertionError("no POST /rest/api-keys was made")


def test_rest_path_sends_only_the_allowed_scopes():
    k8s = _n8n_k8s({("GET", "/rest/api-keys/scopes"): _http(200, json.dumps({"data": SCOPES_88}))})
    body = _post_init_n8n(k8s).json()

    assert body["success"] is True
    posted = _posted_key_body(k8s)
    assert posted["label"] == "mcp-sidecar"
    assert posted["scopes"] == sidecar_scopes(SCOPES_88)
    assert not set(posted["scopes"]) & set(ADMIN_SCOPES)


def test_rest_path_fails_clearly_when_no_scope_is_usable():
    k8s = _n8n_k8s({("GET", "/rest/api-keys/scopes"): _http(200, json.dumps({"data": ["tenantAdmin:manage"]}))})
    resp = _post_init_n8n(k8s)
    body = resp.json()

    assert body["success"] is False
    assert body.get("error_step") == "create_api_key" or "usable" in json.dumps(body)


# ---- exec 腳本路徑（ensure_api_key.js）與 Python 清單對帳 -----------------------


def _js_policy_source() -> str:
    text = _JS.read_text(encoding="utf-8")
    start = text.index("// ---- sidecar scope policy")
    end = text.index("// ---- end sidecar scope policy ----")
    return text[start:end]


def test_js_policy_lists_equal_the_python_lists():
    src = _js_policy_source()
    prefixes = re.search(r"SIDECAR_SCOPE_PREFIXES = \[(.*?)\];", src, re.S).group(1)
    exact = re.search(r"SIDECAR_SCOPE_EXACT = \[(.*?)\];", src, re.S).group(1)
    assert tuple(re.findall(r'"([^"]+)"', prefixes)) == n8n_init._SIDECAR_SCOPE_PREFIXES
    assert frozenset(re.findall(r'"([^"]+)"', exact)) == n8n_init._SIDECAR_SCOPE_EXACT


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_filter_gives_the_same_answer_as_python_on_the_audited_set():
    program = (
        _js_policy_source()
        + "\nconst input = JSON.parse(process.argv[1]);"
        + "\nconsole.log(JSON.stringify({kept: sidecarScopes(input),"
        + " fullKeyOk: hasOnlySidecarScopes(JSON.stringify(input)),"
        + " minimalKeyOk: hasOnlySidecarScopes(JSON.stringify(sidecarScopes(input))),"
        + " emptyOk: hasOnlySidecarScopes('[]'), junkOk: hasOnlySidecarScopes('not json')}));"
    )
    out = subprocess.run(["node", "-e", program, json.dumps(SCOPES_88)],
                         capture_output=True, text=True, check=True, timeout=30)
    result = json.loads(out.stdout)
    assert result["kept"] == sidecar_scopes(SCOPES_88)
    # 沿用判斷：舊的全權限 key 不沿用（Redeploy 時換成最小權限），最小權限 key 可沿用
    assert result["fullKeyOk"] is False
    assert result["minimalKeyOk"] is True
    assert result["emptyOk"] is False and result["junkOk"] is False


def test_js_script_mentions_the_policy_in_both_mint_and_reuse_paths():
    text = _JS.read_text(encoding="utf-8")
    assert "sidecarScopes(req(\"@n8n/permissions\").OWNER_API_KEY_SCOPES)" in text
    assert "hasOnlySidecarScopes(row.scopes)" in text
    assert 'SELECT "apiKey", "scopes" FROM' in text


def test_js_unwraps_the_gcm_wrapped_signing_secret():
    """n8n 2.43.x 起 deployment_key 的簽章密鑰以 AES-256-GCM 包起來；舊碼拿包過的字串當 HMAC 密鑰
    簽出的 key 會被 n8n 以 500 拒絕（lab：2.43.6 的 ensure_api_key 一律 key_unverified）。"""
    text = _JS.read_text(encoding="utf-8")
    assert 'algorithm === "aes-256-gcm"' in text
    assert "decryptDEKWithInstanceKey" in text
    assert 'requireFromN8n(req, "n8n-core")' in text

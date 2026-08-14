"""TDD red phase — minimal SSRF URL guard on MCP create / update endpoint.

Plan: docs/plans/2026-Q2/2026-05-20-mcp-refactor-url-redesign-plan.md
  - AC #30: ``_create_project_mcp_server`` / ``_update_project_mcp_server``
    must validate user-provided ``url`` payload:
      * reject 169.254.169.254 (AWS / GCP / Azure cloud metadata IPv4)
      * reject IPv6 link-local (``fe80::/10``) + IMDSv2 (``fd00:ec2::254``)
      * accept scheme ``http://`` and ``https://`` only
      * allow cluster.local hostnames (sidecar-backed MCP path lives there)
      * internal-network filter (RFC1918 etc.) — left to #52 follow-up
  - AC #31: rejection wording ``{success: False, error: 'URL host not
    allowed: <host>'}`` so frontend can show actionable error.
  - AC #32: pure stdlib (``urllib.parse`` + ``ipaddress``) — no extra
    dep.

Design context (OQ-4 leader-resolved minimal SSRF):
  - User-scope MCP 透過 endpoint ``action=create`` 可以填任意 URL（B 案
    OQ-1 後語意鬆綁 — bound or unbound project 都可填）
  - 沒 SSRF check = AI chat backend / sync 路徑會被誘發呼叫 metadata
    endpoint（IMDSv1 169.254.169.254）取 instance role credentials → 攻擊面
  - 完整 SSRF 防禦留 #52；本 PR 只擋最常見的 cloud metadata + scheme
    abuse + link-local

Red expectation today (pre-implementation, 2026-05-20):

* All reject tests — RED today：``_create_project_mcp_server`` (paas.py:1601)
  has no URL validation; the endpoint accepts metadata-IP URL → row gets
  created → ``assertFalse(result.success)`` fails.
* ``cluster_local`` + ``external_https`` accept tests — GREEN today
  (endpoint accepts any URL). Stay GREEN after implementation (positive
  path). Marked as "boundary lock" — guards against an over-eager
  implementation that bans cluster.local along with metadata IPs.
* ``unsupported_scheme`` ``ftp://`` / ``file://`` — RED today (accepted).

Boundary（刻意不測）:
* 不測 RFC1918 (10.0.0.0/8 / 192.168.0.0/16 / 172.16.0.0/12) — cluster.local
  resolves into pod-network IPs (often 10.x or fd00::), blanket ban would
  break sidecar-backed MCP. Internal-network refinement → #52.
* 不測 0.0.0.0 / 127.0.0.1 — pre-existing decision not in plan AC list.
* 不測 DNS rebinding — out of scope for minimal SSRF.
"""
import json as json_lib
import uuid

from odoo.tests.common import HttpCase, tagged
from ..models.cloud_service import _internal_provision


def _new_user(env, login, name=None):
    return env['res.users'].sudo().create({
        'name': name or login,
        'login': login,
        'password': login,
        'email': f'{login}@test.example',
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })


@tagged('post_install', '-at_install')
class TestSsrfCheckOnMcpUrlCreate(HttpCase):
    """F group — Plan AC #30, #31, #32."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Workspace = cls.env['woow_paas_platform.workspace']
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']
        cls.Service = cls.env['woow_paas_platform.cloud_service']
        cls.Project = cls.env['project.project']
        cls.McpServer = cls.env['woow_paas_platform.mcp_server']

    def setUp(self):
        super().setUp()
        # skill-binding-ux：portal settings flag 預設 fail-closed off — 本
        # class 打 MCP write endpoint，需開 flag 保留正向行為守線（flag-off
        # denied 守線在 test_portal_settings_flag.py）。
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.portal_project_settings_editable', 'True')
        self.alice = _new_user(self.env, 'ssrf_alice', 'Alice (SSRF)')
        self.ws = self.Workspace.sudo().create({
            'name': 'SSRF WS', 'owner_id': self.alice.id,
        })
        self.template = self.Template.sudo().create({
            'name': 'SSRF Tmpl',
            'slug': f'ssrf-tpl-{uuid.uuid4().hex[:8]}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'ssrf-tpl',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        self.service = _internal_provision(self.Service.sudo()).create({
            'name': 'SSRF Svc',
            'workspace_id': self.ws.id,
            'template_id': self.template.id,
            'helm_release_name': 'ssrf-svc',
            'helm_namespace': 'paas-ws-ssrf',
        })
        self.project = self.Project.sudo().create({
            'name': 'SSRF Project',
            'cloud_service_id': self.service.id,
        })

    # ------------------------------------------------------------------ #
    # JSON-RPC helper                                                    #
    # ------------------------------------------------------------------ #
    def _post(self, url, params):
        response = self.url_open(
            url,
            data=json_lib.dumps({
                'jsonrpc': '2.0', 'method': 'call', 'params': params,
            }),
            headers={'Content-Type': 'application/json'},
            timeout=30,
        )
        self.assertEqual(
            response.status_code, 200,
            (
                f"Endpoint {url!r} did not respond with HTTP 200 "
                f"(status={response.status_code}). "
                f"Body excerpt: {response.text[:200]!r}"
            ),
        )
        body = response.json()
        self.assertIn('result', body, f"Body: {body!r}")
        return body['result']

    def _url(self):
        return f'/api/support-projects/{self.project.id}/mcp-servers'

    def _create_payload(self, name, url, **extra):
        payload = {
            'action': 'create',
            'name': name,
            'url': url,
            'transport': 'sse',
        }
        payload.update(extra)
        return payload

    def _assert_rejected(self, result, target_host, ac_label):
        """Helper — assert reject shape + error-message contract.

        Per AC #31: ``{success: False, error: 'URL host not allowed:
        <host>'}``. Substring assertion on ``'URL host not allowed'``
        keeps the test resilient to future message tweaks while still
        guarding the contract surface.
        """
        self.assertFalse(
            result.get('success'),
            f"{ac_label}: URL was accepted but must be rejected. "
            f"Result: {result!r}.",
        )
        err = (result.get('error') or '').lower()
        self.assertIn(
            'url host not allowed',
            err,
            f"{ac_label}: rejection error must contain "
            f"'URL host not allowed' (per plan AC #31 contract). "
            f"Got: {result.get('error')!r}.",
        )
        # Loosely confirm the offending host is mentioned (best-effort,
        # the exact format depends on impl — could be IPv6 brackets etc.).
        self.assertIn(
            target_host.split(':')[0].lstrip('[').lower(),
            err,
            f"{ac_label}: error should name the offending host "
            f"({target_host!r}). Got: {result.get('error')!r}.",
        )

    # ------------------------------------------------------------------ #
    # AC #30 / #31: reject — IPv4 metadata                                #
    # ------------------------------------------------------------------ #
    def test_url_rejects_aws_metadata_ipv4(self):
        """AC #30: reject ``http://169.254.169.254/...`` (AWS / GCP / Azure
        IMDSv1).

        **Red today**：no URL guard → row gets created → ``success: True``.
        """
        self.authenticate('ssrf_alice', 'ssrf_alice')
        result = self._post(
            self._url(),
            self._create_payload(
                'MCP via AWS IMDSv1',
                'http://169.254.169.254/latest/meta-data/',
            ),
        )
        self._assert_rejected(result, '169.254.169.254', 'AC #30 / #31 (IPv4 IMDS)')

    # ------------------------------------------------------------------ #
    # AC #30 / #31: reject — IPv6 link-local + IMDSv2 (IPv6)              #
    # ------------------------------------------------------------------ #
    def test_url_rejects_ipv6_link_local(self):
        """AC #30: reject ``http://[fe80::*]/...`` (RFC 4291 link-local).

        IPv6 link-local can route to cloud metadata too; bracketed host
        form is the URL syntax (RFC 3986 §3.2.2).

        **Red today**：no guard → accepted.
        """
        self.authenticate('ssrf_alice', 'ssrf_alice')
        result = self._post(
            self._url(),
            self._create_payload(
                'MCP via IPv6 link-local',
                'http://[fe80::1]/metadata',
            ),
        )
        self._assert_rejected(result, '[fe80::1]', 'AC #30 / #31 (IPv6 link-local)')

    def test_url_rejects_imds_ipv6(self):
        """AC #30: reject ``http://[fd00:ec2::254]/...`` (AWS IMDSv2 IPv6).

        Per plan §AC #30: explicit allow-listed reject for the AWS
        IMDSv2 IPv6 address.

        **Red today**：no guard → accepted.
        """
        self.authenticate('ssrf_alice', 'ssrf_alice')
        result = self._post(
            self._url(),
            self._create_payload(
                'MCP via IPv6 IMDSv2',
                'http://[fd00:ec2::254]/latest/meta-data/',
            ),
        )
        self._assert_rejected(result, '[fd00:ec2::254]', 'AC #30 / #31 (IPv6 IMDSv2)')

    # ------------------------------------------------------------------ #
    # AC #30 / #31: reject — unsupported schemes                          #
    # ------------------------------------------------------------------ #
    def test_url_rejects_unsupported_scheme(self):
        """AC #30: scheme must be in ``{'http', 'https'}``.

        Two sub-asserts in one method to surface partial implementation
        (ftp blocked, file allowed) as a single red signal.

        **Red today**：both ftp and file accepted (no scheme check).
        """
        self.authenticate('ssrf_alice', 'ssrf_alice')

        # ---- ftp:// ----
        result_ftp = self._post(
            self._url(),
            self._create_payload(
                'MCP via FTP',
                'ftp://example.com/resource',
            ),
        )
        self.assertFalse(
            result_ftp.get('success'),
            "AC #30 (scheme allowlist): ftp:// must be rejected. "
            f"Got: {result_ftp!r}.",
        )

        # ---- file:// ----
        result_file = self._post(
            self._url(),
            self._create_payload(
                'MCP via local file',
                'file:///etc/passwd',
            ),
        )
        self.assertFalse(
            result_file.get('success'),
            "AC #30 (scheme allowlist): file:// must be rejected. "
            f"Got: {result_file!r}.",
        )

    # ------------------------------------------------------------------ #
    # AC #30: accept — legitimate external https                          #
    # ------------------------------------------------------------------ #
    def test_url_accepts_external_https(self):
        """AC #30 boundary: regular https://mcp.example.com/sse accepted.

        **Today GREEN, must stay GREEN** — guards against an over-eager
        implementation that bans everything except cluster.local. Plan
        AC #30 explicitly says 「accept hostname 不做進一步 internal-
        network filter」.
        """
        self.authenticate('ssrf_alice', 'ssrf_alice')
        result = self._post(
            self._url(),
            self._create_payload(
                'MCP via legitimate https',
                'https://mcp.example.com/sse',
            ),
        )
        self.assertTrue(
            result.get('success'),
            "AC #30 boundary: external https URL must be accepted. "
            "Implementation must not over-ban beyond the documented "
            f"reject list. Got: {result!r}.",
        )

    def test_url_accepts_cluster_local(self):
        """AC #30 boundary: cluster.local sidecar URL accepted.

        **Today GREEN, must stay GREEN** — this is the URL form that
        the sidecar-backed MCP path uses (sidecar-hint endpoint returns
        cluster.local URLs by design). A buggy implementation that
        blanket-bans ``svc.cluster.local`` would break the entire
        sidecar workflow.
        """
        self.authenticate('ssrf_alice', 'ssrf_alice')
        result = self._post(
            self._url(),
            self._create_payload(
                'MCP via cluster.local sidecar',
                'http://ssrf-svc-mcp.paas-ws-ssrf.svc.cluster.local:3001/mcp',
                transport='streamable_http',
            ),
        )
        self.assertTrue(
            result.get('success'),
            "AC #30 boundary: cluster.local URL must be accepted (this "
            "is the sidecar-backed MCP form). Implementation must NOT "
            "ban internal hostnames blanket-style. "
            f"Got: {result!r}.",
        )


# =========================================================================== #
# HC-1 — Legacy /api/workspaces/<wid>/services/<sid>/mcp-servers SSRF guard   #
#                                                                             #
# Reviewer M-R1 (multi-agent, 2026-05-21): the legacy MCP endpoint            #
# (``paas.py:1283-1332`` route + ``_create_mcp_server`` 1348-1372 +           #
# ``_update_mcp_server`` 1374-1390) is still used by frontend                 #
# ``cloud_service.js:587/612/638``. It completely bypasses ``_validate_mcp_   #
# url`` — workspace owner / admin can write ``http://169.254.169.254/...``    #
# directly into the URL field, which the subsequent ``action_test_connection``#
# / ``action_sync_tools`` will pursue (SSRF).                                 #
#                                                                             #
# Hotfix surface (per leader (b) ruling):                                     #
#   - Call ``self._validate_mcp_url(url)`` in both                            #
#     ``_create_mcp_server`` and ``_update_mcp_server`` before the            #
#     sudo create/write (mirroring                                            #
#     ``_create_project_mcp_server`` / ``_update_project_mcp_server`` AC #30) #
#                                                                             #
# Red expectation today: all 4 tests below show the legacy endpoint           #
# accepting attack URLs → ``result.success`` is True (or row created).        #
# Post-hotfix: SSRF guard applies → unified reject shape consistent with     #
# the new endpoint surface (F group).                                         #
#                                                                             #
# Scope (per leader): only 4 attack vectors — full 6-case matrix is on the    #
# new endpoint (F group); legacy gets a representative subset.                #
# =========================================================================== #


@tagged('post_install', '-at_install')
class TestLegacySsrfCheckOnMcpUrlCreate(HttpCase):
    """HC-1 — legacy ``/api/workspaces/<wid>/services/<sid>/mcp-servers``
    SSRF guard parity with new endpoint.

    Uses HttpCase identical to TestSsrfCheckOnMcpUrlCreate fixture: one
    workspace owner, one bound service + project. The fixture's
    workspace_id + service_id are the URL parameters for the legacy
    endpoint.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Workspace = cls.env['woow_paas_platform.workspace']
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']
        cls.Service = cls.env['woow_paas_platform.cloud_service']
        cls.Project = cls.env['project.project']
        cls.McpServer = cls.env['woow_paas_platform.mcp_server']

    def setUp(self):
        super().setUp()
        # skill-binding-ux：portal settings flag 預設 fail-closed off — 本
        # class 打 MCP write endpoint，需開 flag 保留正向行為守線（flag-off
        # denied 守線在 test_portal_settings_flag.py）。
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.portal_project_settings_editable', 'True')
        self.alice = _new_user(self.env, 'hc1_alice', 'Alice (HC-1)')
        self.ws = self.Workspace.sudo().create({
            'name': 'HC-1 WS', 'owner_id': self.alice.id,
        })
        self.template = self.Template.sudo().create({
            'name': 'HC-1 Tmpl',
            'slug': f'hc1-tpl-{uuid.uuid4().hex[:8]}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'hc1-tpl',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        self.service = _internal_provision(self.Service.sudo()).create({
            'name': 'HC-1 Svc',
            'workspace_id': self.ws.id,
            'template_id': self.template.id,
            'helm_release_name': 'hc1-svc',
            'helm_namespace': 'paas-ws-hc1',
        })
        self.project = self.Project.sudo().create({
            'name': 'HC-1 Project',
            'cloud_service_id': self.service.id,
        })
        # Seed a valid pre-existing MCP for the update-direction test —
        # legacy ``_update_mcp_server`` expects an existing server_id.
        self.existing_mcp = self.McpServer.sudo().create({
            'name': 'HC-1 Existing Valid MCP',
            'url': 'https://mcp.example.com/sse',
            'transport': 'sse',
            'scope': 'user',
            'project_id': self.project.id,
        })

    def _post(self, url, params):
        response = self.url_open(
            url,
            data=json_lib.dumps({
                'jsonrpc': '2.0', 'method': 'call', 'params': params,
            }),
            headers={'Content-Type': 'application/json'},
            timeout=30,
        )
        self.assertEqual(
            response.status_code, 200,
            f"Endpoint {url!r} returned status={response.status_code}. "
            f"Body excerpt: {response.text[:200]!r}",
        )
        body = response.json()
        self.assertIn('result', body, f"Body: {body!r}")
        return body['result']

    def _legacy_url(self):
        return (
            f'/api/workspaces/{self.ws.id}/services/{self.service.id}'
            f'/mcp-servers'
        )

    def _assert_rejected(self, result, target_host, label):
        """Same shape as F group: reject + 'url host not allowed' error."""
        self.assertFalse(
            result.get('success'),
            f"{label}: URL accepted by legacy endpoint, must be rejected. "
            f"Result: {result!r}.",
        )
        err = (result.get('error') or '').lower()
        self.assertIn(
            'url host not allowed',
            err,
            f"{label}: rejection error must mirror the new endpoint "
            f"contract ('URL host not allowed' per plan AC #31). "
            f"Got: {result.get('error')!r}.",
        )

    # ------------------------------------------------------------------ #
    # HC-1: create direction — AWS metadata IPv4                          #
    # ------------------------------------------------------------------ #
    def test_legacy_create_mcp_server_rejects_aws_metadata_ipv4(self):
        """HC-1 / M-R1: legacy ``_create_mcp_server`` must reject
        ``http://169.254.169.254/...`` (AWS IMDSv1 metadata).

        **Red today**：legacy endpoint accepts any URL → success=True →
        attack row stored → subsequent action_test_connection fires
        SSRF.

        Post-hotfix: legacy endpoint calls ``_validate_mcp_url`` before
        sudo create; rejected with 'URL host not allowed' wording
        matching new endpoint contract (plan AC #31).
        """
        self.authenticate('hc1_alice', 'hc1_alice')
        result = self._post(
            self._legacy_url(),
            {
                'action': 'create',
                'name': 'HC-1 Legacy IMDS Attack',
                'url': 'http://169.254.169.254/latest/meta-data/',
                'transport': 'sse',
            },
        )
        self._assert_rejected(
            result, '169.254.169.254',
            'HC-1 / M-R1 (legacy create / IPv4 IMDS)',
        )
        # Sanity: row not committed.
        injected = self.McpServer.sudo().search([
            ('project_id', '=', self.project.id),
            ('name', '=', 'HC-1 Legacy IMDS Attack'),
        ])
        self.assertFalse(
            injected,
            "HC-1 / M-R1: rejected create must not leave the MCP row "
            "in DB. Hotfix must validate URL BEFORE the sudo create "
            "(validate-then-create order; reverse order would still "
            "leak the row).",
        )

    # ------------------------------------------------------------------ #
    # HC-1: update direction — AWS metadata IPv4                          #
    # ------------------------------------------------------------------ #
    def test_legacy_update_mcp_server_rejects_aws_metadata_ipv4(self):
        """HC-1 / M-R1: legacy ``_update_mcp_server`` must reject URL
        rewrite to ``http://169.254.169.254/...``.

        The update-direction attack: attacker has a legitimate MCP row
        (created via valid URL) then rewrites the URL to a metadata
        endpoint, weaponising the subsequent sync.

        **Red today**：legacy update endpoint blindly writes any URL.
        Post-hotfix: ``_validate_mcp_url`` called before write.
        """
        self.authenticate('hc1_alice', 'hc1_alice')
        original_url = self.existing_mcp.url

        result = self._post(
            self._legacy_url(),
            {
                'action': 'update',
                'server_id': self.existing_mcp.id,
                'url': 'http://169.254.169.254/latest/meta-data/',
            },
        )
        self._assert_rejected(
            result, '169.254.169.254',
            'HC-1 / M-R1 (legacy update / IPv4 IMDS)',
        )
        # Sanity: original URL preserved (no partial write).
        self.existing_mcp.invalidate_recordset()
        self.assertEqual(
            self.existing_mcp.url, original_url,
            "HC-1 / M-R1: rejected update must leave the existing "
            "MCP URL unchanged. Hotfix must validate BEFORE the sudo "
            "write — reverse order would already persist the bad URL.",
        )

    # ------------------------------------------------------------------ #
    # HC-1: create direction — IPv6 link-local                            #
    # ------------------------------------------------------------------ #
    def test_legacy_create_mcp_server_rejects_ipv6_link_local(self):
        """HC-1 / M-R1: legacy create must reject IPv6 link-local
        (``http://[fe80::*]/...``).

        Mirrors F group ``test_url_rejects_ipv6_link_local`` on the
        legacy surface.
        """
        self.authenticate('hc1_alice', 'hc1_alice')
        result = self._post(
            self._legacy_url(),
            {
                'action': 'create',
                'name': 'HC-1 Legacy IPv6 LL',
                'url': 'http://[fe80::1]/metadata',
                'transport': 'sse',
            },
        )
        self._assert_rejected(
            result, '[fe80::1]',
            'HC-1 / M-R1 (legacy create / IPv6 link-local)',
        )

    # ------------------------------------------------------------------ #
    # HC-1: create direction — unsupported scheme                         #
    # ------------------------------------------------------------------ #
    def test_legacy_create_mcp_server_rejects_unsupported_scheme(self):
        """HC-1 / M-R1: legacy create must reject ``file://`` /
        ``ftp://`` schemes.

        Mirrors F group ``test_url_rejects_unsupported_scheme`` on the
        legacy surface. Only ``file://`` exercised here (representative);
        full scheme matrix is on the new endpoint.
        """
        self.authenticate('hc1_alice', 'hc1_alice')
        result = self._post(
            self._legacy_url(),
            {
                'action': 'create',
                'name': 'HC-1 Legacy file:// Attack',
                'url': 'file:///etc/passwd',
                'transport': 'sse',
            },
        )
        self.assertFalse(
            result.get('success'),
            "HC-1 / M-R1 (legacy create / scheme allowlist): file:// "
            f"must be rejected by legacy endpoint. Got: {result!r}.",
        )

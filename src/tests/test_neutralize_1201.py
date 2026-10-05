"""#1201：平台 addon 的 ``data/neutralize.sql`` 與計費 cron 的 ``is_neutralized`` guard。

#1162 裁決 R2-Q1／R2-Q2：prod clone 的中和以 Odoo 官方 ``odoo neutralize`` 當第一層，
平台只補差。官方指令對每個已安裝模組讀 ``<module>/data/neutralize.sql``——那是 SQL，
欄位改名／表改名時不會有任何東西先紅，要等到彩排當下整批交易炸掉才知道。上游
``odoo/addons/base/tests/test_neutralize.py`` 能擋這件事，但平台 CI 的
``--test-tags /woow_paas_platform`` 跑不到 base 的那支 ⇒ 本檔補同型測試，另外斷言
本 addon 這段的關鍵效果（不只是「不炸」）。

guard：``data/billing_ssot_guard.xml`` 是 noupdate=0，每次 ``-u`` 都呼叫
``_ensure_base_auto_invoice_cron_active``。不擋的話，中和後任何一次升級都會把計費
cron 寫回 active（#1162 查核找到的 blocker）。

紅→綠：
* ``TestPlatformNeutralizeSql``：本 addon 沒有 ``data/neutralize.sql`` 時
  ``get_neutralization_queries(['woow_paas_platform'])`` 是空的 ⇒ 第一條就紅。
* ``TestBillingCronNeutralizedGuard``：guard 不存在時，``is_neutralized`` 設了照樣
  把 cron 開回來 ⇒ 紅。陽性對照（key 不存在時照舊開回）保證 guard 不是「一律不開」。

斷言一律直接查 SQL：中和是 SQL 層的事，ORM cache 與 ``ir.config_parameter`` 的
ormcache 都不會知道表被改過。
"""
import json
import logging
from datetime import timedelta

from odoo import fields
from odoo.modules import neutralize
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import convert

from ..models.cloud_service import _internal_provision
from ..models.security_access import _internal_identity as _sa_internal
from .smart_home_fixture import sh_model

MODULE = 'woow_paas_platform'
MARKER = '-- woow_paas_platform neutralize (#1201)'
SENTINEL = 'neutralized'
BASE_CRON_XMLID = 'sh_subscription.sh_ir_cron_subscription_auto_invoice'
GUARD_DATA_FILE = 'data/billing_ssot_guard.xml'
NEUTRALIZED_FLAG = 'database.is_neutralized'


def _platform_queries():
    return list(neutralize.get_neutralization_queries([MODULE]))


@tagged('post_install', '-at_install', 'neutralize')
class TestPlatformNeutralizeSql(TransactionCase):
    """在交易內執行本 addon 的 neutralize.sql，斷言關鍵效果。"""

    def setUp(self):
        super().setUp()
        self.ICP = self.env['ir.config_parameter'].sudo()
        admin = self.env.ref('base.user_admin')

        # ---- operator／身分鍵：一個有 prod 值、一個刻意不存在（驗 upsert）
        self.ICP.set_param('woow_paas_platform.operator_url', 'http://paas-operator:8000')
        self.ICP.set_param('woow_paas_platform.operator_api_key', 'live-operator-key')
        self.ICP.set_param('woow_paas_platform.namespace_prefix', 'paas-ws-')
        self.ICP.set_param('woow_paas_platform.default_enable_agent_runtime', 'True')
        self.ICP.search([('key', '=', 'woow_paas_platform.deploy_env')]).unlink()

        # ---- mail 佇列
        Mail = self.env['mail.mail'].sudo()
        self.mail_outgoing = Mail.create({
            'subject': 'n1201 outgoing', 'body_html': '<p>x</p>',
            'email_to': 'n1201-out@test.example',
        })
        self.mail_exception = Mail.create({
            'subject': 'n1201 exception', 'body_html': '<p>x</p>',
            'email_to': 'n1201-exc@test.example',
        })
        self.mail_exception.write({'state': 'exception'})

        # ---- OAuth 進入面
        client = self.env.ref('woow_paas_platform.oauth_client_ha_smart_home')
        now = fields.Datetime.now()
        self.env['woow_paas_platform.oauth_token'].sudo().create({
            'access_token': 'n1201-access', 'refresh_token': 'n1201-refresh',
            'scope': 'smart-home:read',
            'expires_at': now + timedelta(hours=1),
            'refresh_expires_at': now + timedelta(days=30),
            'client_id': client.id, 'user_id': admin.id,
        })
        self.env['woow_paas_platform.oauth_code'].sudo().create({
            'code': 'n1201-code', 'redirect_uri': 'https://example.invalid/cb',
            'expires_at': now + timedelta(minutes=5),
            'client_id': client.id, 'user_id': admin.id,
        })
        self.env['woow_paas_platform.oauth_device_code'].sudo().create({
            'device_code': 'n1201-device', 'user_code': 'N1201-UC',
            'expires_at': now + timedelta(minutes=10), 'client_id': client.id,
        })

        # ---- 第 E 層：租戶 live 憑證（tunnel_token 等是身分錨點，走 ORM 要開閘；
        # 這裡只需要「表裡有值」這個前提，直接用 SQL 寫最不受 write guard 影響）
        workspace = self.env['woow_paas_platform.workspace'].sudo().create({
            'name': 'n1201 ws', 'owner_id': admin.id,
        })
        self.sa = _sa_internal(
            self.env['woow_paas_platform.security_access'].sudo()
        ).create({'name': 'n1201 gateway', 'workspace_id': workspace.id})
        self.sa_empty = _sa_internal(
            self.env['woow_paas_platform.security_access'].sudo()
        ).create({'name': 'n1201 gateway empty', 'workspace_id': workspace.id})
        self.sh = sh_model(self.env).sudo().create({
            'name': 'n1201 home', 'workspace_id': workspace.id,
        })
        template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'n1201 template', 'slug': 'n1201-template',
            'category': 'ai_llm', 'helm_repo_url': '',
            'helm_chart_name': 'oci://example/n1201',
            'helm_chart_version': '0.1.0', 'mcp_enabled': False,
            'helm_default_values': '{}',
        })
        self.service = _internal_provision(
            self.env['woow_paas_platform.cloud_service'].sudo()
        ).create({
            'name': 'n1201 svc', 'workspace_id': workspace.id,
            'template_id': template.id, 'state': 'running',
            'helm_namespace': 'paas-ws-n1201', 'helm_release_name': 'n1201',
            'helm_chart_version': '0.1.0', 'helm_values': json.dumps({}),
        })
        Secret = self.env['woow_paas_platform.cloud_service_secret'].sudo()
        if not Secret.search([('service_id', '=', self.service.id)]):
            Secret.create({'service_id': self.service.id,
                           'admin_password': 'placeholder'})

        # ---- MCP server／AI config
        self.mcp = self.env['woow_paas_platform.mcp_server'].sudo().create({
            'name': 'n1201 mcp', 'url': 'https://mcp.example.invalid/sse',
        })

        self.ai_config_id = self.env['ai.config'].sudo().create({
            'name': 'n1201 ai', 'api_key': 'live-llm-key',
        }).id

        # ---- agent session（建立路徑要 namespace secret env；這裡只要列存在）
        project_active = self.env['project.project'].sudo().create({'name': 'n1201 p1'})
        project_absent = self.env['project.project'].sudo().create({'name': 'n1201 p2'})

        self.env.flush_all()
        cr = self.env.cr
        cr.execute(
            "UPDATE woow_paas_platform_security_access SET tunnel_token = %s WHERE id = %s",
            ('live-sa-tunnel-token', self.sa.id))
        cr.execute(
            "UPDATE woow_paas_platform_security_access SET tunnel_token = NULL WHERE id = %s",
            (self.sa_empty.id,))
        cr.execute(
            "UPDATE woow_paas_platform_smart_home SET tunnel_token = %s WHERE id = %s",
            ('live-sh-tunnel-token', self.sh.id))
        cr.execute(
            "UPDATE woow_paas_platform_cloud_service"
            "   SET mcp_auth_token = %s, n8n_api_key = %s, n8n_owner_password = %s"
            " WHERE id = %s",
            ('live-mcp-token', 'live-n8n-key', 'live-n8n-pass', self.service.id))
        cr.execute(
            "UPDATE woow_paas_platform_cloud_service_secret"
            "   SET admin_password = %s, gateway_api_key = %s WHERE service_id = %s",
            ('live-admin-pass', 'live-gateway-key', self.service.id))
        cr.execute(
            "UPDATE woow_paas_platform_mcp_server"
            "   SET api_key = %s, headers_json = %s, active = true WHERE id = %s",
            ('live-mcp-key', '{"Authorization": "Bearer live"}', self.mcp.id))
        cr.execute(
            "INSERT INTO woow_paas_platform_agent_session (project_id, state, pod_ws_url)"
            " VALUES (%s, 'active', 'ws://pod.example.invalid'), (%s, 'absent', NULL)"
            " RETURNING id, state",
            (project_active.id, project_absent.id))
        self.session_ids = dict((state, sid) for sid, state in cr.fetchall())
        # 排程：一顆會被推遲的 cron、一筆待觸發的 trigger
        self.cron = self.env.ref(BASE_CRON_XMLID)
        cr.execute("UPDATE ir_cron SET nextcall = %s WHERE id = %s",
                   (now, self.cron.id))
        cr.execute(
            "INSERT INTO ir_cron_trigger (cron_id, call_at) VALUES (%s, %s)",
            (self.cron.id, now))

    def _run_platform_sql(self):
        queries = _platform_queries()
        self.assertEqual(
            len(queries), 1,
            "woow_paas_platform 必須出 data/neutralize.sql（#1201）——"
            "`odoo neutralize` 讀的就是這支檔")
        self.env.flush_all()
        for query in queries:
            self.env.cr.execute(query)
        self.env.invalidate_all()
        return queries[0]

    def _param(self, key):
        self.env.cr.execute(
            "SELECT value FROM ir_config_parameter WHERE key = %s", (key,))
        row = self.env.cr.fetchone()
        return row and row[0]

    def _scalar(self, query, params=None):
        self.env.cr.execute(query, params)
        return self.env.cr.fetchone()[0]

    def test_marker_is_first_line(self):
        """#1206 的 neutralize-clone.sh 用第一行判斷 image 內的 addon 是否已含平台段。"""
        sql = self._run_platform_sql()
        self.assertEqual(sql.splitlines()[0], MARKER)

    def test_stdout_join_keeps_last_statement_terminated(self):
        """``odoo neutralize --stdout`` 對每段做 ``rstrip(';') + ';'``——檔尾若是註解，
        補上的 ``;`` 會落在註解裡，下一個模組的第一句就會黏到本檔最後一句。"""
        sql = _platform_queries()[0]
        last_line = sql.rstrip().splitlines()[-1].strip()
        self.assertTrue(last_line.endswith(';'), last_line)
        self.assertFalse(last_line.startswith('--'), last_line)

    def test_operator_blackhole_and_identity_keys(self):
        self._run_platform_sql()
        self.assertEqual(self._param('woow_paas_platform.operator_url'),
                         'http://127.0.0.1:9')
        self.assertEqual(self._param('woow_paas_platform.operator_api_key'), SENTINEL)
        self.assertEqual(self._param('woow_paas_platform.namespace_prefix'),
                         'paas-ws-stg-')
        self.assertEqual(
            self._param('woow_paas_platform.deploy_env'), 'stg',
            "deploy_env 在 setUp 被刻意刪掉：鍵不存在時也要寫進去（upsert）")
        self.assertEqual(
            self._param('woow_paas_platform.default_enable_agent_runtime'), 'False')

    def test_mail_queue_outgoing_cancelled(self):
        self._run_platform_sql()
        self.assertEqual(self.mail_outgoing.state, 'cancel')
        self.assertEqual(self.mail_exception.state, 'exception',
                         "只動 outgoing，其他狀態保留原樣")
        self.assertEqual(
            self._scalar("SELECT count(*) FROM mail_mail WHERE state = 'outgoing'"), 0)

    def test_oauth_entry_surface_emptied(self):
        self._run_platform_sql()
        for table in ('woow_paas_platform_oauth_token',
                      'woow_paas_platform_oauth_code',
                      'woow_paas_platform_oauth_device_code'):
            self.assertEqual(self._scalar(f"SELECT count(*) FROM {table}"), 0, table)
        self.assertTrue(
            self.env.ref('woow_paas_platform.oauth_client_ha_smart_home').exists(),
            "oauth_client 不動（secret 已 hash）")

    def test_tenant_live_credentials_neutralized(self):
        self._run_platform_sql()
        checks = [
            ('woow_paas_platform_security_access', 'tunnel_token'),
            ('woow_paas_platform_smart_home', 'tunnel_token'),
            ('woow_paas_platform_cloud_service_secret', 'admin_password'),
            ('woow_paas_platform_cloud_service_secret', 'gateway_api_key'),
            ('woow_paas_platform_cloud_service', 'mcp_auth_token'),
            ('woow_paas_platform_cloud_service', 'n8n_api_key'),
            ('woow_paas_platform_cloud_service', 'n8n_owner_password'),
            ('woow_paas_platform_mcp_server', 'api_key'),
        ]
        for table, column in checks:
            leaked = self._scalar(
                f"SELECT count(*) FROM {table}"
                f" WHERE {column} IS NOT NULL AND {column} <> '' AND {column} <> %s",
                (SENTINEL,))
            self.assertEqual(leaked, 0, f"{table}.{column} 仍有 live 值")
        self.assertEqual(
            self._scalar("SELECT tunnel_token FROM woow_paas_platform_security_access"
                         " WHERE id = %s", (self.sa.id,)), SENTINEL)
        self.assertIsNone(
            self._scalar("SELECT tunnel_token FROM woow_paas_platform_security_access"
                         " WHERE id = %s", (self.sa_empty.id,)),
            "空值保持空值：不能把「沒設定」改成看似有憑證")

    def test_mcp_and_ai_outbound_disabled(self):
        self._run_platform_sql()
        self.assertEqual(
            self._scalar("SELECT count(*) FROM woow_paas_platform_mcp_server"
                         " WHERE active"), 0)
        self.assertIsNone(self._scalar(
            "SELECT headers_json FROM woow_paas_platform_mcp_server WHERE id = %s",
            (self.mcp.id,)))
        self.assertIsNone(self._scalar(
            "SELECT api_key FROM ai_config WHERE id = %s", (self.ai_config_id,)))
        self.assertEqual(
            self._scalar("SELECT count(*) FROM ai_config WHERE api_key IS NOT NULL"), 0)

    def test_agent_sessions_reaped(self):
        self._run_platform_sql()
        self.env.cr.execute(
            "SELECT id, state, reaped_since FROM woow_paas_platform_agent_session"
            " WHERE id IN %s", (tuple(self.session_ids.values()),))
        rows = {sid: (state, reaped) for sid, state, reaped in self.env.cr.fetchall()}
        state, reaped_since = rows[self.session_ids['active']]
        self.assertEqual(state, 'reaped')
        self.assertTrue(reaped_since,
                        "model invariant：reaped_since 有值 ⟺ state='reaped'")
        state, reaped_since = rows[self.session_ids['absent']]
        self.assertEqual(state, 'absent', "absent 沒有 pod，不改")
        self.assertFalse(reaped_since)

    def test_cron_pushed_out_and_triggers_cleared(self):
        self._run_platform_sql()
        self.assertEqual(self._scalar("SELECT count(*) FROM ir_cron_trigger"), 0)
        self.assertEqual(
            self._scalar(
                "SELECT count(*) FROM ir_cron c"
                " WHERE c.nextcall < (NOW() AT TIME ZONE 'UTC') + INTERVAL '99 years'"
                "   AND c.id <> %s",
                (self.env.ref('base.autovacuum_job').id,)),
            0, "除 autovacuum 探針外，所有 cron 的 nextcall 都要推到 100 年後")
        before = self._scalar("SELECT nextcall FROM ir_cron WHERE id = %s",
                              (self.cron.id,))
        self._run_platform_sql()
        self.assertEqual(
            self._scalar("SELECT nextcall FROM ir_cron WHERE id = %s", (self.cron.id,)),
            before, "重複執行（彩排 Stage A／B）不可一直往後疊")

    def test_autovacuum_probe_untouched(self):
        """base.autovacuum_job 是彩排的 cron 探針（#1162 D3-A），與官方 SQL 一樣排除。"""
        probe = self.env.ref('base.autovacuum_job')
        before = self._scalar("SELECT nextcall FROM ir_cron WHERE id = %s", (probe.id,))
        self._run_platform_sql()
        self.assertEqual(
            self._scalar("SELECT nextcall FROM ir_cron WHERE id = %s", (probe.id,)),
            before)

    def test_all_installed_modules_neutralize_in_one_transaction(self):
        """仿上游 TestNeutralize：全部已安裝模組的 SQL 依序執行不炸（含本 addon）。"""
        installed = neutralize.get_installed_modules(self.env.cr)
        self.assertIn(MODULE, installed)
        queries = list(neutralize.get_neutralization_queries(installed))
        self.assertIn(MARKER, '\n'.join(queries))
        self.env.flush_all()
        for query in queries:
            self.env.cr.execute(query)
        self.env.invalidate_all()
        self.assertEqual(self._param(NEUTRALIZED_FLAG), 'true')
        self.assertEqual(self._param('woow_paas_platform.operator_url'),
                         'http://127.0.0.1:9')


@tagged('post_install', '-at_install', 'neutralize')
class TestBillingCronNeutralizedGuard(TransactionCase):
    """``database.is_neutralized`` 為真時，計費 cron 不得被 re-assert 回 active。"""

    def setUp(self):
        super().setUp()
        self.Sub = self.env['sh.subscription.subscription'].sudo()
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.cron = self.env.ref(BASE_CRON_XMLID)
        # 中和後的狀態：官方 SQL 已把 cron 關掉
        self.cron.sudo().write({'active': False})

    def test_guard_keeps_cron_inactive_when_neutralized(self):
        self.ICP.set_param(NEUTRALIZED_FLAG, 'true')

        with self.assertLogs(
                'odoo.addons.woow_paas_platform.models.sh_subscription_fix',
                level=logging.INFO) as logs:
            result = self.Sub._ensure_base_auto_invoice_cron_active()

        self.assertFalse(result)
        self.cron.invalidate_recordset()
        self.assertFalse(
            self.cron.active,
            "中和過的 DB 不可把計費 cron 開回來（#1201）——否則 prod clone 會對真實"
            "租戶開帳單")
        self.assertTrue(any('neutralized' in line for line in logs.output), logs.output)

    def test_reasserts_when_flag_absent(self):
        """陽性對照：key 不存在時行為不變（#778 re-assert 照舊）。"""
        self.ICP.search([('key', '=', NEUTRALIZED_FLAG)]).unlink()

        result = self.Sub._ensure_base_auto_invoice_cron_active()

        self.assertTrue(result)
        self.assertTrue(self.cron.active)

    def test_upgrade_replay_after_real_neutralize_keeps_cron_inactive(self):
        """端到端：跑全部已安裝模組的中和 SQL（設 flag 的是官方 base SQL），再重播
        ``billing_ssot_guard.xml``（＝每次 ``-u`` 實際執行的資料檔），cron 仍 inactive。"""
        self.cron.sudo().write({'active': True})
        installed = neutralize.get_installed_modules(self.env.cr)
        self.env.flush_all()
        for query in neutralize.get_neutralization_queries(installed):
            self.env.cr.execute(query)
        self.env.invalidate_all()
        self.env.registry.clear_cache()
        self.assertFalse(self.cron.active, "官方 SQL 應已停掉計費 cron")

        convert.convert_file(
            self.env, MODULE, GUARD_DATA_FILE, {}, mode='update', noupdate=False)

        self.cron.invalidate_recordset()
        self.assertFalse(
            self.cron.active,
            "重播 billing_ssot_guard.xml（模擬中和後的 -u）不可把計費 cron 開回來")

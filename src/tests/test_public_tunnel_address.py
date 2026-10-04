# -*- coding: utf-8 -*-
"""對外通道位址回讀（ngrok sidecar ＋ operator `GET …/public-address`；P1 泛化）。

## 要解的問題

平台的 Cloudflare 通道只載 HTTP/WS（而且會剝掉 Tailscale 控制協議的 Upgrade
header），EMQX 的原生 MQTT（TCP）與 Headscale 的控制面（HTTPS）都走租戶自己的
ngrok agent（chart 的 sidecar）。agent 實際拿到的位址只有它自己知道，chart 因此每
60 秒把當前位址印到 sidecar 自己的 container log，operator 從那裡讀回來。
18.0.1.0.205 以前這條路只給 EMQX（`has_public_mqtt_address`／`mqtt_public_url`／
`/mqtt-address`），P1 把它泛化成「對外通道位址」，EMQX 行為不變。

## 守線

1. **能力 gate 只有一個事實源**：`cloud_app_template.has_public_tunnel_address`。
   model 的探測閘門、service payload、前端文案三處都讀它（repo-lint 第 7 條機械守著
   前端不得用 slug 做集合成員判斷）。本檔的 fixture 刻意把 slug 取成**反直覺**的組合
   ——旗標為 True 的 template slug 不叫 emqx、叫 emqx 的那個旗標是 False——
   有人改回用 slug 推導，兩支會同時變紅。

2. **「沒有位址」不是錯誤，但「有結論的沒有」要清帳本**：
   `_probe_public_tunnel_address` 的三態（None 沒探／'' 探過沒有／位址）直接對應
   `_release_facts` 的「只有探過才寫」。隧道斷掉之後還留著一個連不上的舊位址，
   比空白更糟：租戶會拿它去設定裝置然後一直連不上。

3. **附帶資訊不得打斷 poll**：operator 這一支掛掉時探測降級成「沒探」。

4. **改名不掉資料**：帳本欄位從 `mqtt_public_url` 改名，升級時 `init()` 把舊值搬過來
   ——否則 EMQX 服務的位址在升級後消失最多一天（直到對帳 cron 重新探到）。

5. **平台先上也不壞 EMQX**：operator 還沒有 `/public-address`（404）時 client 退回
   `/mqtt-address`。

6. **標籤是資料值、不是能力判準**：`public_tunnel_label` 只決定那一格叫什麼；它是範本
   record 的欄位內容，seed 直接寫中文、不可翻譯、不進 .po
   （docs/decisions/patterns/i18n-scope-data-values-vs-code-strings.md）。
"""
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged
from odoo.tools.translate import PoFileReader

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from ..models.cloud_service import _internal_provision
from ..services.paas_operator import PaaSOperatorAPIError, PaaSOperatorClient

ADDRESS = 'tcp://6.tcp.ngrok.io:22222'
HTTPS_ADDRESS = 'https://abc-123.ngrok-free.app'


class _Base(TransactionCase):

    def setUp(self):
        super().setUp()
        self.controller = PaasController()
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'Tunnel readback workspace',
        })

    def _template(self, slug, has_public_tunnel_address, label=False):
        return self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Tunnel readback %s' % slug,
            'slug': slug,
            'category': 'automation',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example/%s' % slug,
            'helm_chart_version': '0.1.0',
            'mcp_enabled': False,
            'has_public_tunnel_address': has_public_tunnel_address,
            'public_tunnel_label': label,
        })

    def _service(self, template, **extra):
        vals = {
            'name': 'svc-tunnel-%s' % template.slug,
            'workspace_id': self.workspace.id,
            'template_id': template.id,
            'state': 'running',
            'helm_namespace': 'paas-ws-test',
            'helm_release_name': 'svc-tunnel-%s' % template.slug,
            'helm_values': json.dumps({}),
        }
        vals.update(extra)
        return _internal_provision(
            self.env['woow_paas_platform.cloud_service']).create(vals)

    def _client(self, public_url=ADDRESS, raises=None):
        client = MagicMock()
        if raises is not None:
            client.get_public_address.side_effect = raises
        else:
            client.get_public_address.return_value = {'public_url': public_url}
        return client


@tagged('post_install', '-at_install')
class TestCapabilityGateSingleSource(_Base):
    """旗標 ⟺ 能力。slug 刻意反直覺，擋「改回用 slug 推導」。"""

    def test_flag_on_a_slug_that_is_not_emqx(self):
        service = self._service(self._template('iot-broker-x', True))
        self.assertTrue(
            service._chart_has_public_tunnel_address(),
            '能力只看 template 旗標；slug 不叫 emqx 也必須成立')

    def test_emqx_named_slug_without_the_flag(self):
        service = self._service(self._template('emqx', False))
        self.assertFalse(
            service._chart_has_public_tunnel_address(),
            'slug 叫 emqx 但旗標關著 ⇒ 沒有能力（否則平台會為每輪 poll 打一次'
            '問不到東西的 operator 呼叫，並在服務頁長出永遠空白的一格）')

    def test_seeded_products(self):
        """EMQX 有（標籤是 MQTT 那一格）、Nextcloud 沒有——改名後 EMQX 行為不變。"""
        emqx = self.env.ref('woow_paas_platform.cloud_app_emqx')
        self.assertTrue(emqx.has_public_tunnel_address)
        self.assertEqual(
            emqx.public_tunnel_label, '對外 MQTT 位址',
            '標籤是資料值：seed 直接寫中文原文（不靠 .po 翻譯）')
        self.assertFalse(
            self.env.ref('woow_paas_platform.cloud_app_nextcloud')
            .has_public_tunnel_address)

    def test_legacy_field_is_gone(self):
        """舊名不留雙軌：兩個欄位並存就是兩個事實源（#460 的形態）。"""
        self.assertNotIn(
            'has_public_mqtt_address',
            self.env['woow_paas_platform.cloud_app_template']._fields)
        self.assertNotIn(
            'mqtt_public_url', self.env['woow_paas_platform.cloud_service']._fields)


@tagged('post_install', '-at_install')
class TestProbeThreeStates(_Base):

    def test_no_capability_does_not_even_ask(self):
        service = self._service(self._template('no-tunnel', False))
        client = self._client()
        self.assertIsNone(service._probe_public_tunnel_address(client))
        client.get_public_address.assert_not_called()

    def test_probed_and_present(self):
        service = self._service(self._template('has-tunnel', True))
        client = self._client(ADDRESS)
        self.assertEqual(service._probe_public_tunnel_address(client), ADDRESS)
        client.get_public_address.assert_called_once_with(
            'paas-ws-test', 'svc-tunnel-has-tunnel')

    def test_probed_https_is_passed_through(self):
        """Headscale 的控制網址是 https://——平台不再假設只有 tcp://。"""
        service = self._service(self._template('has-https', True))
        self.assertEqual(
            service._probe_public_tunnel_address(self._client(HTTPS_ADDRESS)),
            HTTPS_ADDRESS)

    def test_probed_and_absent_is_empty_string_not_none(self):
        """租戶沒開／agent 還沒連上 ⇒ 有結論的「沒有」，要能把帳本清掉。"""
        service = self._service(self._template('has-tunnel-absent', True))
        self.assertEqual(
            service._probe_public_tunnel_address(self._client(None)), '',
            "探過但沒有位址必須回 ''（不是 None）——None 的語意是「這一輪沒探」，"
            '會讓斷掉的隧道留著一個連不上的舊位址')

    def test_operator_failure_degrades_to_not_probed(self):
        service = self._service(self._template('has-tunnel-err', True))
        with self.assertLogs(
                'odoo.addons.woow_paas_platform.models.cloud_service',
                level='WARNING'):
            got = service._probe_public_tunnel_address(
                self._client(raises=RuntimeError('operator down')))
        self.assertIsNone(
            got, 'operator 掛掉是「沒探」，不是「沒有位址」——不得清掉帳本，'
            '也不得讓例外冒出去打斷同一輪 poll 的 state 回寫')


@tagged('post_install', '-at_install')
class TestReleaseFactsFunnel(_Base):
    """欄位集合收斂在 `_release_facts`（#627 的紀律）。"""

    def _facts(self, service, tunnel_address):
        return service._release_facts(
            {'revision': 3, 'chart_version': '0.1.0'}, tunnel_address=tunnel_address)

    def test_not_probed_leaves_the_ledger_alone(self):
        service = self._service(self._template('facts-none', True))
        self.assertNotIn('public_tunnel_url', self._facts(service, None))

    def test_probed_present_writes_the_address(self):
        service = self._service(self._template('facts-set', True))
        self.assertEqual(
            self._facts(service, ADDRESS)['public_tunnel_url'], ADDRESS)

    def test_probed_absent_clears_the_address(self):
        service = self._service(self._template('facts-clear', True))
        self.assertIs(
            self._facts(service, '')['public_tunnel_url'], False,
            '有結論的「沒有」要寫成 False 清掉帳本')


@tagged('post_install', '-at_install')
class TestPollWritesTheAddress(_Base):

    def _status(self, release_status='deployed'):
        return {
            'release': {
                'status': release_status, 'revision': 3, 'chart_version': '0.1.0',
            },
            # 空 pods 對就緒判定是 fail-closed ⇒ 不給 pod 會在「什麼都沒發生」
            # 的狀態下對回寫下斷言（#456／F-423-1）。
            'pods': [{'name': 'p1', 'phase': 'Running', 'ready': '1/1'}],
        }

    def _poll(self, service, client, status):
        client.get_status.return_value = status
        service._poll_and_update_status(client)
        service.invalidate_recordset()

    def test_deployed_release_writes_the_address(self):
        service = self._service(self._template('poll-set', True), state='deploying')
        client = self._client(HTTPS_ADDRESS)
        self._poll(service, client, self._status())
        self.assertEqual(service.helm_revision, 3, '前提：這輪 poll 真的寫了')
        self.assertEqual(service.public_tunnel_url, HTTPS_ADDRESS)

    def test_steady_state_poll_does_not_write(self):
        """釘住既有形狀：state 沒推進時這條 poll 不回寫任何 release 事實。

        `running` 服務的對帳由 `_cron_reconcile_release_facts` 負責（那是唯一看得到
        running 的路徑），見 TestReconcileCronRefreshesTheAddress。
        """
        service = self._service(self._template('poll-steady', True), state='running')
        client = self._client(ADDRESS)
        self._poll(service, client, self._status())
        self.assertFalse(
            service.public_tunnel_url,
            'state 未推進的 poll 不寫 facts；若哪天改成會寫，請一併檢視'
            'last_release_sync_at 的寫入頻率（見 _cron_reconcile_release_facts '
            'docstring 的 write_date 代價段）')

    def test_not_deployed_release_is_not_probed(self):
        """還沒 deploy 的服務裡根本還沒有 agent，問它只是白跑一趟。"""
        service = self._service(self._template('poll-pending', True), state='deploying')
        client = self._client(ADDRESS)
        self._poll(service, client, self._status(release_status='pending-install'))
        client.get_public_address.assert_not_called()


@tagged('post_install', '-at_install')
class TestReconcileCronRefreshesTheAddress(_Base):
    """`running` 服務的位址新鮮度由每日對帳 cron 維持。

    ngrok 免費方案每次 agent 重連就換 TCP 位址，而那種變動發生時服務正是 running
    ——poll 的 domain 只收轉移狀態，所以這條是唯一接得到的路徑。
    """

    def _run_cron(self, client, status_release=None):
        status = {'release': status_release or {
            'status': 'deployed', 'revision': 3, 'chart_version': '0.1.0'}}
        client.get_status.return_value = status
        with patch(
            'odoo.addons.woow_paas_platform.services.paas_operator'
            '.get_paas_operator_client',
            return_value=client,
        ):
            self.env['woow_paas_platform.cloud_service'] \
                ._cron_reconcile_release_facts()

    def test_a_changed_address_is_reconciled(self):
        service = self._service(self._template('cron-set', True), state='running')
        service.write({'public_tunnel_url': 'tcp://0.tcp.ngrok.io:11111'})
        self._run_cron(self._client(ADDRESS))
        service.invalidate_recordset()
        self.assertEqual(
            service.public_tunnel_url, ADDRESS,
            'agent 重連換了位址，帳本要跟上——否則租戶拿舊位址設定裝置會一直連不上')

    def test_a_dropped_tunnel_clears_a_stale_address(self):
        service = self._service(self._template('cron-clear', True), state='running')
        service.write({'public_tunnel_url': ADDRESS})
        self._run_cron(self._client(None))
        service.invalidate_recordset()
        self.assertFalse(
            service.public_tunnel_url,
            '隧道斷掉後必須清掉——留著一個連不上的舊位址比空白更糟')

    def test_products_without_the_capability_cost_nothing(self):
        """本 cron 掃全部 running 服務 ⇒ 沒這能力的商品不得多打一次 operator。"""
        self._service(self._template('cron-off', False), state='running')
        client = self._client(ADDRESS)
        self._run_cron(client)
        client.get_public_address.assert_not_called()


@tagged('post_install', '-at_install')
class TestDetailPayload(_Base):
    """前端那一格的資料來源：旗標、標籤、值分開給。"""

    def _detail(self, service):
        member = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Tunnel detail viewer',
            'login': 'tunnel-detail-viewer-%s' % service.id,
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id, 'user_id': member.id, 'role': 'admin'})
        with patch('odoo.addons.woow_paas_platform.controllers.paas.request',
                   new=SimpleNamespace(env=self.env(user=member))):
            return self.controller._format_service(service, include_details=True)

    def test_flag_label_and_value_are_all_exposed(self):
        service = self._service(
            self._template('payload-tunnel', True, label='Public control URL'))
        service.write({'public_tunnel_url': HTTPS_ADDRESS})
        payload = self._detail(service)
        self.assertIs(
            payload['has_public_tunnel_address'], True,
            '旗標必須是 bool：前端 fail-closed 比較，None／字串會讓判斷漂移')
        self.assertEqual(payload['public_tunnel_url'], HTTPS_ADDRESS)
        self.assertEqual(payload['public_tunnel_label'], 'Public control URL')

    def test_flag_on_with_no_address_yet(self):
        """空位址仍要帶旗標：前端靠它畫出「尚未取得」而不是整格消失
        ——整格消失會讓剛填完 token 的租戶以為自己設定失敗。"""
        payload = self._detail(self._service(self._template('payload-empty', True)))
        self.assertIs(payload['has_public_tunnel_address'], True)
        self.assertIsNone(payload['public_tunnel_url'])
        self.assertIsNone(
            payload['public_tunnel_label'], '沒設標籤 ⇒ None，前端用通用的「對外位址」')

    def test_products_without_the_capability(self):
        payload = self._detail(self._service(self._template('payload-off', False)))
        self.assertIs(payload['has_public_tunnel_address'], False)
        self.assertIsNone(payload['public_tunnel_url'])

    def test_legacy_payload_keys_are_gone(self):
        """前端與後端同版發佈；舊鍵留著會讓有人又去讀它（第二個判準）。"""
        payload = self._detail(self._service(self._template('payload-legacy', True)))
        self.assertNotIn('has_public_mqtt_address', payload)
        self.assertNotIn('mqtt_public_url', payload)


@tagged('post_install', '-at_install')
class TestLegacyLedgerCarryOver(_Base):
    """`init()` 把改名前的 mqtt_public_url 搬到 public_tunnel_url。

    舊 column 只存在於從 ≤ .205 升上來的 DB；這裡在測試交易裡把它加回去模擬
    （PostgreSQL 的 DDL 在交易內，測試結束一起回捲）。
    """

    _TABLE = 'woow_paas_platform_cloud_service'

    def _add_legacy_column(self):
        self.env.cr.execute(
            'ALTER TABLE %s ADD COLUMN IF NOT EXISTS mqtt_public_url varchar' % self._TABLE)

    def _set_legacy(self, service, value):
        self.env.flush_all()
        self.env.cr.execute(
            'UPDATE %s SET mqtt_public_url = %%s WHERE id = %%s' % self._TABLE,
            (value, service.id))

    def _legacy(self, service):
        self.env.cr.execute(
            'SELECT mqtt_public_url FROM %s WHERE id = %%s' % self._TABLE, (service.id,))
        return self.env.cr.fetchone()[0]

    def _init(self):
        self.env['woow_paas_platform.cloud_service'].init()
        self.env.invalidate_all()

    def test_value_is_carried_over_and_legacy_cleared(self):
        self._add_legacy_column()
        service = self._service(self._template('carry', True))
        self._set_legacy(service, ADDRESS)
        self._init()
        self.assertEqual(service.public_tunnel_url, ADDRESS)
        self.assertIsNone(
            self._legacy(service),
            '搬完要清掉舊 column——否則下次 -u 會把一個已撤回的舊位址又搬回來')

    def test_existing_new_value_wins(self):
        """新欄位已經有值（例：升級後 poll 已經回寫過）就不覆蓋。"""
        self._add_legacy_column()
        service = self._service(self._template('carry-keep', True))
        service.write({'public_tunnel_url': HTTPS_ADDRESS})
        self._set_legacy(service, ADDRESS)
        self._init()
        self.assertEqual(service.public_tunnel_url, HTTPS_ADDRESS)

    def test_second_run_does_not_resurrect_a_retracted_address(self):
        self._add_legacy_column()
        service = self._service(self._template('carry-twice', True))
        self._set_legacy(service, ADDRESS)
        self._init()
        service.write({'public_tunnel_url': False})   # 之後 sidecar 撤回、poll 清掉
        self._init()
        self.assertFalse(service.public_tunnel_url)

    def test_fresh_install_without_legacy_column_is_a_noop(self):
        service = self._service(self._template('carry-none', True))
        self.env.cr.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = %s AND column_name = 'mqtt_public_url'", (self._TABLE,))
        if self.env.cr.fetchone():
            self.skipTest('這個 DB 還留著舊 column（從舊版升上來）；新裝路徑測不到')
        self._init()   # 不得炸
        self.assertFalse(service.public_tunnel_url)


@tagged('post_install', '-at_install')
class TestClientFallsBackOnOldOperator(TransactionCase):
    """平台先上、operator 還是舊版（沒有 /public-address）時，EMQX 位址不得整格消失。"""

    def _client(self, side_effect):
        client = PaaSOperatorClient('http://operator.invalid', 'k')
        client._request = MagicMock(side_effect=side_effect)
        return client

    def test_new_endpoint_first(self):
        client = self._client([{'public_url': HTTPS_ADDRESS}])
        self.assertEqual(
            client.get_public_address('paas-ws-x', 'svc-1')['public_url'], HTTPS_ADDRESS)
        self.assertEqual(
            client._request.call_args.args,
            ('GET', '/api/releases/paas-ws-x/svc-1/public-address'))

    def test_404_falls_back_to_the_legacy_endpoint(self):
        client = self._client([
            PaaSOperatorAPIError('API error: Not Found', status_code=404),
            {'public_url': ADDRESS},
        ])
        self.assertEqual(
            client.get_public_address('paas-ws-x', 'svc-1')['public_url'], ADDRESS)
        self.assertEqual(
            client._request.call_args.args,
            ('GET', '/api/releases/paas-ws-x/svc-1/mqtt-address'))

    def test_other_errors_are_not_masked(self):
        client = self._client([
            PaaSOperatorAPIError('API error: boom', status_code=500)])
        with self.assertRaises(PaaSOperatorAPIError):
            client.get_public_address('paas-ws-x', 'svc-1')
        self.assertEqual(client._request.call_count, 1)


@tagged('post_install', '-at_install')
class TestZhTwTranslations(TransactionCase):
    """服務頁那一格在中文介面要是中文（舊的 "Public MQTT" 說明一直沒有翻譯）。

    分兩層（docs/decisions/patterns/i18n-scope-data-values-vs-code-strings.md）：
    - 程式碼字串（OverviewTab 的 `_t()`：通用標題、尚未取得說明、複製）→ 走 .po；
    - 資料值（範本的 `public_tunnel_label`、gateway 兩格提示）→ seed 原文，⛔ 不進 .po。

    用 Odoo 自己的 `PoFileReader` 讀——runtime 就是用它（見 test_lang_zh_tw_activation_107）。
    """

    _DATA_VALUE_FIELDS = ('public_tunnel_label', 'gateway_api_key_hint', 'gateway_url_hint')

    def _entries(self):
        po = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'i18n', 'zh_TW.po')
        return list(PoFileReader(po))

    def test_overview_strings_are_translated(self):
        js = {e['src']: e['value'] for e in self._entries() if e.get('type') == 'code'}
        for src in ('Public address', 'Copy address'):
            self.assertTrue(js.get(src), 'OverviewTab 缺中文：%r' % src)
        self.assertTrue(
            any(src.startswith('Not available yet. This address needs your own ngrok')
                and value for src, value in js.items()),
            '「尚未取得」說明缺中文')

    def test_data_values_stay_out_of_po(self):
        """範本 record 的欄位內容不可翻譯、不進 .po（欄位 string／help 是 metadata，照常翻）。"""
        fields = self.env['woow_paas_platform.cloud_app_template']._fields
        names = {'woow_paas_platform.cloud_app_template,%s' % f
                 for f in self._DATA_VALUE_FIELDS}
        for f in self._DATA_VALUE_FIELDS:
            self.assertFalse(fields[f].translate, '%s 是資料值，不得 translate=True' % f)
        leaked = [e['src'] for e in self._entries()
                  if e.get('type') == 'model' and e.get('name') in names]
        self.assertEqual(leaked, [], '資料值不得出現在 zh_TW.po 的 model: 條目')

# -*- coding: utf-8 -*-
"""對外 MQTT 位址回讀（charts/emqx 0.1.0 ＋ operator `GET …/mqtt-address`）。

## 要解的問題

平台的 Cloudflare 通道只載 HTTP/WS，原生 MQTT 走租戶自己的 ngrok agent（chart 的
sidecar）。租戶若買了 ngrok 的**保留位址**，那個值本來就在 helm values 裡、平台早就
知道；真正需要回讀的是**隨機指派**那一種——agent 每次連線拿到的位址只有它自己知道。
chart 因此每 60 秒把當前位址印到 sidecar 自己的 container log，operator 從那裡讀回來。

## 三個守線

1. **能力 gate 只有一個事實源**：`cloud_app_template.has_public_mqtt_address`。
   model 的探測閘門、service payload、前端文案三處都讀它（repo-lint 第 7 條機械守著
   前端不得用 slug 做集合成員判斷）。本檔的 fixture 刻意把 slug 取成**反直覺**的組合
   ——旗標為 True 的 template slug 不叫 emqx、叫 emqx 的那個旗標是 False——
   有人改回用 slug 推導，兩支會同時變紅。

2. **「沒有位址」不是錯誤，但「有結論的沒有」要清帳本**：
   `_probe_mqtt_address` 的三態（None 沒探／'' 探過沒有／位址）直接對應
   `_release_facts` 的「只有探過才寫」。隧道斷掉之後還留著一個連不上的舊位址，
   比空白更糟：租戶會拿它去設定裝置然後一直連不上。

3. **附帶資訊不得打斷 poll**：operator 這一支掛掉時探測降級成「沒探」。
   同一輪 poll 還要回寫 state、就緒判定、chart 版本——讓服務頁上的一格附帶資訊
   把那些全部拖下水是完全不成比例的。
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from ..models.cloud_service import _internal_provision

ADDRESS = 'tcp://6.tcp.ngrok.io:22222'


class _Base(TransactionCase):

    def setUp(self):
        super().setUp()
        self.controller = PaasController()
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'MQTT readback workspace',
        })

    def _template(self, slug, has_public_mqtt_address):
        return self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'MQTT readback %s' % slug,
            'slug': slug,
            'category': 'automation',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example/%s' % slug,
            'helm_chart_version': '0.1.0',
            'mcp_enabled': False,
            'has_public_mqtt_address': has_public_mqtt_address,
        })

    def _service(self, template, **extra):
        vals = {
            'name': 'svc-mqtt-%s' % template.slug,
            'workspace_id': self.workspace.id,
            'template_id': template.id,
            'state': 'running',
            'helm_namespace': 'paas-ws-test',
            'helm_release_name': 'svc-mqtt-%s' % template.slug,
            'helm_values': json.dumps({}),
        }
        vals.update(extra)
        return _internal_provision(
            self.env['woow_paas_platform.cloud_service']).create(vals)

    def _client(self, public_url=ADDRESS, raises=None):
        client = MagicMock()
        if raises is not None:
            client.get_mqtt_address.side_effect = raises
        else:
            client.get_mqtt_address.return_value = {'public_url': public_url}
        return client


@tagged('post_install', '-at_install')
class TestCapabilityGateSingleSource(_Base):
    """旗標 ⟺ 能力。slug 刻意反直覺，擋「改回用 slug 推導」。"""

    def test_flag_on_a_slug_that_is_not_emqx(self):
        service = self._service(self._template('iot-broker-x', True))
        self.assertTrue(
            service._chart_has_public_mqtt_address(),
            '能力只看 template 旗標；slug 不叫 emqx 也必須成立')

    def test_emqx_named_slug_without_the_flag(self):
        service = self._service(self._template('emqx', False))
        self.assertFalse(
            service._chart_has_public_mqtt_address(),
            'slug 叫 emqx 但旗標關著 ⇒ 沒有能力（否則平台會為每輪 poll 打一次'
            '問不到東西的 operator 呼叫，並在服務頁長出永遠空白的一格）')

    def test_seeded_products(self):
        """上架中的兩個商品：EMQX 有、Nextcloud 沒有。"""
        self.assertTrue(
            self.env.ref('woow_paas_platform.cloud_app_emqx')
            .has_public_mqtt_address)
        self.assertFalse(
            self.env.ref('woow_paas_platform.cloud_app_nextcloud')
            .has_public_mqtt_address)


@tagged('post_install', '-at_install')
class TestProbeThreeStates(_Base):

    def test_no_capability_does_not_even_ask(self):
        service = self._service(self._template('no-mqtt', False))
        client = self._client()
        self.assertIsNone(service._probe_mqtt_address(client))
        client.get_mqtt_address.assert_not_called()

    def test_probed_and_present(self):
        service = self._service(self._template('has-mqtt', True))
        client = self._client(ADDRESS)
        self.assertEqual(service._probe_mqtt_address(client), ADDRESS)
        client.get_mqtt_address.assert_called_once_with(
            'paas-ws-test', 'svc-mqtt-has-mqtt')

    def test_probed_and_absent_is_empty_string_not_none(self):
        """租戶沒開／agent 還沒連上 ⇒ 有結論的「沒有」，要能把帳本清掉。"""
        service = self._service(self._template('has-mqtt-absent', True))
        self.assertEqual(
            service._probe_mqtt_address(self._client(None)), '',
            "探過但沒有位址必須回 ''（不是 None）——None 的語意是「這一輪沒探」，"
            '會讓斷掉的隧道留著一個連不上的舊位址')

    def test_operator_failure_degrades_to_not_probed(self):
        service = self._service(self._template('has-mqtt-err', True))
        with self.assertLogs(
                'odoo.addons.woow_paas_platform.models.cloud_service',
                level='WARNING'):
            got = service._probe_mqtt_address(
                self._client(raises=RuntimeError('operator down')))
        self.assertIsNone(
            got, 'operator 掛掉是「沒探」，不是「沒有位址」——不得清掉帳本，'
            '也不得讓例外冒出去打斷同一輪 poll 的 state 回寫')


@tagged('post_install', '-at_install')
class TestReleaseFactsFunnel(_Base):
    """欄位集合收斂在 `_release_facts`（#627 的紀律）。"""

    def _facts(self, service, mqtt_address):
        return service._release_facts(
            {'revision': 3, 'chart_version': '0.1.0'}, mqtt_address=mqtt_address)

    def test_not_probed_leaves_the_ledger_alone(self):
        service = self._service(self._template('facts-none', True))
        self.assertNotIn('mqtt_public_url', self._facts(service, None))

    def test_probed_present_writes_the_address(self):
        service = self._service(self._template('facts-set', True))
        self.assertEqual(
            self._facts(service, ADDRESS)['mqtt_public_url'], ADDRESS)

    def test_probed_absent_clears_the_address(self):
        service = self._service(self._template('facts-clear', True))
        self.assertIs(
            self._facts(service, '')['mqtt_public_url'], False,
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
        client = self._client(ADDRESS)
        self._poll(service, client, self._status())
        self.assertEqual(service.helm_revision, 3, '前提：這輪 poll 真的寫了')
        self.assertEqual(service.mqtt_public_url, ADDRESS)

    def test_steady_state_poll_does_not_write(self):
        """釘住既有形狀：state 沒推進時這條 poll 不回寫任何 release 事實。

        不是本功能造成的，但它決定了位址的新鮮度故事——所以明寫出來，而不是
        讓後人以為「poll 會一直更新位址」。`running` 服務的對帳由
        `_cron_reconcile_release_facts` 負責（那是唯一看得到 running 的路徑），
        見 TestReconcileCronRefreshesTheAddress。
        """
        service = self._service(self._template('poll-steady', True), state='running')
        client = self._client(ADDRESS)
        self._poll(service, client, self._status())
        self.assertFalse(
            service.mqtt_public_url,
            'state 未推進的 poll 不寫 facts；若哪天改成會寫，請一併檢視'
            'last_release_sync_at 的寫入頻率（見 _cron_reconcile_release_facts '
            'docstring 的 write_date 代價段）')

    def test_not_deployed_release_is_not_probed(self):
        """還沒 deploy 的服務裡根本還沒有 agent，問它只是白跑一趟。"""
        service = self._service(self._template('poll-pending', True), state='deploying')
        client = self._client(ADDRESS)
        self._poll(service, client, self._status(release_status='pending-install'))
        client.get_mqtt_address.assert_not_called()


@tagged('post_install', '-at_install')
class TestReconcileCronRefreshesTheAddress(_Base):
    """`running` 服務的位址新鮮度由每日對帳 cron 維持。

    ngrok 免費方案每次 agent 重連就換位址，而那種變動發生時服務正是 running
    ——poll 的 domain 只收轉移狀態，所以這條是唯一接得到的路徑（與 #627 (i)
    抓「非 Odoo 發起的 chart 變動」同一個結構性理由）。
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
        service.write({'mqtt_public_url': 'tcp://0.tcp.ngrok.io:11111'})
        self._run_cron(self._client(ADDRESS))
        service.invalidate_recordset()
        self.assertEqual(
            service.mqtt_public_url, ADDRESS,
            'agent 重連換了位址，帳本要跟上——否則租戶拿舊位址設定裝置會一直連不上')

    def test_a_dropped_tunnel_clears_a_stale_address(self):
        service = self._service(self._template('cron-clear', True), state='running')
        service.write({'mqtt_public_url': ADDRESS})
        self._run_cron(self._client(None))
        service.invalidate_recordset()
        self.assertFalse(
            service.mqtt_public_url,
            '隧道斷掉後必須清掉——留著一個連不上的舊位址比空白更糟')

    def test_products_without_the_capability_cost_nothing(self):
        """本 cron 掃全部 running 服務 ⇒ 沒這能力的商品不得多打一次 operator。"""
        self._service(self._template('cron-off', False), state='running')
        client = self._client(ADDRESS)
        self._run_cron(client)
        client.get_mqtt_address.assert_not_called()


@tagged('post_install', '-at_install')
class TestDetailPayload(_Base):
    """前端那一格的資料來源：旗標與值分開給。"""

    def _detail(self, service):
        member = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'MQTT detail viewer',
            'login': 'mqtt-detail-viewer-%s' % service.id,
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id, 'user_id': member.id, 'role': 'admin'})
        with patch('odoo.addons.woow_paas_platform.controllers.paas.request',
                   new=SimpleNamespace(env=self.env(user=member))):
            return self.controller._format_service(service, include_details=True)

    def test_flag_and_value_are_both_exposed(self):
        service = self._service(self._template('payload-mqtt', True))
        service.write({'mqtt_public_url': ADDRESS})
        payload = self._detail(service)
        self.assertIs(
            payload['has_public_mqtt_address'], True,
            '旗標必須是 bool：前端 fail-closed 比較，None／字串會讓判斷漂移')
        self.assertEqual(payload['mqtt_public_url'], ADDRESS)

    def test_flag_on_with_no_address_yet(self):
        """空位址仍要帶旗標：前端靠它畫出「尚未取得」而不是整格消失
        ——整格消失會讓剛填完 token 的租戶以為自己設定失敗。"""
        payload = self._detail(self._service(self._template('payload-empty', True)))
        self.assertIs(payload['has_public_mqtt_address'], True)
        self.assertIsNone(payload['mqtt_public_url'])

    def test_products_without_the_capability(self):
        payload = self._detail(self._service(self._template('payload-off', False)))
        self.assertIs(payload['has_public_mqtt_address'], False)
        self.assertIsNone(payload['mqtt_public_url'])

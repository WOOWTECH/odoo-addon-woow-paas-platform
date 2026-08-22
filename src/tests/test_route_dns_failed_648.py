# -*- coding: utf-8 -*-
"""#648 (a)：operator 對 expose 的**具名失敗欄位**，平台必須讀齊。

#484 為 Smart Home 補過這條縫（``TunnelCreateResponse.dns_failed`` → 不寫 active），
CloudService 這半漏接：`_execute_deploy` 拿到 install 回應後只讀 ``revision`` 與
``chart_version``，``route`` 這個物件在平台側**一次都沒被讀過**（`rg route_info src/`
與 `rg` 找「`.get` 帶 route」兩種寫法都是 0 命中）。於是 operator 把
``create_dns_record`` 的 ``CloudflareException`` 就地吞掉、端點照樣回 HTTP 201，租戶
拿到一個永遠不會解析的 hostname 而服務標成 running。

## 🔴 本檔最重要的一條是 A5，不是 A1

判準必須是「**明說的失敗**」而不是「東西不在」：

- operator 的 ``cloudflare_enabled`` **預設 false**（`config.py`），而且缺
  account_id／tunnel_id／api_token／domain 任一個會被強制關回 False；
- 舊 operator 根本沒有 ``status`` / ``dns_failed`` 這兩個欄位。

⇒ 一道「沒回 route 就當失敗」的守線，會讓沒開 Cloudflare 的環境**每一次部署都翻
error**——那比它要修的 bug 嚴重得多，而且只會在別人的沙盒或 e2e 上先炸。
A5（缺席不得 fail-closed）與 A4（舊 operator 不回鍵不得算失敗）是這條線的守門，
紅了代表守線被改成用缺席當判準。

載體：``docs/tracking/pitfalls/operator-route-none-conflates-disabled-and-failed.md``

Refs #484 #648
"""
import copy
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

from ..models.cloud_service import _internal_provision

#: `_execute_deploy` 用 **function-local import** 取 client ⇒ patch 的是**來源模組**
#: （`docs/decisions/patterns/unittest-mock-dual-namespace-patch.md`：頂部 import 才
#: 需要 patch 使用者所在的 namespace）。
_CLIENT_PATH = ('odoo.addons.woow_paas_platform.services.paas_operator'
                '.get_paas_operator_client')

#: 🔴 **fixture 不是手寫的，是 operator 實際序列化出來的**。
#:
#: 這一段是本檔存在的關鍵。Odoo 端不吃 operator 的碼，所以若測試自己組回應 dict，
#: 它釘住的只是作者**相信** operator 會回什麼——欄位名、`str, Enum` 的序列化形式、
#: `None` 欄位有沒有被丟掉，任何一項不同都會讓這裡全綠而 production 直接斷。那是
#: 這條修法兩半之間唯一沒有守線的接縫（CLAUDE.md 說的「unit-test mocks 蓋不住
#: contract drift」）。
#:
#: golden 由 operator 端 `tests/generate_route_contract_golden.py` **打真的端點**
#: 產生（FastAPI `TestClient` → `response.json()`），operator 端另有
#: `tests/test_route_contract_golden_648.py` 斷言「現在的序列化 == golden」⇒ 任一端
#: 改欄位，一端必紅。
#:
#: 重現：
#:     cd extra/paas-operator
#:     /opt/homebrew/bin/python3.11 tests/generate_route_contract_golden.py
_GOLDEN = json.loads(
    (Path(__file__).resolve().parent / 'fixtures'
     / 'route_contract_golden.json').read_text(encoding='utf-8'))

#: 連 hostname 都從 golden 取——手寫一份再拿去比對，等於自己跟自己比。
_HOSTNAME = _GOLDEN['ok_dns_healthy']['route']['hostname']
_PAAS_DOMAIN = _HOSTNAME.split('.', 1)[1]
_SUBDOMAIN = _HOSTNAME.split('.', 1)[0]


def _install_result(case):
    """取 golden 裡某一格的**完整** operator 回應（深拷貝，避免測試互相污染）。"""
    return copy.deepcopy(_GOLDEN[case])


class _DeployCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.paas_domain', _PAAS_DOMAIN)
        workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS 648a'})
        template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'T-648a',
            'slug': 'slug-648a',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'chart',
            'helm_chart_version': '1.0.0',
            'workload_type': 'container',
            'post_deploy_init_type': 'none',
        })
        self.svc = _internal_provision(
            self.env['woow_paas_platform.cloud_service']).create({
                'name': 'svc-648a',
                'workspace_id': workspace.id,
                'template_id': template.id,
                'state': 'pending',
                'helm_namespace': 'paas-ws-test-648a',
                'helm_release_name': 'svc-648a',
                'subdomain': _SUBDOMAIN,
            })

    def _deploy(self, install_result):
        client = MagicMock()
        client.install_release.return_value = install_result
        with patch(_CLIENT_PATH, return_value=client):
            self.svc._execute_deploy()
        return client


@tagged('post_install', '-at_install')
class TestExposeFailureIsFailClosed(_DeployCase):

    def test_a1_dns_failed_is_fail_closed(self):
        """🔴 A1：``dns_failed=True`` ⇒ hostname 不會解析 ⇒ 不得留在 deploying。

        修前 ``route`` 整個物件沒被讀過，服務照樣往 running 走。
        """
        self._deploy(_install_result('ok_dns_failed'))

        self.assertEqual(
            self.svc.state, 'error',
            'DNS record 沒建成卻讓部署繼續（state=%r）' % self.svc.state)

    def test_a2_error_message_names_the_hostname(self):
        """A2：訊息要指名是哪個 hostname，並給出可行動的出路。"""
        self._deploy(_install_result('ok_dns_failed'))

        msg = self.svc.error_message or ''
        self.assertIn(_HOSTNAME, msg, '訊息沒指名 hostname（回 %r）' % msg)
        self.assertIn(
            'administrator', msg,
            '訊息沒給出路——「壞了」而不說「怎麼辦」等於把問題丟給租戶')

    def test_a3_route_status_failed_is_fail_closed(self):
        """A3：整條 route 沒建成（找不到 service／CF API 掛了）也要 fail-closed。

        這比 DNS 失敗更嚴重：連 tunnel ingress 都沒有。用 golden 的
        ``failed_no_services`` 那格——它同時是唯一 ``service_url`` 真的是 ``null``
        的形狀（那條失敗發生在 service 解析完成之前）。
        """
        self._deploy(_install_result('failed_no_services'))

        self.assertEqual(self.svc.state, 'error', self.svc.state)

    def test_a6_ledger_fields_still_land_on_failure(self):
        """A6：判成 error 也不得丟掉帳本——helm release 是真的存在的。

        #484 的 SmartHome 把 tunnel 欄位照樣落地，理由是「資源已在計費，丟了就是
        永久孤兒」。CS 的錨點（subdomain／helm_release_name／helm_namespace）在
        **建立當下**就寫好了，所以孤兒風險不同構；但 revision／chart_version／
        deployed_at 仍然是**已經發生的事實**，不因判定失敗而消失。
        """
        self._deploy(_install_result('ok_dns_failed'))

        self.assertEqual(self.svc.state, 'error')
        self.assertEqual(self.svc.helm_revision, 1)
        self.assertEqual(self.svc.helm_chart_version, '1.0.0')
        self.assertTrue(self.svc.deployed_at, 'deployed_at 沒落地')


@tagged('post_install', '-at_install')
class TestExposeAbsenceIsNotFailure(_DeployCase):
    """🔴 守門組：**缺席不是失敗**。這兩條紅了代表守線被改成用缺席當判準。"""

    def test_a5_missing_route_is_not_a_failure(self):
        """🔴 A5：回應完全沒有 ``route`` ⇒ **不得** fail-closed。

        它同時涵蓋兩種正常情形：這個部署沒開 Cloudflare（operator 的
        ``cloudflare_enabled`` 預設 **false**），或 operator 舊到沒有這個欄位。
        把缺席當失敗，會讓那種環境**每一次部署都翻 error**——比原 bug 嚴重得多，
        而且 prod 的 CF 是開的、只會在別人的沙盒或 e2e 上先炸。
        """
        self._deploy(_install_result('cloudflare_disabled'))

        self.assertNotEqual(
            self.svc.state, 'error',
            '沒有 route 被當成失敗 ⇒ 沒開 Cloudflare 的環境每次部署都會翻 error')

    def test_a4_absent_dns_failed_key_is_treated_as_ok(self):
        """A4：舊 operator 不回 ``dns_failed`` / ``status`` 這兩個鍵 ⇒ 不得算失敗。

        鏡射 ``test_named_failure_fields_484.py`` 的同名對照組。相容性在本 repo 靠
        「Optional ＋ 預設值 ＋ 呼叫端 `.get`」，不是靠 schema 版本協商。
        """
        payload = _install_result('ok_dns_healthy')
        del payload['route']['dns_failed']
        del payload['route']['status']

        self._deploy(payload)

        self.assertNotEqual(self.svc.state, 'error', self.svc.state)

    def test_healthy_route_does_not_trip_the_gate(self):
        """對照組：一切正常時行為與修前相同（不寫 error、留給 poll 決定）。"""
        self._deploy(_install_result('ok_dns_healthy'))

        self.assertNotEqual(self.svc.state, 'error', self.svc.state)
        self.assertFalse(self.svc.error_message)

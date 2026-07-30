# -*- coding: utf-8 -*-
"""#484：operator 在 HTTP 200 下用**具名欄位**回報的失敗，呼叫端必須讀齊。

#477／#481 收斂的是 ``success: false`` 那一族。同一族還有另一種載體：operator 把失敗
放進**具名欄位**而不是 ``success``，於是「呼叫端沒檢查」不再表現為漏了一個 ``else``，
而是**那個欄位在平台側一次都沒被讀過**（`grep -rn <field> src/` 零命中）。兩處都在租戶
控制面不健康的時刻失效——與 #477 縫 (a)「保護最該生效的那一刻自己讓路」同一個性質。

縫 1 — ``dns_failed``（``extra/paas-operator/src/api/tunnels.py:84-99``）
    建 tunnel 時 DNS record 建立失敗，``CloudflareException`` 被**就地吞掉**，端點照樣
    回 **HTTP 200**，只用 ``dns_failed=True`` 標記。schema 明文要求呼叫端據此
    「alert/re-sync rather than silently treating the tunnel as fully reachable」。
    `smart_home.action_provision` 原本無條件寫 ``state='active'`` ＋
    ``tunnel_route=https://<host>`` → 租戶拿到一個 NXDOMAIN 的連結。而 operator 註解
    假設的「rely entirely on a later re-sync to self-heal」在這裡**不成立**：``src/data/``
    底下沒有任何 smart-home cron。
    對照組（證明是漏接而非取捨）：同一個 operator 對 route 同步路徑的 DNS 失敗是
    **raise**（``api/routes.py`` 明文 ``raise_on_error=True so a CF API error is NOT
    silently ...``）→ 同一件事在 create 路徑吞成 200-flag、在 routes 路徑 fail-loud。

縫 2 — ``pod_retrieval_error``（``extra/paas-operator/src/api/releases.py:535-543``）
    抓 pod 失敗時 ``pods`` 保持空陣列、錯誤放進 ``pod_retrieval_error``，回 HTTP 200。
    ``_is_container_ready`` 只看 ``pods``，空陣列 → ``not pods`` → **True** → 服務翻
    ``running``，而平台從來沒看過任何一顆 pod。

    ⚠ **與既有 follow-up F-423-1 的分界（別把兩件事混成一件）**：F-423-1 記載的是
    「**正常過渡期**的空 pod 列表」（helm 已 deployed 但 controller 還沒把 pod 建出來），
    並以「改 fail-closed 會讓所有現存租戶服務多卡一輪 poll」為理由刻意 v1 不修。本檔
    **不動那個決議**（見 `test_empty_pods_without_error_keeps_f423_1_behaviour`），只修
    「pod 查詢**本身**失敗」這個子情形——它在正常過渡期成本為零，因為那時
    ``pod_retrieval_error`` 是 ``None``。同一份 operator schema 自己也把規則寫在容器
    欄位上（``vmi_retrieval_error`` 的說明：「Mirrors pod_retrieval_error: the caller
    MUST NOT read an empty vmis list as 'no VM' while this is set」），但實作當初只落在
    VM 分支。

Refs #477 #481 #423
"""
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

# 兩個 model 都在**自己的 module namespace** 頂部 `from ..services.paas_operator import
# get_paas_operator_client`，所以 patch 目標是使用者所在的 module，不是定義處
# （docs/decisions/patterns/unittest-mock-dual-namespace-patch.md）。
_SH_CLIENT = (
    'odoo.addons.woow_paas_platform.models.smart_home.get_paas_operator_client')
_CTRL_CLIENT = (
    'odoo.addons.woow_paas_platform.controllers.paas.get_paas_operator_client')


# ====================================================================== #
#  縫 1：dns_failed                                                      #
# ====================================================================== #

def _tunnel_result(**overrides):
    """組一份 operator ``POST /api/tunnels`` 形狀的回應（預設 DNS 正常）。"""
    body = {
        'tunnel_id': 'tun-484',
        'tunnel_name': 'paas-prod-sm-484',
        'tunnel_token': 'tok-484',
        'hostname': 'sh-484.woowtech.io',
        'dns_record_id': 'dns-484',
        'dns_failed': False,
    }
    body.update(overrides)
    return body


class _SmartHomeCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS 484'})
        self.sh = self.env['woow_paas_platform.smart_home'].create({
            'name': 'SH 484',
            'reference_id': 'sh-484',
            'workspace_id': self.workspace.id,
        })

    @staticmethod
    def _client(**overrides):
        client = MagicMock()
        client.create_tunnel.return_value = _tunnel_result(**overrides)
        return client

    def _provision(self, client):
        with patch(_SH_CLIENT, return_value=client):
            self.sh.action_provision()


@tagged('post_install', '-at_install')
class TestDnsFailedIsFailClosed(_SmartHomeCase):
    """operator 回 ``dns_failed=True`` 時，開通不得宣告成功。"""

    def test_dns_failed_must_not_mark_active(self):
        """🔴 縫 1 的核心：DNS 沒建成 → hostname 不會解析 → 不能標 active。

        修前：``dns_failed`` 完全沒被讀，state 直接寫 ``active``，租戶拿到一個
        NXDOMAIN 的 ``tunnel_route``。
        """
        self._provision(self._client(dns_failed=True))

        self.assertNotEqual(
            self.sh.state, 'active',
            'DNS record 沒建出來就宣告 active＝把不可達的服務標成可用（state=%r）'
            % self.sh.state)
        self.assertEqual(self.sh.state, 'error', self.sh.state)

    def test_dns_failed_surfaces_why_and_which_host(self):
        """訊息要讓租戶／維運看得出「DNS 沒好」以及是哪個 hostname。

        只把 state 翻成 error 不夠——維運看到 error 卻不知道 tunnel 已經存在、
        該去補哪一筆 DNS，就只能重跑（而重跑正是會漏掉計費 tunnel 的動作）。
        """
        self._provision(self._client(dns_failed=True))

        msg = self.sh.error_message or ''
        self.assertIn('DNS', msg, '訊息要指名是 DNS 的問題（回 %r）' % msg)
        self.assertIn(
            self.sh.subdomain or '<none>', msg,
            '訊息要帶上是哪個 hostname 缺 DNS（回 %r）' % msg)

    def test_dns_failed_keeps_the_billed_tunnel_anchored(self):
        """tunnel 已經在 CF 誕生且在計費——``tunnel_id`` 絕不能丟。

        丟掉就是永久孤兒（token 有效、持續計費、DB 沒有任何 row 指向它），與
        F-ENV-4 的孤兒守線同一個要求。
        """
        self._provision(self._client(dns_failed=True))

        self.assertEqual(
            self.sh.tunnel_id, 'tun-484',
            '失敗時保留 tunnel_id 才有重試／人工收尾的錨點')
        self.assertEqual(self.sh.tunnel_name, 'paas-prod-sm-484')

    def test_retry_after_dns_failure_does_not_create_a_second_tunnel(self):
        """🔴 縫 1 的第二半：這個修法把單段 provision 變成**雙結果**，
        於是必須補回 stored-id guard。

        ``docs/decisions/patterns/two-stage-remote-provision-idempotency.md`` 的原話：
        「單段 method 的『免 guard 安全』是它的單段性換來的，不是天生的；一旦加第二段，
        這份安全就得用 guard 重新買回來。」該 doc 當初正是拿 SmartHome 當「單段、
        免 guard 也安全」的對照組——本 PR 打破了那個前提（``error`` 從此可能與一條
        活著的 tunnel 共存），所以 guard 是修法的必要組成，不是附帶重構。

        修前：``action_provision`` 對 ``tunnel_id`` 沒有任何 guard，重試會再 POST 一條
        全新 tunnel（operator 的 ``create_tunnel`` 每次都帶新 ``tunnel_secret``，**不**
        同名冪等），第一條就此失去 DB 引用。
        """
        client = self._client(dns_failed=True)
        self._provision(client)
        self.assertEqual(client.create_tunnel.call_count, 1)

        with self.assertRaises(UserError):
            self._provision(client)

        self.assertEqual(
            client.create_tunnel.call_count, 1,
            '重試又建了一條 tunnel＝第一條變成沒人看管的計費孤兒')
        self.assertEqual(
            self.sh.tunnel_id, 'tun-484', '重試不得改寫既有 tunnel_id')

    def test_provision_on_an_active_smart_home_is_also_guarded(self):
        """同一道 guard 順帶關掉一個既存的孤兒洞。

        ``/api/smart-homes`` 的 ``action='provision'`` **沒有 state 閘**（只驗
        OWNER/ADMIN ＋ 訂閱），所以對一個已經 ``active`` 的 Smart Home 再打一次
        provision，修前會建第二條 tunnel 並蓋掉 ``tunnel_id``。
        """
        client = self._client()
        self._provision(client)
        self.assertEqual(self.sh.state, 'active')

        with self.assertRaises(UserError):
            self._provision(client)

        self.assertEqual(client.create_tunnel.call_count, 1)


@tagged('post_install', '-at_install')
class TestHealthyProvisionUnchanged(_SmartHomeCase):
    """反過度修復錨：DNS 正常的開通必須照舊成功（修前修後都綠）。"""

    def test_dns_ok_still_becomes_active(self):
        self._provision(self._client())

        self.assertEqual(self.sh.state, 'active')
        self.assertEqual(self.sh.tunnel_id, 'tun-484')
        self.assertTrue(self.sh.tunnel_route.startswith('https://'))
        self.assertFalse(self.sh.error_message)

    def test_absent_dns_failed_key_is_treated_as_ok(self):
        """舊版 operator 不回 ``dns_failed`` 這個鍵——不得因此被當成失敗。

        （反向的 fail-closed 過頭：把「欄位不存在」讀成 True 會讓所有既有部署
        一開通就變 error。）
        """
        result = _tunnel_result()
        del result['dns_failed']
        client = MagicMock()
        client.create_tunnel.return_value = result

        self._provision(client)

        self.assertEqual(self.sh.state, 'active')


# ====================================================================== #
#  縫 2：pod_retrieval_error                                             #
# ====================================================================== #

def _status(release_status='deployed', pods=None, **extra):
    """組一份 operator ``get_status`` 形狀的回應（沿用 W5a 測試的形狀）。"""
    body = {
        'release': {'status': release_status, 'revision': 1},
        'pods': pods if pods is not None else [],
        'pod_retrieval_error': None,
        'vm_included': False,
        'vmis': None,
        'datavolumes': None,
        'vmi_retrieval_error': None,
        'datavolume_retrieval_error': None,
    }
    body.update(extra)
    return body


class _ReadinessCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS 484 readiness'})
        template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'T-484',
            'slug': 'slug-484',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'chart',
            'helm_chart_version': '1.0.0',
            'workload_type': 'container',
            'post_deploy_init_type': 'none',
        })
        self.svc = self.env['woow_paas_platform.cloud_service'].create({
            'name': 'svc-484',
            'workspace_id': self.workspace.id,
            'template_id': template.id,
            'state': 'deploying',
            'helm_namespace': 'paas-ws-test-484',
            'helm_release_name': 'svc-484',
        })


@tagged('post_install', '-at_install')
class TestPodRetrievalErrorIsFailClosed(_ReadinessCase):
    """pod 查詢失敗 → 狀態未知 → 不得判定就緒（與 VM 分支對稱）。"""

    def test_pod_retrieval_error_is_fail_closed(self):
        """🔴 縫 2 的核心：``pods=[]`` ＋ ``pod_retrieval_error`` → 不可就緒。

        修前 ``not pods`` 回 True，服務直接翻 running，而平台從來沒看過任何 pod。
        """
        ready, detail = self.svc._is_release_ready(_status(
            pods=[], pod_retrieval_error='Forbidden: cannot list pods'))

        self.assertFalse(
            ready,
            'pod 查不到就宣告就緒＝在租戶控制面不健康時讓路（detail=%r）' % detail)
        self.assertIn(
            'Forbidden', detail, '理由要帶上 operator 的原文（回 %r）' % detail)
        self.assertTrue(
            detail.startswith('HARD:'),
            'RBAC/API 失敗不會自己好，必須走 HARD 分支升 warning（回 %r）' % detail)

    def test_pod_retrieval_error_keeps_service_deploying(self):
        """model poll 層：不得從 deploying 翻 running。"""
        client = MagicMock()
        client.get_status.return_value = _status(
            pods=[], pod_retrieval_error='connection reset')

        self.svc._poll_and_update_status(client)

        self.assertEqual(
            self.svc.state, 'deploying',
            '查不到 pod 卻翻 %r' % self.svc.state)

    def test_pod_retrieval_error_fail_closed_at_controller_layer_too(self):
        """controller 呼叫點走同一個單一事實源（W5a 收斂的驗收）。

        只修 model 層不夠：租戶打開前台服務詳情頁，controller 那條 poll 會把狀態
        覆寫回 running（#423 M4 的教訓）。
        """
        from odoo.addons.woow_paas_platform.controllers.paas import PaasController

        client = MagicMock()
        client.get_status.return_value = _status(
            pods=[], pod_retrieval_error='Forbidden')
        # ⚠ 不能 patch(...request)：odoo 的 request 是 werkzeug LocalProxy，mock 在
        # __enter__ 期就會 introspect 它而炸 "object is not bound"。用 new= 塞替身。
        fake_request = MagicMock()
        fake_request.env = self.env
        module = 'odoo.addons.woow_paas_platform.controllers.paas'
        with patch(f'{module}.get_paas_operator_client', return_value=client), \
                patch(f'{module}.request', new=fake_request):
            PaasController()._update_service_status(self.svc)

        self.assertEqual(self.svc.state, 'deploying', self.svc.state)


@tagged('post_install', '-at_install')
class TestF4231DecisionUntouched(_ReadinessCase):
    """反過度修復錨：F-423-1 那個**刻意保留**的 fail-open 不許被順手改掉。"""

    def test_empty_pods_without_error_keeps_f423_1_behaviour(self):
        """``pods=[]`` 而**沒有** ``pod_retrieval_error`` → 維持現行 True。

        這是 F-423-1 的決議：容器型有一個合法短暫狀態是「release 已 deployed 但
        pod 還沒被 controller 建出來」，改 fail-closed 會讓**所有**現存租戶服務多卡
        一輪 poll。本 PR 只收「查詢失敗」那個子情形（該欄位有值時），正常過渡期
        成本為零。這條紅了代表修過頭。
        """
        ready, _ = self.svc._is_release_ready(_status(pods=[]))

        self.assertTrue(
            ready, 'F-423-1 的 characterization 行為被改掉了（修過頭）')

    def test_ready_pods_still_ready(self):
        ready, _ = self.svc._is_release_ready(_status(
            pods=[{'name': 'p', 'phase': 'Running', 'ready': '1/1'}]))

        self.assertTrue(ready)

    def test_unready_pods_still_not_ready(self):
        ready, _ = self.svc._is_release_ready(_status(
            pods=[{'name': 'p', 'phase': 'Running', 'ready': '0/1'}]))

        self.assertFalse(ready)

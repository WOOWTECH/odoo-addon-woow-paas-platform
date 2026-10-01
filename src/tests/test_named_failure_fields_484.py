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

    ⚠ **與既有 follow-up F-423-1 的分界（寫於 #484 當時，現已合流）**：F-423-1 記載的是
    「**正常過渡期**的空 pod 列表」（helm 已 deployed 但 controller 還沒把 pod 建出來），
    並以「改 fail-closed 會讓所有現存租戶服務多卡一輪 poll」為理由刻意 v1 不修。#484
    當時**不動那個決議**，只修「pod 查詢**本身**失敗」這個子情形——它在正常過渡期成本
    為零，因為那時 ``pod_retrieval_error`` 是 ``None``。同一份 operator schema 自己也把
    規則寫在容器欄位上（``vmi_retrieval_error`` 的說明：「Mirrors pod_retrieval_error:
    the caller MUST NOT read an empty vmis list as 'no VM' while this is set」），但實作
    當初只落在 VM 分支。

    **後續**：F-423-1 Step 1（2026-07-31）已把 ``pods=[]`` 那條一併翻成 fail-closed
    ——評估推翻了原本的兩個前提（VM 合取並不依賴它；影響面不是「所有現存租戶服務」），
    並找到已發生的實例（#456）。所以現在兩條路的處置一致，差別只剩理由字串。翻轉的
    完整論證與三層守線在 ``test_cloud_service_poll_vm_branch.py``；本檔保留原敘述是
    為了讓「當時為什麼只修一半」這個判斷仍可考古。

Refs #477 #481 #423
"""
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged
from ..models.cloud_service import _internal_provision
from .operator_contract import status_response  # #1052：get_status 形狀的唯一定義處
from .smart_home_fixture import sh_model  # #524：建立閘 fixture helper

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
        self.sh = sh_model(self.env).create({
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

        Smart Home 開通重試之後：guard 分支從 ``raise`` 改成**在同一條 tunnel 上收斂**
        （``_converge_existing_tunnel`` → ``sync_tunnel_routes``）。本條守的核心不變
        ——``create_tunnel`` 只能被呼叫一次、``tunnel_id`` 不得被改寫——只是重試現在
        會成功而不是被拒。完整矩陣見 ``test_sh_provision_retry.py``。
        """
        client = self._client(dns_failed=True)
        self._provision(client)
        self.assertEqual(client.create_tunnel.call_count, 1)

        self._provision(client)

        self.assertEqual(
            client.create_tunnel.call_count, 1,
            '重試又建了一條 tunnel＝第一條變成沒人看管的計費孤兒')
        self.assertEqual(
            self.sh.tunnel_id, 'tun-484', '重試不得改寫既有 tunnel_id')
        self.assertEqual(
            client.sync_tunnel_routes.call_count, 1,
            '重試要在既有 tunnel 上補 ingress／DNS，而不是什麼都不做')
        self.assertEqual(self.sh.state, 'active')

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
    """組一份 operator ``get_status`` 形狀的回應（沿用 W5a 測試的形狀）。

    #1052：形狀的唯一定義處是 ``operator_contract.status_response``（與本檔原本手抄的
    8 key 版本逐 key 相同）。
    """
    return status_response(release_status, pods, **extra)


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
        self.svc = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
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


@tagged('post_install', '-at_install')
class TestContainerReadinessAnchors(_ReadinessCase):
    """容器就緒判定的其餘輸入型：兩種「不空但不健康」的 pods 必須維持 fail-closed。

    本 class 原名 ``TestF4231DecisionUntouched``，鎖的是 #484 當時**刻意不動**的
    F-423-1 fail-open（``pods=[]`` 回 True）。F-423-1 Step 1（2026-07-31）已把那條
    翻成 fail-closed，對應的斷言隨之搬到
    ``test_cloud_service_poll_vm_branch.py``（判定 / model poll / controller poll
    三層各一條）。這裡留下的是它的**鄰居**——它們在兩次改動中語意都沒變，正好用來
    證明翻轉沒有波及其他輸入型。
    """

    def test_empty_pods_is_fail_closed(self):
        """``pods=[]`` 而**沒有** ``pod_retrieval_error`` → 也不可就緒（Step 1）。

        與 ``pod_retrieval_error`` 那條的差別只在**理由**：那條是「查不到」，這條是
        「查到了，而且真的一顆都沒有」。兩者都不構成「服務可用」的證據，所以 Step 1
        之後處置一致。詳細翻轉理由見
        ``test_cloud_service_poll_vm_branch.py::test_5_container_with_no_pods_is_fail_closed``。
        """
        ready, _ = self.svc._is_release_ready(_status(pods=[]))

        self.assertFalse(ready)

    def test_ready_pods_still_ready(self):
        ready, _ = self.svc._is_release_ready(_status(
            pods=[{'name': 'p', 'phase': 'Running', 'ready': '1/1'}]))

        self.assertTrue(ready)

    def test_unready_pods_still_not_ready(self):
        ready, _ = self.svc._is_release_ready(_status(
            pods=[{'name': 'p', 'phase': 'Running', 'ready': '0/1'}]))

        self.assertFalse(ready)

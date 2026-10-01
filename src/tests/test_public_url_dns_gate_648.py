# -*- coding: utf-8 -*-
"""#648：CloudService 翻 ``running`` 不得早於 Public URL 的 DNS 落地。

## 兩條縫（同因不同表徵，本檔只鎖 (b)；(a) 在 ``test_route_dns_failed_648.py``）

CS 的部署鏈把 DNS 當 best-effort：``cloudflare.py`` 的 ``create_route`` 把
``create_dns_record`` 的 ``CloudflareException`` **就地吞掉**（註解原文
「Continue with route creation - DNS might be managed manually」）後照樣建 tunnel
ingress，而 ``_is_release_ready`` 全文沒有任何 DNS 判定。於是 helm deployed ＋ pods
ready 一滿足就寫 ``state='running'``，租戶看到綠燈、點進去拿 ``ERR_NAME_NOT_RESOLVED``。

- **(a) 永久縫**：``create_dns_record`` 真的失敗 ⇒ hostname 永遠不會解析。#484 已為
  Smart Home 補上 ``dns_failed`` fail-closed，CS 這半漏接。
- **(b) 暫時縫（本檔）**：``create_dns_record`` 成功，但 DNS 落地比 pods ready 慢幾秒到
  幾十秒。``dns_failed`` 全程 ``False``，#484 那條守線不會觸發。

## 為什麼閘放在 ``_is_release_ready`` 而不是「寫 running 的那幾個分支」

``_is_release_ready`` 是 #423 W5a 刻意收斂的**單一事實源**（model 的
``_poll_and_update_status`` 與 controller 的 ``_update_service_status`` 共用）。把閘移到
running 的 write 分支要改 6 個點，等於再造一次 #627 的形狀。另外
``elif needs_init and original_state == 'initializing'`` 分支**每輪都會重跑**
``_run_post_deploy_init``（#1257 起的單一 init 入口）——閘放 write 上，服務會停在 ``initializing`` 反覆進 init
去撞 ``init_retries`` 上界（5 次翻 error），繞回同一個「健康服務被標 error」的結果。

## 🔴 為什麼窗口必須綁在 ``_transition_anchor()`` 上（本檔最重要的不變式）

#489 的 age timeout（``_check_transition_timeout``）從 ``_transition_anchor()``
＝``max(deployed_at, last_upgraded_at)`` 起算，而 ``deployed_at`` 在 **helm install 一
回來就寫**（``_execute_deploy``），不是「開始等 DNS」的時間點。所以若 DNS 閘自己另計時
（例如「從第一次探測失敗起算 90 秒」），「pods 花 29 分鐘 ready ＋ 閘再等 90 秒」會讓
age 超過 ``deploy_timeout_minutes``（預設 30 分）⇒ 服務被寫成 ``error``，而 ``error``
不在 ``_TIMEOUT_GOVERNED_STATES`` 裡、**沒有任何 poll 分支救得回來**。那比原 bug 嚴重。

修法：**閘只在 ``age(anchor) < 有效寬限期`` 這個窗口內可能擋，窗口外恆放行**，且有效
寬限期在讀取時 clamp 到 ``deploy_timeout − 60 秒`` 以下。兩者合起來可證明：閘擋住時
age 必定小於 timeout ⇒ **DNS 閘永遠不可能造成 ``error``**。``test_gate_can_never_cause
_age_timeout`` 與 ``test_effective_grace_is_clamped_below_deploy_timeout`` 各鎖一半。

## 這個修法**修不掉**什麼（PR 也要寫）

伺服器端探測回「可解析」時，租戶仍可能看到 NXDOMAIN——issue #648 的 2026-08-12 現場即為
此：三條 resolver 當下都查得到、``curl --resolve`` 直連拿到 302，租戶端是**本機負面 DNS
快取**，純等待與重整都不會好。(b) 蓋的是真伺服器端傳播窗口，蓋不到那一半；放行時寫下的
提示文案必須涵蓋它。

## 被測契約（實作要提供的東西）

- ``src/services/dns_probe.py`` 的 ``resolve_state(hostname, timeout=...)``
  → ``'ok'`` / ``'missing'`` / ``'unknown'``。
- ``cloud_service.py`` 以 ``from ..services import dns_probe`` 引入、呼叫
  ``dns_probe.resolve_state(...)``。**只有這一個 lookup namespace**，所以 patch
  ``...services.dns_probe.resolve_state`` 涵蓋全部呼叫者
  （見 ``docs/decisions/patterns/unittest-mock-dual-namespace-patch.md``：module-attr
  形式要 patch 來源模組）。每條依賴 mock 的測試都斷言它**真的被呼叫過**，避免 mock 變死碼。
- ``cloud_service`` 新欄位 ``public_url_dns_unverified``（Boolean）。
- 兩個 ``ir.config_parameter``：``woow_paas_platform.dns_grace_seconds``（預設 90）與
  ``woow_paas_platform.dns_gate_enabled``（預設開；prod kill switch）。

Refs #484 #423 #489 #627
"""
import time
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

from ..models.cloud_service import _internal_provision
from .operator_contract import status_response  # #1052：get_status 形狀的唯一定義處

#: 被測碼以 ``dns_probe.resolve_state(...)`` 形式（module attribute）查名字，
#: 所以 patch 來源模組即涵蓋所有呼叫者。
_RESOLVE = 'odoo.addons.woow_paas_platform.services.dns_probe.resolve_state'
_CTRL_MODULE = 'odoo.addons.woow_paas_platform.controllers.paas'

_PAAS_DOMAIN = 'woowtech.io'
_SUBDOMAIN = 'paas-cs-648test-abcd1234'
_HOSTNAME = '%s.%s' % (_SUBDOMAIN, _PAAS_DOMAIN)


def _status(release_status='deployed', pods=None, **extra):
    """operator ``get_status`` 形狀（沿用 W5a／#484 測試的形狀）。

    預設帶一顆健康的 pod ⇒ **pod/VM 那層必定通過**，這樣任何 not-ready 都只可能
    來自本檔要加的 DNS 閘。否則測試會紅在錯的分支上。

    #1052：形狀的唯一定義處是 ``operator_contract.status_response``；本檔只保留
    「預設一顆健康 pod」這個本檔專屬的預設值。
    """
    if pods is None:
        pods = [{'name': 'p', 'phase': 'Running', 'ready': '1/1'}]
    return status_response(release_status, pods, **extra)


class _DnsGateCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.paas_domain', _PAAS_DOMAIN)
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS 648'})
        self.template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'T-648',
            'slug': 'slug-648',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'chart',
            'helm_chart_version': '1.0.0',
            'workload_type': 'container',
            'post_deploy_init_type': 'none',
        })
        self.svc = self._make_service(subdomain=_SUBDOMAIN)

    #: `_make_service` 的流水號。``subdomain`` 有 UNIQUE constraint，而 `setUp` 已經
    #: 用掉 `_SUBDOMAIN` ⇒ 任何再建一台的測試若沿用同一個值，會在 flush 時撞
    #: ``UniqueViolation`` **而且死在建立階段、根本走不到斷言**。第一次跑 RED 時
    #: 實際發生過：6 條測試全部紅在這裡，紅得像實作有問題，其實零資訊。
    _subdomain_seq = 0

    def _make_service(self, anchor_seconds_ago=5, subdomain=None, **vals):
        """建一台 ``deploying`` 中的服務，錨點預設在 5 秒前（＝窗口內）。

        ``subdomain=None`` ⇒ 自動配一個唯一值（見 `_subdomain_seq`）。要斷言
        hostname 的測試請用 `setUp` 建的 ``self.svc``（它固定是 `_SUBDOMAIN`）。
        """
        if subdomain is None:
            type(self)._subdomain_seq += 1
            subdomain = '%s-%d' % (_SUBDOMAIN, type(self)._subdomain_seq)
        base = {
            'name': 'svc-648',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'state': 'deploying',
            'helm_namespace': 'paas-ws-test-648',
            'helm_release_name': 'svc-648',
            'subdomain': subdomain,
            # 錨點直接寫在過去——沿用 test_cloud_service_deploy_timeout.py 的既有
            # 招式（**不** patch `fields.Datetime.now`：那份 docstring 明講理由是
            # 避免 patch 失效後靜默變死碼的假綠）。
            'deployed_at': fields.Datetime.now() - timedelta(
                seconds=anchor_seconds_ago),
        }
        base.update(vals)
        return _internal_provision(
            self.env['woow_paas_platform.cloud_service']).create(base)

    def _poll(self, service=None, resolve='missing'):
        """跑一輪 model 層 poll，並回傳 resolve mock 供斷言它真的被呼叫過。"""
        service = service or self.svc
        client = MagicMock()
        client.get_status.return_value = _status()
        with patch(_RESOLVE, return_value=resolve) as m:
            service._poll_and_update_status(client)
        return m

    def _poll_controller(self, service=None, resolve='missing'):
        from odoo.addons.woow_paas_platform.controllers.paas import PaasController
        service = service or self.svc
        client = MagicMock()
        client.get_status.return_value = _status()
        # ⚠ 不能 patch(...request)：odoo 的 request 是 werkzeug LocalProxy，mock 在
        # __enter__ 期就會 introspect 它而炸。用 new= 塞替身（同 #484 測試）。
        fake_request = MagicMock()
        fake_request.env = self.env
        with patch(_RESOLVE, return_value=resolve) as m, \
                patch('%s.get_paas_operator_client' % _CTRL_MODULE,
                      return_value=client), \
                patch('%s.request' % _CTRL_MODULE, new=fake_request):
            PaasController()._update_service_status(service)
        return m


# ====================================================================== #
#  判定層                                                                 #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestPublicUrlDnsGateVerdict(_DnsGateCase):
    """``_is_release_ready`` 在窗口內必須把「DNS 尚未落地」判成未就緒。"""

    def test_unresolvable_public_url_is_not_ready_inside_window(self):
        """🔴 (b) 的核心：pods 全 ready 但 Public URL 解析不到 ⇒ 不得就緒。

        修前 ``_is_release_ready`` 全文無 DNS 判定，這裡回 ``True``。
        """
        with patch(_RESOLVE, return_value='missing') as m:
            ready, detail = self.svc._is_release_ready(_status())

        self.assertTrue(m.called, 'DNS 探測根本沒被呼叫 ⇒ mock 是死碼，這條測試無效')
        self.assertFalse(
            ready,
            'Public URL 還解析不到就宣告就緒＝綠燈搶先 DNS（detail=%r）' % detail)
        self.assertIn(
            _HOSTNAME, detail,
            '理由要指名是哪個 hostname 還沒落地（回 %r）' % detail)

    def test_resolved_public_url_is_ready(self):
        """characterization：DNS 解析得到 ⇒ 行為與修前完全相同。"""
        with patch(_RESOLVE, return_value='ok') as m:
            ready, _ = self.svc._is_release_ready(_status())

        self.assertTrue(m.called)
        self.assertTrue(ready)

    def test_probe_is_given_an_fqdn_with_trailing_dot(self):
        """🔴 安全性質：送進 resolver 的名字結尾必須是點。

        pod 的 ``/etc/resolv.conf`` 是 ``options ndots:5`` ＋ 4 個 search domain，
        而 ``paas-cs-x-y.woowtech.io`` 只有 2 個點 ⇒ 相對名會**先跑完 4 個 search
        domain 才問絕對名**。實測（prod paas-odoo pod）：miss 相對名約 100–216ms、
        FQDN 約 1–9ms；更要緊的是 resolver 不可用時，relative 最壞是
        5 次查詢 × (timeout 5s × 2 attempts) ＝ **50 秒**，FQDN 是 10 秒。而平台
        Odoo 是 ``workers = 0`` / ``max_cron_threads = 1``，cron 逐台序列跑。
        這條把那個性質釘住，否則日後會被無聲改掉。
        """
        with patch(_RESOLVE, return_value='ok') as m:
            self.svc._is_release_ready(_status())

        self.assertTrue(m.called, 'mock 沒被呼叫，這條測試無效')
        sent = m.call_args[0][0] if m.call_args[0] else m.call_args[1]['hostname']
        self.assertTrue(
            sent.endswith('.'),
            '送進 resolver 的必須是 FQDN（結尾帶點），實際送出 %r' % sent)
        self.assertTrue(sent.startswith(_HOSTNAME), sent)

    def test_unknown_resolution_passes_through_and_does_not_hold(self):
        """resolver 自己不可用（EAI_AGAIN／探測逾時）⇒ 放行，不擋。

        刻意**不** fail-closed：resolver 不可用是平台自身故障，讓它把每一台租戶
        服務都扣住，比原 bug 嚴重得多（探針壞掉不該比它要偵測的故障更痛）。
        """
        with patch(_RESOLVE, return_value='unknown') as m:
            ready, detail = self.svc._is_release_ready(_status())

        self.assertTrue(m.called)
        self.assertTrue(ready, 'resolver 不可用不該扣住服務（detail=%r）' % detail)

    def test_service_without_subdomain_skips_the_probe_entirely(self):
        """沒有對外網址的服務不可能有 DNS 問題 ⇒ 完全跳過探測。

        既有測試的 fixture 幾乎都沒設 ``subdomain``（``_public_base_url()`` 回空
        字串）；這裡對空值 fail-closed 的話，那批測試會整片紅。語意上也該放行，
        比照 ``_gateway_public_url()`` 的既有空值慣例。
        """
        svc = self._make_service(subdomain=False)

        with patch(_RESOLVE) as m:
            ready, _ = svc._is_release_ready(_status())

        self.assertFalse(
            m.called, '沒有 subdomain 還去探 DNS ⇒ 探的是空字串')
        self.assertTrue(ready)

    def test_gate_passes_outside_the_grace_window(self):
        """窗口外恆放行——這是「不能永遠卡」的那一半。"""
        svc = self._make_service(anchor_seconds_ago=10_000)

        with patch(_RESOLVE, return_value='missing') as m:
            ready, _ = svc._is_release_ready(_status())

        self.assertTrue(
            ready, '窗口已過還擋著 ⇒ DNS 永不落地的服務會永遠卡在 deploying')
        self.assertFalse(
            m.called,
            '窗口外就不該再探測（省掉 resolver 掛掉時的阻塞）；若實作選擇仍探測，'
            '改成斷言 ready 為 True 即可，但要在 PR 說明理由')

    def test_gate_passes_when_anchor_is_unknown(self):
        """兩個時間錨都空 ⇒ 無從判斷 age ⇒ 放行（與 age timeout 同方向的 fail-safe）。

        ``_transition_anchor()`` 對此回 ``None``，而 ``_check_transition_timeout``
        的處置是「沒有依據就不裁決」。閘若反過來擋，就會變成永遠卡。
        """
        svc = self._make_service(deployed_at=False, last_upgraded_at=False)

        with patch(_RESOLVE, return_value='missing'):
            ready, _ = svc._is_release_ready(_status())

        self.assertTrue(ready)

    def test_kill_switch_disables_the_gate_entirely(self):
        """``dns_gate_enabled=0`` ⇒ 完全跳過探測（prod kill switch）。"""
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.dns_gate_enabled', '0')

        with patch(_RESOLVE) as m:
            ready, _ = self.svc._is_release_ready(_status())

        self.assertFalse(m.called, 'kill switch 關了還在探測')
        self.assertTrue(ready)


# ====================================================================== #
#  兩層 poll（#423 W5a：只改一層會被另一層覆寫回去）                        #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestPublicUrlDnsGateBothPolls(_DnsGateCase):

    def test_model_poll_keeps_service_deploying(self):
        """🔴 model（cron）層：DNS 未落地不得翻 running。"""
        m = self._poll(resolve='missing')

        self.assertTrue(m.called)
        self.assertEqual(
            self.svc.state, 'deploying',
            'DNS 還沒落地卻翻 %r' % self.svc.state)

    def test_controller_poll_keeps_service_deploying(self):
        """🔴 controller（前台每 5 秒）層：同一個單一事實源要同時生效。

        只修 model 層不夠——租戶打開服務詳情頁，controller 那條 poll 會把 cron
        判好的狀態覆寫回 running（#423 M4 的教訓）。
        """
        m = self._poll_controller(resolve='missing')

        self.assertTrue(m.called)
        self.assertEqual(self.svc.state, 'deploying', self.svc.state)

    def test_model_poll_flips_running_when_dns_resolves(self):
        """characterization：DNS 落地 ⇒ 照常翻 running、不留旗標。"""
        self._poll(resolve='ok')

        self.assertEqual(self.svc.state, 'running')
        self.assertFalse(self.svc.public_url_dns_unverified)

    def test_controller_poll_flips_running_when_dns_resolves(self):
        self._poll_controller(resolve='ok')

        self.assertEqual(self.svc.state, 'running')
        self.assertFalse(self.svc.public_url_dns_unverified)


# ====================================================================== #
#  窗口過期後的放行 ＋ 對使用者可見的旗標                                   #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestPublicUrlDnsGateReleasesWithNotice(_DnsGateCase):

    def test_expired_window_flips_running_and_flags_unverified(self):
        """🔴 逾時的結果是「翻 running ＋ 留下提示」，**不是** error。

        工單要求：DNS 落地會自癒 ⇒ 必須有逾時、且不能永遠卡。
        """
        svc = self._make_service(anchor_seconds_ago=10_000)

        self._poll(svc, resolve='missing')

        self.assertEqual(svc.state, 'running', svc.state)
        self.assertTrue(
            svc.public_url_dns_unverified,
            '放行了卻沒留下任何使用者看得到的訊號')
        self.assertFalse(
            svc.error_message,
            '不得寫進 error_message——ServiceDetailPage.xml 的 '
            't-if="service.error_message" 不判 state，會直接冒紅色故障 banner')

    def test_expired_window_flags_at_controller_layer_too(self):
        svc = self._make_service(anchor_seconds_ago=10_000)

        self._poll_controller(svc, resolve='missing')

        self.assertEqual(svc.state, 'running', svc.state)
        self.assertTrue(svc.public_url_dns_unverified)

    def test_flag_is_cleared_once_dns_lands(self):
        """旗標必須清得掉，否則變成永久噪音（running 不在任何 poll 的 domain 裡）。"""
        svc = self._make_service(anchor_seconds_ago=10_000)
        self._poll(svc, resolve='missing')
        self.assertTrue(svc.public_url_dns_unverified)

        svc.write({'state': 'deploying'})
        self._poll(svc, resolve='ok')

        self.assertFalse(svc.public_url_dns_unverified)


# ====================================================================== #
#  🔴 H2 不變式：DNS 閘永遠不可能造成 age timeout                          #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestGateNeverCausesAgeTimeout(_DnsGateCase):

    def test_gate_can_never_cause_age_timeout(self):
        """🔴 本檔最重要的一條：慢部署 ＋ DNS 未落地，**不得**被寫成 error。

        情境：pods 直到 age 已經**超過** ``deploy_timeout`` 才 ready，此時 DNS 仍
        未落地。

        ⚠ 判別式的選法（第一版寫錯過，記在這裡免得被改回去）：一開始我把錨點設在
        ``deploy_timeout − 30 秒``，那個**分不出兩種實作**——自己另計時的錯誤版本
        在那一輪 age 仍小於門檻，要**再跑一輪**才會寫 error，一輪測不出來。改成
        錨點已經超過門檻之後，一輪就有鑑別力：

        - 窗口綁 anchor 的正確版：age 早已超出寬限期 ⇒ 閘不擋 ⇒ ``all_ready``
          為真 ⇒ ``stuck_detail`` 保持 ``None`` ⇒ ``_check_transition_timeout``
          **根本不會被呼叫** ⇒ ``running``。
        - 自己另計時的錯誤版：閘擋住 ⇒ ``stuck_detail`` 被設 ⇒ age 已過門檻 ⇒
          當場寫 ``state='error'``，而 ``error`` 不在
          ``_TIMEOUT_GOVERNED_STATES`` 裡、沒有任何 poll 分支救得回來。

        （本條在修法接線**之前**就是綠的——它不是 RED，是防止修法走偏的守線。
        兩種實作都會讓其他 RED 轉綠，只有這一條把它們分開。）
        """
        timeout_minutes = self.svc._transition_timeout_minutes()
        svc = self._make_service(
            anchor_seconds_ago=timeout_minutes * 60 + 30)

        self._poll(svc, resolve='missing')

        self.assertNotEqual(
            svc.state, 'error',
            '健康但 DNS 慢的服務被 age timeout 判成部署失敗——這比原 bug 更糟')
        self.assertEqual(svc.state, 'running', svc.state)

    def test_effective_grace_is_clamped_below_deploy_timeout(self):
        """設定值大於 deploy timeout 時，有效寬限期必須被夾住。

        沒有這道 clamp，上面那條不變式只在「有人沒亂設參數」時成立。
        """
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.dns_grace_seconds', '999999')

        effective = self.svc._dns_grace_seconds()
        timeout_seconds = self.svc._transition_timeout_minutes() * 60

        self.assertLess(
            effective, timeout_seconds,
            '有效寬限期沒被夾到 deploy timeout 以下 ⇒ 閘可能撐到 age timeout 開火')


# ====================================================================== #
#  卸載清理（repo-lint 的 config-parameter gate 對純 get_param 是盲區）      #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestNewConfigParametersAreRegisteredForUninstall(TransactionCase):
    """新的兩個 ``ir.config_parameter`` 必須進 ``hooks.py`` 的 ``_CONFIG_PARAMETERS``。

    Odoo 卸載 addon **不會**自動清 ``ir.config_parameter``。repo-lint 的
    ``check_config_parameter_uninstall`` 只掃 ``config_parameter=`` 這個字面 kwarg
    （``repo-lint.py`` 的 ``_declared_config_parameters`` docstring 明講），而本題
    兩個 key 走的是「模組常數 ＋ ``sudo().get_param()``」的寫法（同 #489 的
    ``deploy_timeout_minutes`` 先例）⇒ **忘了登記時 gate 不會紅**。這條測試就是補上
    那個盲區的機器守線。
    """

    def test_dns_params_are_in_config_parameters_list(self):
        from ..hooks import _CONFIG_PARAMETERS

        for key in ('woow_paas_platform.dns_grace_seconds',
                    'woow_paas_platform.dns_gate_enabled'):
            self.assertIn(
                key, _CONFIG_PARAMETERS,
                '%s 沒進 hooks.py 的 _CONFIG_PARAMETERS ⇒ 卸載後留孤兒紀錄，'
                '而 repo-lint 那條 gate 對這種寫法是盲區、不會替你抓' % key)


# ====================================================================== #
#  探針本身的行程安全（壞掉時與「DNS 一直正常」不可區分）                   #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestDnsProbeExecutorIsForkSafe(TransactionCase):
    """``dns_probe`` 的 executor 必須跟著行程走，不能是 import 時就定死的那一個。

    🔴 這條守的不是效能，是**靜默失效**。實測（Python 3.14 / darwin，機制與版本
    無關）：``ThreadPoolExecutor`` 在 fork 之後於子行程裡是廢的——``_threads``
    集合有 2 個物件但 ``is_alive()`` 全 False，而**繼承來的 ``_idle_semaphore``**
    讓 ``_adjust_thread_count()`` 的 ``acquire(timeout=0)`` 回 True 而提前 return
    ⇒ 一條 worker 都不會開 ⇒ 工作進 ``_work_queue`` 沒人消化 ⇒ future 永不完成。

    後果的形狀才是重點：future 逾時 ⇒ ``resolve_state`` 回 ``'unknown'`` ⇒ 呼叫端
    **放行** ⇒ DNS 閘永遠不擋。而「閘永遠不擋」與「DNS 一直都很正常」在所有可觀測
    訊號上完全一樣（沒有 error、測試全綠、服務照樣翻 running）——這個 feature 存在
    的唯一理由會靜靜地失效。

    今天 prod 是 ``workers = 0``（不 fork）所以碰不到，但那是**設定值不是不變式**，
    改設定的人不會知道要來看這裡。所以用測試釘住。
    """

    def test_executor_is_rebuilt_when_the_process_changes(self):
        from ..services import dns_probe

        dns_probe._reset_after_fork()
        first = dns_probe._executor()

        # 對照組先做：同一個行程內不得重建（否則下面那條「有重建」是恆真的，
        # 等於沒有鑑別力）。
        self.assertIs(
            first, dns_probe._executor(),
            '同一個行程內每次呼叫都重建 executor ⇒ 下面那條斷言變恆真')

        # 假裝我們現在在 fork 出來的子行程裡（pid 不同）。
        dns_probe._EXECUTOR_PID = -1
        self.assertIsNot(
            first, dns_probe._executor(),
            'pid 變了卻沿用 fork 前的 executor ⇒ future 永不完成 ⇒ 探測恆回 '
            "'unknown' ⇒ 閘永遠不擋，而且與「DNS 一直正常」不可區分")

    def test_reset_after_fork_drops_the_stale_executor(self):
        """``os.register_at_fork`` 掛的那個 hook 必須真的把舊的丟掉。"""
        from ..services import dns_probe

        dns_probe._executor()
        dns_probe._reset_after_fork()

        self.assertIsNone(dns_probe._EXECUTOR)
        self.assertIsNone(dns_probe._EXECUTOR_PID)

    def test_probe_never_raises(self):
        """探針不得炸掉呼叫端——就緒判定為了一個探針而爆掉是本末倒置。"""
        from ..services import dns_probe

        for bad in ('', None, 'not a hostname at all', '.' * 300):
            self.assertIn(
                dns_probe.resolve_state(bad, timeout=0.5),
                ('ok', 'missing', 'unknown'),
                '對 %r 回了預期外的值' % (bad,))


# ====================================================================== #
#  序列化：旗標必須到得了**列表頁**，不只詳情頁                             #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestFlagReachesTheListPayload(_DnsGateCase):
    """``_format_service`` 的 base dict 要帶這個旗標，不能只放 ``include_details``。

    列表頁走的是不帶 ``include_details`` 的那條路（``_list_services``），而
    ServiceCard 上的網址在 ``state === 'running'`` 時就是可點的 ⇒ 旗標若只到詳情
    頁，使用者最常出發的那個入口完全沒有預警，點下去一樣拿 NXDOMAIN。
    """

    def _format(self, include_details):
        from odoo.addons.woow_paas_platform.controllers.paas import PaasController
        fake_request = MagicMock()
        fake_request.env = self.env
        with patch('%s.request' % _CTRL_MODULE, new=fake_request):
            return PaasController()._format_service(
                self.svc, include_details=include_details)

    def test_flag_is_in_the_base_payload(self):
        self.svc.write({'state': 'running', 'public_url_dns_unverified': True})

        payload = self._format(include_details=False)

        self.assertIn(
            'public_url_dns_unverified', payload,
            '旗標沒進 base dict ⇒ 列表卡片永遠拿不到，UI 對這台服務完全沉默')
        self.assertTrue(payload['public_url_dns_unverified'])

    def test_flag_is_false_by_default(self):
        """對照組：沒問題的服務不得帶著旗標（否則每台都掛警告）。"""
        self.svc.write({'state': 'running'})

        self.assertFalse(self._format(include_details=False)
                         ['public_url_dns_unverified'])


@tagged('post_install', '-at_install')
class TestDnsProbeResolverBackoff(TransactionCase):
    """resolver 整個不可用時要退避，但**不得**被單一壞主機名癱瘓。

    ``'unknown'`` 的處置是「放行、連旗標都不動」⇒ resolver 掛掉時每次探測都是「付滿
    timeout、拿零資訊」。而平台 Odoo 是 ``workers = 0`` ＋ ``max_cron_threads = 1``、
    ``_cron_check_deploying_services`` 逐台序列跑、``limit_time_real = 120`` ⇒ 轉移中
    的服務一多，那條 cron 會被純粹的等待吃光。

    ⚠ 這道閂與 ``_is_public_url_ready`` 開頭那個「窗口外提前 return」**不是同一件事**
    ——後者在 production 熱路徑上省不到探測（兩份 poll 在更早就探完了）。熱路徑上有效
    的是這一條。
    """

    def setUp(self):
        super().setUp()
        from ..services import dns_probe
        self.dns_probe = dns_probe
        dns_probe._reset_after_fork()
        self.addCleanup(dns_probe._reset_after_fork)

    def test_resolver_timeout_latches_and_skips_further_probes(self):
        """🔴 resolver 級失效 ⇒ 上閂，窗口內不再送出查詢。

        ⚠ 這裡用**真的會阻塞**的 `_lookup` 去逼出 `future.result(timeout=...)` 的
        逾時，不是讓 `_lookup` 直接 raise `TimeoutError`。後者能通過只是因為
        Python 3.11 起 `concurrent.futures.TimeoutError` 是內建 `TimeoutError` 的
        別名——那是版本相依的巧合，換個版本就會掉進 `except Exception`（那條刻意
        **不**上閂），測試會綠得莫名其妙或紅得莫名其妙。
        """
        d = self.dns_probe
        calls = []

        def _blocking(hostname):
            calls.append(hostname)
            time.sleep(1.0)

        with patch.object(d, '_lookup', side_effect=_blocking):
            self.assertEqual(d.resolve_state('a.example.', timeout=0.05), 'unknown')
            calls_after_first = len(calls)

            self.assertEqual(d.resolve_state('b.example.'), 'unknown')

            self.assertEqual(
                len(calls), calls_after_first,
                '已經上閂還繼續送查詢 ⇒ resolver 掛掉時 cron 仍會被逐台的滿額 '
                'timeout 吃光，這道閂等於沒有')
        self.assertTrue(d._resolver_is_latched_down())

    def test_a_bad_hostname_must_not_latch(self):
        """🔴 負向對照：逐主機名的例外**不得**上閂。

        ``except Exception`` 那條也回 ``unknown``，但它可能是這個名字自己的問題
        （實測過：全是點的主機名讓 IDNA 編碼拋 ``UnicodeEncodeError``）。拿一個壞輸入
        斷定「resolver 掛了」，會讓其他健康的主機名跟著整段窗口不被探測——那是用一個
        壞輸入癱瘓整個守線。
        """
        d = self.dns_probe
        with patch.object(d, '_lookup', side_effect=UnicodeEncodeError(
                'idna', '', 0, 1, 'bad')):
            self.assertEqual(d.resolve_state('bad.example.'), 'unknown')

        self.assertFalse(
            d._resolver_is_latched_down(),
            '被單一壞主機名上了閂 ⇒ 其他健康主機名跟著停止探測')

    def test_latch_expires_and_probing_resumes(self):
        """閂是退避不是停用：窗口過了要恢復探測。"""
        d = self.dns_probe
        d._resolver_down_until = 0.0

        with patch.object(d, '_lookup', return_value=None) as lookup:
            self.assertEqual(d.resolve_state('ok.example.'), 'ok')

        self.assertTrue(lookup.called, '窗口外沒有恢復送查詢')

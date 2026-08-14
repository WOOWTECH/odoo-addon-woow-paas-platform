"""HA PaaS 開通 API（契約 v1.1 §7）＋ hidden template 呈現層過濾 helper（#336）。

- HiddenChainMixin：hidden template（如 HA PaaS）的 /woow 前台過濾 helper，混進
  paas/billing/dashboard controller。
- HAPaaSController：provision/status 端點（Bearer + ha:provision scope）。
"""
from __future__ import annotations

import json
import logging

from odoo.http import Controller, Response, request, route

from .oauth2 import verify_oauth_token
from ..models.cloud_service import _internal_provision

_logger = logging.getLogger(__name__)
_HA_PAAS_APP_TEMPLATE = 'woow_paas_platform.cloud_app_ha_paas'


def _json(data, status=200):
    return Response(json.dumps(data), status=status,
                    content_type='application/json')


def _auth(required_scopes):
    """auth='none' 端點的 Bearer 驗證（比照 ha_api._authenticate 慣例）：把
    werkzeug 例外轉成 JSON error Response，成功回 ``(user, token)``。"""
    from werkzeug.exceptions import Unauthorized, Forbidden
    try:
        return verify_oauth_token(request, required_scopes=required_scopes)
    except Unauthorized as exc:
        return _json({'error': 'Unauthorized', 'detail': str(exc.description)}, 401)
    except Forbidden as exc:
        return _json({'error': 'Forbidden', 'detail': str(exc.description)}, 403)


class HiddenChainMixin:
    """HA PaaS 這類 template 的**既有實例**呈現層過濾 helper。
    SSOT = ``template.conceal_instances``。

    ⚠️ #471：SSOT 從 ``is_hidden`` 換成 ``conceal_instances``。原本一個旗標同時承載
    「不上架」與「既有實例整條鏈消失」，於是任何「暫時下架」的一般商品都會把租戶手上
    正在跑的服務一起從前台抹掉（#464 實測 hermes-agent：詳情頁打不開、rollback／
    change-plan／MCP 設定全不可達）。本鏈處理的一律是**既有實例**（service／訂閱／
    發票／workspace），語意上就該由實例層旗標驅動；曝光層（marketplace 清單、template
    詳情／plans、generic create）仍讀 ``is_hidden``，那四處不經本 mixin。
    改判準前先讀 docs/decisions/patterns/hidden-template-presentation-chain.md。

    定性：呈現層過濾、**非 ACL**（租戶知 id 仍可打 JSON 端點；Odoo 原生 ``/my`` 與
    ``/report`` 繞過本層）。目的是讓平台自動開通的 0 元隱藏服務不污染前台清單。

    sudo justification（見 docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md）：
    下列 helper 的 ``.sudo()`` 讀的是「哪些 template/service/subscription/workspace
    屬於 hidden 鏈」這個**平台級全域事實**（server-authoritative），結果只用於
    ``('id','not in', hidden_ids)`` 式的**排除過濾**（subtractive），從不把跨租戶 row
    直接回給 caller（不是 anti-pattern 的 sudo-list-and-return）。因此對 caller 是純
    收斂可見範圍、無 over-access 風險。repo-lint baseline 已納入此 justification。

    效能：hidden 鏈規模極小（目前僅 HA PaaS 一個 template），每 request 呼叫端最多
    觸發一兩次 search，無需 memoize。

    ⚠️ helper 一律收呼叫端傳入的 ``env``（＝該 controller 的 ``request.env``），**不**在
    mixin 內直接引用 module-level ``request``。原因：mixin 定義在 ha_paas 模組，若引用
    ``request`` 會綁定成 ``ha_paas.request``；而既有單元測試（如 test_cloud_service_acl）
    patch 的是各 controller 自己模組的 ``request``（``paas.request`` 等），patch 不到
    ha_paas.request → 直呼端點時 unbound。收 env 讓依賴顯式、可單測、且沿用各測試既有的
    per-module request patch。
    """

    def _hidden_template_ids(self, env):
        # 下游三個 helper 全部由這一個 search 推導（DRY）——判準只有這一處，
        # 換語意就只改這行。#471：conceal_instances 而非 is_hidden。
        return env['woow_paas_platform.cloud_app_template'].sudo() \
            .search([('conceal_instances', '=', True)]).ids

    def _hidden_service_ids(self, env):
        CS = env['woow_paas_platform.cloud_service'].sudo()
        return CS.search([('template_id', 'in', self._hidden_template_ids(env))]).ids

    def _hidden_subscription_ids(self, env):
        # hidden service 的 subscription_id（mixin 提供的 M2O）攤平即完整 hidden
        # sub 集。state-agnostic（含 cancelled）——刪服務後的 0 元 cancelled sub 與
        # 歷史 0 元發票也不可漏進 billing 歷史。
        CS = env['woow_paas_platform.cloud_service'].sudo()
        svcs = CS.browse(self._hidden_service_ids(env))
        return [s.id for s in svcs.subscription_id if s]

    def _hidden_workspace_ids(self, env):
        # HA PaaS 服務住在 provision 當下自建的專屬 hidden workspace（D2 不變式：
        # 該 workspace 只裝這一個 hidden service），故 hidden service 的 workspace_id
        # 即專屬 hidden workspace 集。
        CS = env['woow_paas_platform.cloud_service'].sudo()
        return CS.browse(self._hidden_service_ids(env)).mapped('workspace_id').ids


class HAPaaSController(Controller):
    """HA PaaS 開通/狀態端點（契約 v1.1 §7）。Bearer + ha:provision scope。

    定性：這是**平台自動開通**的 server-authoritative 端點——App 拿 device-flow
    授權的 token 打 provision，平台代租戶建 workspace+service+0 元 sub。auth='none'
    下 ``request.env.user`` 是 public，故一律以 ``_auth`` 回傳的 token user 當 owner。

    sudo justification（見 docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md）：
    provision 建 workspace/service/subscription 屬平台級寫入（server-authoritative，
    非租戶自助 ORM 寫），bare token-user 無 ``base.group_user`` 無法過 workspace/
    workspace_access create ACL，故以 ``.sudo()`` 執行並顯式綁 ``owner_id=user.id``／
    ``provision_partner_id=partner.id`` 收斂歸屬；read 路徑（``_find_existing`` 等）以
    ``provision_partner_id`` 為鍵鎖定單一 partner，非 sudo-list-and-return。cross-user
    覆蓋見 TestHAPaaSProvisionEndpoint（401/403/冪等）。repo-lint baseline 已納入。
    """

    def _guard_partner(self, user):
        """回 partner 或 None。排除 public/system partner（多個 res.users 掛同一
        public partner 時，若不排除會讓它們共用同一把 advisory lock → 全站互擋）。"""
        partner = user.partner_id
        if not partner or not partner.active:
            return None
        blocked = set()
        for xmlid in ('base.public_partner', 'base.partner_root'):
            rec = request.env.ref(xmlid, raise_if_not_found=False)
            if rec:
                blocked.add(rec.id)
        if partner.id in blocked:
            return None
        return partner

    def _ha_paas_template(self):
        # auth='none' 下 request.env.user 是空 recordset，非 sudo 讀 template 欄位會
        # 觸發 ACL check 崩「Expected singleton: res.users()」，故回 sudo record。
        tpl = request.env.ref(_HA_PAAS_APP_TEMPLATE, raise_if_not_found=False)
        return tpl.sudo() if tpl else tpl

    def _template_deployable(self, tpl):
        # 工作項5 stopgap 落地後 seed 已 is_active=True ＋ pin 到 OCI chart，常態即
        # deployable。此 gate 仍保留為安全網：template 被停用（is_active=False）或
        # helm_chart_name 回退成佔位（TBD-*）時不得建 service，provision 回 503。
        return bool(tpl) and tpl.is_active \
            and not (tpl.helm_chart_name or '').startswith('TBD')

    def _find_existing(self, partner):
        return request.env['woow_paas_platform.cloud_service'].sudo().search(
            [('provision_partner_id', '=', partner.id)], limit=1)

    def _ha_url(self, svc):
        domain = request.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.paas_domain', 'woowtech.io')
        return 'https://%s.%s' % (svc.subdomain, domain)

    def _trigger_deploy_cron(self):
        cron = request.env.ref(
            'woow_paas_platform.ir_cron_deploy_cloud_services',
            raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()

    @route('/api/ha-paas/provision', type='http', auth='none',
           methods=['POST'], csrf=False, readonly=False)
    def provision(self, **kw):
        res = _auth(['ha:provision'])
        if isinstance(res, Response):
            return res
        user, _t = res
        partner = self._guard_partner(user)
        if not partner:
            return _json({'error': 'Forbidden', 'detail': 'No valid partner'}, 403)
        # auth='none' 下 env.user 是空 recordset；.sudo() 只翻 su 不改 uid，故建
        # subscription→invoice→mail 時 self.env.user._is_public() 會崩空 singleton。
        # 綁 token user 讓 env.user 成合法 record（發票/chatter 也正確歸屬）。
        request.update_env(user=user.id)
        tpl = self._ha_paas_template()
        if not self._template_deployable(tpl):
            # 未 deployable：回 503 且**不建任何 service**（契約 v1.1；工作項 5 blocked）
            return _json({'status': 'error',
                          'error': 'HA PaaS is not available yet.'}, 503)
        # 每帳號限一：advisory lock（partner 為鍵）序列化並發 provision，
        # 搭配 Task 5 的 partial unique index 雙層防護。
        from ..models.cloud_service import _HA_PROVISION_LOCK_NS
        request.env.cr.execute('SELECT pg_advisory_xact_lock(%s, %s)',
                               (_HA_PROVISION_LOCK_NS, int(partner.id)))
        existing = self._find_existing(partner)
        if existing:
            state = existing.state
            if state == 'running':
                return _json({'status': 'ready',
                              'ha_url': self._ha_url(existing)}, 200)
            if state == 'error':
                # 可重試：鎖內翻 error→pending + 觸發 cron（不 inline redeploy）
                existing.sudo().write({'state': 'pending', 'error_message': False})
                self._trigger_deploy_cron()
                return _json({'status': 'provisioning'}, 202)
            if state in ('pending', 'deploying', 'initializing', 'upgrading'):
                return _json({'status': 'provisioning'}, 202)
            if state == 'deleting':
                # 刪除進行中：不復活、不假裝 provisioning（deleting 由另一路徑控制、
                # cron 不撿它）。回明確 409、status=deleting（契約 v1.1 直映；Q4）。
                # App 待刪完後重試走「無既有」新建路徑。
                return _json({'status': 'deleting',
                              'error': 'A previous instance is being removed. '
                                       'Retry shortly.'}, 409)
            if state == 'suspended':
                # 蓄意停用：不靜默 resume。回明確 409、status=suspended
                # （契約 v1.1 直映；Q4）。App 指引：聯繫支援。
                return _json({'status': 'suspended',
                              'error': 'Service is suspended. Contact support.'}, 409)
            # 未知 state 的 fail-closed fallback（不洩漏 ha_url）
            return _json({'status': 'provisioning'}, 202)
        try:
            with request.env.cr.savepoint():
                self._create_ha_paas_service(user, partner, tpl)
        except Exception:  # partial unique index 撞 → 回既有（並發最後贏家）
            _logger.exception('HA PaaS provision create race')
            existing = self._find_existing(partner)
            if existing:
                return _json({'status': 'provisioning'}, 202)
            raise
        return _json({'status': 'provisioning'}, 202)

    @route('/api/ha-paas/status', type='http', auth='none',
           methods=['GET'], csrf=False)
    def status(self, **kw):
        res = _auth(['ha:provision'])
        if isinstance(res, Response):
            return res
        user, _t = res
        partner = self._guard_partner(user)
        if not partner:
            return _json({'error': 'Forbidden', 'detail': 'No valid partner'}, 403)
        request.update_env(user=user.id)  # 與 provision 一致（合法 env.user）
        svc = self._find_existing(partner)
        if not svc:
            return _json({'status': 'none'}, 200)
        mapping = {
            'pending': 'provisioning', 'deploying': 'provisioning',
            'initializing': 'provisioning', 'upgrading': 'provisioning',
            'running': 'ready', 'error': 'error',
            'suspended': 'suspended', 'deleting': 'deleting',  # 契約 v1.1 直映（Q4）
        }
        st = mapping.get(svc.state, 'provisioning')  # fail-closed default
        body = {'status': st}
        if st == 'ready':
            body['ha_url'] = self._ha_url(svc)
        elif st == 'error':
            body['error'] = svc.error_message or 'Provisioning failed'
        # suspended/deleting 只回 status 值（契約 v1.1 §7.3）；App 端據此顯示指引
        # （suspended＝聯繫支援、deleting＝稍後重試）。ha_url 僅 ready、error 僅 error。
        return _json(body, 200)

    def _create_ha_paas_service(self, user, partner, tpl):
        """建 hidden workspace + cloud_service（pending）+ 0 元 subscription，觸發
        deploy cron。比照 paas.py:_create_service（含 ingress expose_config，否則
        ha_url 死鏈），精簡為 HA PaaS 專用——無 **user** helm values，但 template 的
        helm_default_values 必須寫入（見下）。

        ⚠️ 對 plan Task 6 Step 4 的落地偏離：plan 寫 ``.sudo().with_user(user)``，
        但 with_user 會清 su → 實際以 bare token-user 執行，該 user 無 base.group_user
        無法過 workspace/workspace_access create ACL（見 ir.model.access.csv）。改以
        ``.sudo()`` 執行、顯式 ``owner_id=user.id`` 綁歸屬（server-authoritative）。
        """
        import uuid
        from ..services.naming import make_namespace, make_service_subdomain
        env = request.env
        # auth='none' 下 env.user 空 → env.company 也空，subscription mixin 讀
        # self.env.company.sh_journal_id 會誤判「journal 未設」。顯式綁 token user
        # 的 company（fresh user 預設為 main company，post_init 已設 sh_journal_id）。
        company = user.company_id or env.company
        ws = env['woow_paas_platform.workspace'].sudo().with_company(company).create({
            'name': 'HA PaaS', 'owner_id': user.id})
        reference_id = str(uuid.uuid4())
        ns_prefix = env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.namespace_prefix', 'paas-ws-')
        # #677：吃 stored `ws_hash`（伺服器產生、DB UNIQUE），不是租戶決定的 slug。
        subdomain = make_service_subdomain(ws.ws_hash, reference_id, 'ha')
        # ingress expose_config（比照 _create_service paas.py:2218-2233），否則
        # 部署後無 ingress、ha_url 指向不存在的網域。
        deploy_params = {}
        if tpl.ingress_enabled:
            deploy_params['expose_config'] = {'enabled': True, 'subdomain': subdomain}
        svc = _internal_provision(
            env['woow_paas_platform.cloud_service'].sudo().with_company(company)
        ).create({
            'workspace_id': ws.id, 'template_id': tpl.id, 'name': 'HA PaaS',
            'reference_id': reference_id, 'state': 'pending', 'subdomain': subdomain,
            'internal_port': tpl.default_port,
            'helm_release_name': 'svc-%s' % reference_id[:8],
            'helm_namespace': make_namespace(ws.ws_hash, prefix=ns_prefix),
            'helm_chart_version': tpl.helm_chart_version,
            # ⚠ helm_values 必須寫入 template 的 helm_default_values（比照 marketplace
            # _create_service 的 merged_values；HA 無 user values → template 值即全部）。
            # 漏寫的話 _execute_deploy 從 {} 起手、只剩平台注入值（imagePullSecrets／
            # storageClass）→ seed 釘的 image tag@digest／haProxy／resources 全程不進
            # helm → chart 0.2.x 的 fail-loud 佔位 tag（00000000.0000）被拉去部署，
            # 全新開通卡 Init:ErrImagePull（2026-07-23 stg 平台全路徑 e2e 實證）。
            # 0.1.x 年代沒炸只是 chart 預設恰好自足可部署的僥倖。loads→dumps 兼作
            # JSON 正規化驗證（seed 壞 JSON 在 create 當下 fail-loud，不留到部署期）。
            'helm_values': json.dumps(json.loads(tpl.helm_default_values or '{}')),
            'deploy_params': json.dumps(deploy_params) if deploy_params else False,
            'provision_partner_id': partner.id})
        svc._create_and_confirm_subscription(
            partner, env.ref('woow_paas_platform.plan_ha_paas_monthly'))
        self._trigger_deploy_cron()
        return svc

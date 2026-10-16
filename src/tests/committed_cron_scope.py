"""會 commit 的 cron tests 的 own-record ledger；只供 tests，不新增 production context 閘門。"""
from collections import defaultdict
from contextlib import ExitStack, contextmanager
from datetime import timedelta
from unittest.mock import patch

from odoo import SUPERUSER_ID, api, fields

SERVICE = 'woow_paas_platform.cloud_service'
ATTEMPT = 'woow_paas_platform.cloud_service_init_attempt'
MONITOR = 'woow_paas_platform.platform_monitor'
INIT_CRON = 'woow_paas_platform.ir_cron_run_post_deploy_inits'
POLL_CRON = 'woow_paas_platform.ir_cron_check_deploying_services'

# 先清 FK children，再清本組 parents。每一步只用記錄到的精確 IDs，不依 model／cron 全刪。
_CLEANUP_MODELS = (
    'mail.notification', 'mail.mail', 'mail.activity', 'mail.tracking.value',
    'ir.attachment', 'mail.followers', 'mail.message', 'ir.cron.trigger', 'ir.cron.progress',
    ATTEMPT, 'woow_paas_platform.cloud_service_secret', SERVICE,
    'woow_paas_platform.cloud_app_template', 'woow_paas_platform.workspace_access',
    'sh.subscription.subscription', 'product.product', 'product.template', 'sh.subscription.plan',
    MONITOR, 'woow_paas_platform.workspace_quota_audit', 'woow_paas_platform.workspace',
    'res.users', 'res.partner',
)


class CommittedCronScope:
    """資源建立前登記 cleanup 的 collector；metadata／ID 不帶 recordset 出 closed cursor。"""

    def __init__(self, case):
        self.case = case
        self.registry = case.registry
        self.ids = defaultdict(set)
        self.tables = {name: case.env[name]._table for name in _CLEANUP_MODELS}
        self.classes = {name: type(case.env[name]) for name in _CLEANUP_MODELS}
        self.foreign_calls = []
        self.monitor_id = None
        self.unowned_monitor_id = None
        self.releases = set()
        self._active = True
        self._real_monitor = False
        with self.registry.cursor() as cr:
            self.cursor_classes = {type(case.env.cr), type(cr)}

    def env(self, cr):
        if cr.transaction is None:
            cr.transaction = api.Transaction(self.registry)
        return api.Environment(cr, SUPERUSER_ID, {})

    @contextmanager
    def track(self):
        """collector 在 create 前掛好；交易失敗時捕捉到的 IDs 不存在也可冪等 purge。"""
        def tracker(original, name):
            @api.model_create_multi
            def create(records, vals_list):
                created = original(records, vals_list)
                if self._active:
                    self.ids[name].update(created.ids)
                    if name == SERVICE:
                        self.releases.update((record.helm_namespace, record.helm_release_name)
                                             for record in created)
                return created
            return create

        with ExitStack() as stack:
            for name, model in self.classes.items():
                stack.enter_context(patch.object(model, 'create', tracker(model.create, name)))
            yield self

    @contextmanager
    def paused(self):
        """只在建立另一組 sentinel 時暫停本組 adapter；不是 production/RPC context。"""
        active = self._active
        self._active = False
        try:
            yield
        finally:
            self._active = active

    @contextmanager
    def monitoring(self):
        """容量／monitor-focused cases 跑真正 checks/savepoints，不拿 no-op 當驗收。"""
        previous = self._real_monitor
        self._real_monitor = True
        try:
            yield
        finally:
            self._real_monitor = previous

    def operator(self, client):
        """外部 client 只識別 own namespace/release；caller catch-all 後仍留下可斷言紀錄。"""
        if client is None:
            return None
        scope = self

        class OwnedOperator:
            def __getattr__(self, name):
                attribute = getattr(client, name)
                if not callable(attribute):
                    return attribute

                def call(*args, **kwargs):
                    namespace = kwargs.get('namespace', args[0] if args else None)
                    release = kwargs.get('release_name', kwargs.get('name', args[1] if len(args) > 1 else None))
                    # quota API 沒有 release 引數，第二個 positional value 是額度，不能誤認為 name。
                    owned = ((namespace, release) in scope.releases
                             if name not in ('update_namespace_quota', 'get_namespace_quota')
                             else namespace in {pair[0] for pair in scope.releases})
                    if not owned:
                        scope.foreign_calls.append((name, namespace, release))
                        raise AssertionError('test operator received a foreign namespace/release')
                    return attribute(*args, **kwargs)
                return call
        return OwnedOperator()

    @contextmanager
    def isolated(self, real_monitor=False):
        """保留真 selection/order/loop，只將候選與通知目標限制在本組 IDs。

        非 monitor-focused cases 的兩個 health hooks 明確隔離；monitor-focused cases
        仍跑原函式。oldest-observation 使用 raw SQL，僅在 test adapter 加 own-ID WHERE，
        不 fake age/eligibility/state/outcome。所有 adapter 隨 test 退場恢復。
        """
        self._real_monitor = real_monitor
        service_model = self.classes[SERVICE]
        attempt_model = self.classes[ATTEMPT]
        monitor_model = self.classes[MONITOR]

        def scoped_search(original, attempt=False):
            def search(records, *args, **kwargs):
                selected = original(records, *args, **kwargs)
                if not self._active:
                    return selected
                return selected.filtered(lambda record: (
                    record.service_id.id if attempt else record.id) in self.ids[SERVICE])
            return search

        original_queue = service_model._init_cron_queue
        def queue(records):
            selected = original_queue(records)
            return selected.filtered(lambda record: record.id in self.ids[SERVICE]) if self._active else selected

        original_singleton = monitor_model._singleton
        def singleton(records):
            if not self._active:
                return original_singleton(records)
            return records.sudo().browse(self.monitor_id)

        def monitor_hook(original):
            def check(records):
                if self._active and not self._real_monitor:
                    return None  # 本 case 不驗 health hooks；不是監控行為的 GREEN。
                return original(records)
            return check

        def scoped_sql(original):
            def execute(cr, query, params=None, *args, **kwargs):
                if (self._active and isinstance(query, str)
                        and self.tables[SERVICE] in query
                        and ('END AS seen' in query or 'SET observation_episode_started_at' in query)
                        and 'WHERE state IN %s' in query):
                    query = query.replace('WHERE state IN %s', 'WHERE id = ANY(%s) AND state IN %s')
                    params = (sorted(self.ids[SERVICE]),) + tuple(params or ())
                return original(cr, query, params, *args, **kwargs)
            return execute

        with ExitStack() as stack:
            stack.enter_context(self.track())
            stack.enter_context(patch.object(service_model, 'search', scoped_search(service_model.search)))
            stack.enter_context(patch.object(attempt_model, 'search', scoped_search(attempt_model.search, attempt=True)))
            stack.enter_context(patch.object(service_model, '_init_cron_queue', queue))
            stack.enter_context(patch.object(monitor_model, '_singleton', singleton))
            for method in ('_run_poll_cron_checks', '_run_init_cron_checks'):
                stack.enter_context(patch.object(monitor_model, method, monitor_hook(getattr(monitor_model, method))))
            for cursor_class in self.cursor_classes:
                stack.enter_context(patch.object(cursor_class, 'execute', scoped_sql(cursor_class.execute)))
            yield self

    def purge(self):
        with self.registry.cursor() as cr:
            for name in _CLEANUP_MODELS:
                if self.ids[name]:
                    cr.execute(f'DELETE FROM {self.tables[name]} WHERE id = ANY(%s)',
                               (sorted(self.ids[name]),))
            cr.commit()

    def snapshot(self):
        """獨立 cursor 的身份＋內容快照；service 只取非敏感狀態／時計欄位。"""
        result = {}
        with self.registry.cursor() as cr:
            for name in _CLEANUP_MODELS:
                if not self.ids[name]:
                    continue
                table = self.tables[name]
                if name == SERVICE:
                    columns = ('id, name, state, observed_at, observation_episode_started_at, '
                               'init_pending, init_generation, init_ready_observed, init_ready_since, '
                               'init_lease_until, init_attempt_id, init_attempt_seq, init_retries, init_unknown_count, '
                               'init_wait_spent_seconds, init_wait_started_at, init_wait_since, init_retry_eligible_at, '
                               'init_queue_turn, init_capacity_wait_since, init_exit, error_message, '
                               'init_redeploy_requested, public_url_dns_unverified')
                    query = f'SELECT to_jsonb(t) FROM (SELECT {columns} FROM {table} WHERE id = ANY(%s)) t ORDER BY t.id'
                elif name == 'res.users':
                    query = (f'SELECT to_jsonb(t) FROM (SELECT id, login, active, partner_id FROM {table} '
                             'WHERE id = ANY(%s)) t ORDER BY t.id')
                else:
                    query = f'SELECT to_jsonb(t) FROM {table} t WHERE id = ANY(%s) ORDER BY id'
                cr.execute(query, (sorted(self.ids[name]),))
                result[name] = [row[0] for row in cr.fetchall()]
            cr.execute('SELECT monitor_id, attempt_id FROM woow_paas_platform_monitor_unknown_attempt_rel '
                       'WHERE monitor_id = ANY(%s) ORDER BY monitor_id, attempt_id',
                       (sorted(self.ids[MONITOR]),))
            result['unknown_ledger'] = cr.fetchall()
            if self.unowned_monitor_id:
                # 原 data singleton 不是 cleanup-owned parent；只讀身份/內容及 ledger 作對照。
                cr.execute(f'SELECT to_jsonb(t) FROM {self.tables[MONITOR]} t WHERE id = %s',
                           (self.unowned_monitor_id,))
                result['unowned_singleton'] = cr.fetchone()[0]
                cr.execute('SELECT monitor_id, attempt_id FROM woow_paas_platform_monitor_unknown_attempt_rel '
                           'WHERE monitor_id = %s ORDER BY attempt_id', (self.unowned_monitor_id,))
                result['unowned_singleton_ledger'] = cr.fetchall()
        return result

    def make_sentinels(self, tag):
        """另一組本 test 自有資料；不在主要 operation／cleanup scope，最後才精確清掉。"""
        from ..models.cloud_service import _internal_provision
        with self.track(), self.registry.cursor() as cr:
            env = self.env(cr)
            workspace = env['woow_paas_platform.workspace'].create({'name': f'Sentinel WS {tag}'})
            template = env['woow_paas_platform.cloud_app_template'].create({
                'name': f'Sentinel app {tag}', 'slug': f'sentinel-{tag}',
                'helm_repo_url': 'https://example.invalid', 'helm_chart_name': 'chart',
                'helm_chart_version': '1.0.0', 'post_deploy_init_type': 'odoo', 'mcp_enabled': False})
            for kind, state in (('initializing', 'initializing'), ('upgrading', 'upgrading'),
                                ('spent', 'initializing'), ('redeploy', 'running'), ('dns', 'running')):
                service = _internal_provision(env[SERVICE]).create({
                    'name': f'Sentinel {kind} {tag}', 'workspace_id': workspace.id,
                    'template_id': template.id, 'state': state,
                    'helm_namespace': f'dev-sandbox-1159-sentinel-{tag}',
                    'helm_release_name': f'sentinel-{kind}-{tag}'})
                service.write({'init_ready_observed': True, 'init_pending': True,
                    'observed_at': fields.Datetime.now() - timedelta(days=3),
                    'observation_episode_started_at': False})
                if kind == 'initializing':
                    eligible = service
                elif kind == 'spent':
                    service.write({'init_wait_spent_seconds': 1800, 'init_ready_observed': False})
                elif kind == 'redeploy':
                    service.write({'init_redeploy_requested': True})
                elif kind == 'dns':
                    service.write({'subdomain': f'sentinel-dns-{tag}', 'public_url_dns_unverified': True})
            attempt = env[ATTEMPT].create({
                'service_id': eligible.id, 'generation': eligible.init_generation, 'sequence': 91,
                'init_type': 'odoo', 'claimed_at': fields.Datetime.now() - timedelta(hours=1),
                'lease_until': fields.Datetime.now(), 'finished_at': fields.Datetime.now(),
                'outcome': 'unknown'})
            monitor = env[MONITOR].create({'name': f'Sentinel monitor {tag}'})
            monitor.write({'notified_unknown_attempt_ids': [(4, attempt.id)]})
            original = env.ref('woow_paas_platform.platform_monitor_singleton')
            self.unowned_monitor_id = original.id  # 不登進 cleanup IDs，不能刪 data singleton。
            partner = env['res.partner'].create({'name': f'Sentinel follower {tag}'})
            for target in (monitor, original):
                target.with_context(mail_notify_force_send=False).activity_schedule(
                    activity_type_id=env.ref('mail.mail_activity_data_todo').id,
                    summary=f'Sentinel untouched {tag}', note=f'<p>Sentinel note {tag}</p>',
                    user_id=env.ref('base.user_admin').id)
                env['mail.message'].create({'model': MONITOR, 'res_id': target.id,
                    'body': f'<p>Sentinel message {tag}</p>', 'message_type': 'comment'})
                target.message_subscribe(partner_ids=partner.ids)
            for xmlid in (INIT_CRON, POLL_CRON):
                env.ref(xmlid)._trigger(at=fields.Datetime.now() + timedelta(hours=4))
            cr.commit()
        return self.snapshot()

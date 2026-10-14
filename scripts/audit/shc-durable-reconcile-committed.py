# Isolated QA only: no production clone, keys, namespaces, or provider endpoints.
# Caller must run this inside its own deny-all dev-sandbox Pod; no SA token.
"""Committed-cursor/process proof for the authoritative C1 reconcile pass.

Own empty QA DB only: real ORM, independent worker/reconciler processes, real
commits, SIGKILL, and a localhost operator-shaped fake provider that also serves
``/api/reconcile/inventory``. It is not Cloudflare and proves nothing about it.
"""
from datetime import timedelta
from pathlib import Path
import json, os, subprocess, sys, threading, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB = os.environ.get('SHC_RECONCILE_QA_DB', '')
assert DB.startswith('woow_shc_c1rec_') and DB.replace('_', '').isalnum(), 'explicit own empty QA DB required'
assert os.environ.get('SHC_RECONCILE_QA_NAMESPACE', '').startswith('dev-sandbox-shc-c1rec-'), 'isolated QA namespace marker required'
CONF = os.environ.get('SHC_RECONCILE_QA_ODOO_CONF', '/tmp/odoo.conf')
ADDONS = os.environ.get('SHC_RECONCILE_QA_ADDONS', '/tmp/shc-c1-addons,/usr/lib/python3/dist-packages/odoo/addons,/opt/woow/addons')
ROOT = Path(os.environ.get('SHC_RECONCILE_QA_EVIDENCE', '/tmp/shc-c1-reconcile-evidence'))
ROOT.mkdir(parents=True, exist_ok=True)
MODEL = 'woow_paas_platform.smart_home_provision_operation'
HOME = 'woow_paas_platform.smart_home'
FLAG = 'woow_paas_platform.shc_durable_provision_prototype_enabled'
RECONCILE_FLAG = 'woow_paas_platform.shc_durable_provision_reconcile_enabled'
DOMAIN = 'shc-prototype.invalid'

import odoo
from odoo import api, SUPERUSER_ID, fields
odoo.tools.config.parse_config(['-c', CONF, '-d', DB, '--addons-path=' + ADDONS, '--max-cron-threads=0', '--workers=0', '--no-http', '--smtp=127.0.0.1', '--smtp-port=9'])
assert odoo.tools.config['db_host'] == '127.0.0.1', 'own loopback synthetic DB required'
registry = odoo.registry(DB)

if len(sys.argv) > 1 and sys.argv[1] in ('worker', 'reconcile', 'cron'):
    with registry.cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {})
        Operation = env[MODEL]
        if sys.argv[1] == 'worker':
            out = {'worker': [Operation._process_one(int(i)) for i in sys.argv[2:]]}
        elif sys.argv[1] == 'reconcile':
            out = {'outcomes': {str(k): v for k, v in Operation._reconcile_batch(limit=int(sys.argv[2])).items()}}
        else:
            Operation._cron_process(limit=int(sys.argv[2]))
            out = {'cron': 'done'}
    print('CHILD_RESULT=' + json.dumps(out), flush=True)
    sys.exit(0)

from odoo.addons.woow_paas_platform.tests.smart_home_fixture import sh_model
from odoo.addons.woow_paas_platform.models.smart_home_provision_operation import _internal_operation


class FakeOperator:
    """Operator-shaped loopback provider with an authoritative inventory."""

    def __init__(self):
        self.calls = []
        self.live = {}            # tunnel_id -> name (non-deleted)
        self.account = 'synthetic-account'
        self.paas_env = 'prod'
        self.create_mode = 'normal'
        self.inventory_script = []
        self.binding_script = []
        self.poisoned = set()      # tunnel names whose token read fails
        self.lock = threading.Lock()
        self.entered = threading.Event()
        self.release = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, data, code=200):
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def record(self, method, path, **values):
                with owner.lock:
                    event = {'method': method, 'path': path, **values}
                    owner.calls.append(event)
                    with (ROOT / 'fake-provider-ledger.jsonl').open('a') as f:
                        f.write(json.dumps(event) + '\n')

            def do_GET(self):
                if self.path == '/api/tunnels/provider-binding':
                    with owner.lock:
                        account = owner.binding_script.pop(0) if owner.binding_script else owner.account
                    self.record('BINDING', self.path)
                    return self.send({'provider_account_id': account})
                if self.path == '/api/reconcile/inventory':
                    with owner.lock:
                        behaviour = owner.inventory_script.pop(0) if owner.inventory_script else 'normal'
                    self.record('INVENTORY', self.path, behaviour=behaviour)
                    if behaviour == 'pause':
                        owner.entered.set()
                        owner.release.wait(60)
                    if behaviour == '503':
                        return self.send({'detail': 'synthetic inventory unavailable'}, 503)
                    with owner.lock:
                        resources = [{'id': tid, 'name': name, 'kind': 'tunnel'} for tid, name in owner.live.items()]
                    resources.append({'id': 'synthetic-app', 'name': 'synthetic', 'kind': 'access_app'})
                    partial = behaviour == 'partial'
                    return self.send({
                        'paas_env': owner.paas_env, 'shared_tunnel_id': 'synthetic-shared',
                        'partial': partial, 'failed_kinds': ['access_app'] if partial else [],
                        'resources': resources,
                    })
                self.record('GET', self.path)
                if self.path.endswith('/token'):
                    tunnel_id = self.path.split('/')[-2]
                    if tunnel_id not in owner.live:
                        return self.send({'detail': 'unknown synthetic tunnel'}, 404)
                    if owner.live[tunnel_id] in owner.poisoned:
                        return self.send({'detail': 'synthetic token unavailable'}, 503)
                    return self.send({'token': 'synthetic-only-token'})
                return self.send({'detail': 'synthetic unavailable'}, 404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path != '/api/tunnels/bare':
                    self.record('POST', self.path)
                    return self.send({'detail': 'legacy mutations forbidden'}, 503)
                if body['expected_account_id'] != owner.account:
                    self.record('POST_PREFLIGHT_REFUSED', self.path, name=body['name'])
                    return self.send({'detail': {'error': 'provider_binding_changed', 'outcome': 'not_created'}}, 409)
                if owner.create_mode == 'refuse_unknown':
                    # Error that does NOT create: the client cannot tell.
                    self.record('CREATE_ATTEMPT_NOT_CREATED', self.path, name=body['name'])
                    return self.send({'detail': {'error': 'bare_create_outcome_unknown', 'outcome': 'unknown'}}, 502)
                tunnel_id = str(uuid.uuid4())
                with owner.lock:
                    owner.live[tunnel_id] = body['name']
                self.record('CREATE', self.path, tunnel_id=tunnel_id, name=body['name'])
                if owner.create_mode == 'redirect307':
                    self.send_response(307)
                    self.send_header('Location', '/api/tunnels/redirect-target')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                if owner.create_mode == 'malformed201':
                    return self.send({'unexpected': 'synthetic-created-but-unusable-response'}, 201)
                if owner.create_mode == 'pause_create':
                    owner.entered.set()
                    owner.release.wait(60)
                return self.send({'tunnel_id': tunnel_id, 'tunnel_name': body['name'], 'provider_account_id': owner.account}, 201)

            def do_PUT(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                self.record('PUT', self.path)
                if self.path.endswith('/routes'):
                    tunnel_id = self.path.split('/')[-2]
                    if tunnel_id not in owner.live:
                        return self.send({'detail': 'unknown synthetic tunnel'}, 404)
                    return self.send({'tunnel_id': tunnel_id, 'routes': [{'subdomain': r['subdomain'], 'hostname': r['subdomain'] + '.' + DOMAIN, 'dns_record_id': 'synthetic-dns-id'} for r in body['routes']], 'dns_failed': False})
                if self.path.endswith('/config'):
                    tunnel_id = self.path.split('/')[-2]
                    if tunnel_id not in owner.live or body.get('tunnel_name') != owner.live[tunnel_id]:
                        return self.send({'detail': 'synthetic ownership mismatch'}, 409)
                    return self.send({'message': 'Synthetic configuration accepted', 'hostname': body['hostname'], 'service_url': body['service_url']})
                if self.path == '/api/access/sync':
                    return self.send({'app_id': str(uuid.uuid4()), 'policy_ids': [], 'orphaned_policy_ids': []})
                return self.send({'detail': 'unexpected mutation'}, 503)

            def do_DELETE(self):
                self.record('DELETE', self.path)
                return self.send({'detail': 'automatic compensation forbidden'}, 503)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def barrier(self):
        self.entered.clear()
        self.release.clear()

    def count(self, method, name=None):
        return sum(c['method'] == method and (name is None or c.get('name') == name) for c in self.calls)

    def ids_for(self, name):
        return [tid for tid, n in self.live.items() if n == name]

    def token_reads(self, tunnel_id):
        return sum(c['method'] == 'GET' and c['path'] == '/api/tunnels/%s/token' % tunnel_id for c in self.calls)


fake = FakeOperator()
results = []


def set_param(key, value):
    with registry.cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {})
        env['ir.config_parameter'].sudo().set_param(key, value)


def fixture(label):
    with registry.cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {})
        params = env['ir.config_parameter'].sudo()
        params.set_param(FLAG, 'True')
        params.set_param('woow_paas_platform.operator_url', fake.url)
        params.set_param('woow_paas_platform.operator_api_key', 'synthetic-only-key')
        params.set_param('woow_paas_platform.paas_domain', DOMAIN)
        portal = env['res.users'].with_context(no_reset_password=True).create({'name': 'C1R ' + label, 'login': 'c1r-' + str(uuid.uuid4()) + '@example.invalid', 'groups_id': [(6, 0, [env.ref('base.group_portal').id])]})
        plan = env['sh.subscription.plan'].create({'name': 'C1R ' + label, 'sh_plan_price': 0, 'sh_duration': 1, 'sh_unit': 'month', 'sh_no_of_billing_cycle': -1})
        product = env['product.template'].create({'name': 'C1R ' + label, 'type': 'service', 'sh_product_subscribe': True, 'sh_subscription_plan_id': plan.id})
        ws = env['woow_paas_platform.workspace'].create({'name': 'C1R ' + label, 'owner_id': portal.id})
        sub = env['sh.subscription.subscription'].create({'sh_partner_id': portal.partner_id.id, 'product_id': product.product_variant_id.id, 'sh_subscription_plan_id': plan.id, 'sh_source': 'manual', 'state': 'in_progress'})
        home = sh_model(env).create({'name': 'C1R ' + label, 'workspace_id': ws.id, 'subscription_id': sub.id})
        return home.id, portal.id


def enqueue(home_id, actor_id):
    with registry.cursor() as cr:
        env = api.Environment(cr, actor_id, {})
        home = env[HOME].sudo().browse(home_id)
        outcome = home.action_provision()
        return env[MODEL]._for_home(home).id, outcome


def snapshot(op_id):
    with registry.cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {})
        op = env[MODEL].browse(op_id)
        if not op.exists():
            return {'exists': False}
        h = op.home_id
        return {
            'exists': True, 'phase': op.phase, 'error_code': op.error_code or None,
            'home_state': h.state, 'remote_tunnel_id': op.remote_tunnel_id or None,
            'home_tunnel_id': h.tunnel_id or None, 'name': op.tunnel_name,
            'retry_count': op.retry_count, 'create_attempts': op.create_attempts,
            'next_attempt_at': op.next_attempt_at, 'absent_since': op.absent_since,
            'claim': op.reconcile_generation or None,
        }


def write_op(op_id, **values):
    with registry.cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {})
        _internal_operation(env[MODEL].browse(op_id)).write(values)


def now():
    return fields.Datetime.now()


def due(op_id):
    write_op(op_id, next_attempt_at=now() - timedelta(seconds=1))


def settle_absence(op_id):
    write_op(op_id, absent_since=now() - timedelta(minutes=16), next_attempt_at=now() - timedelta(seconds=1))


def spawn(args, label):
    log = (ROOT / (label + '.log')).open('w')
    return subprocess.Popen([sys.executable, __file__] + [str(a) for a in args], stdout=log, stderr=subprocess.STDOUT)


def finish(p, label):
    p.wait(timeout=90)
    out = (ROOT / (label + '.log')).read_text()
    assert p.returncode == 0, ('child exit', label, p.returncode, out[-3000:])
    lines = [x for x in out.splitlines() if x.startswith('CHILD_RESULT=')]
    return json.loads(lines[-1].split('=', 1)[1]) if lines else {}


def run(args, label):
    return finish(spawn(args, label), label)


def inprocess(method, *args):
    """Same committed registry cursors as a child, without process start-up.

    Kill/concurrency cases use real child processes (``spawn``) instead."""
    with registry.cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {})
        return getattr(env[MODEL], method)(*args)


def reconcile(label):
    return dict(inprocess('_reconcile_batch', 50))


def worker(op_ids, label):
    return [inprocess('_process_one', int(op)) for op in op_ids]


def cron(limit, label):
    inprocess('_cron_process', limit)


def assert_not_due(op_id):
    s = snapshot(op_id)
    terminal = s['phase'] == 'completed' or (s['phase'] == 'recovery_required' and s['error_code'] and s['error_code'].startswith('reconcile_'))
    assert terminal or (s['next_attempt_at'] and s['next_attempt_at'] > now()), ('case left a due row', s)


def make_unknown(label, mode):
    home, actor = fixture(label)
    op, _ = enqueue(home, actor)
    fake.create_mode = mode
    worker([op], label + '-create')
    fake.create_mode = 'normal'
    s = snapshot(op)
    assert s['phase'] == 'recovery_required' and s['error_code'] == 'create_outcome_unknown', s
    assert s['remote_tunnel_id'] is None and s['home_tunnel_id'] is None, s
    return home, actor, op, s['name']


def make_late_response(label):
    home, actor = fixture(label)
    op, _ = enqueue(home, actor)
    fake.barrier()
    fake.create_mode = 'pause_create'
    original = spawn(['worker', op], label + '-original')
    assert fake.entered.wait(30)
    write_op(op, lease_until=now() - timedelta(seconds=1))
    worker([op], label + '-expiry')
    assert snapshot(op)['phase'] == 'recovery_required'
    fake.release.set()
    finish(original, label + '-original')
    fake.create_mode = 'normal'
    s = snapshot(op)
    assert s['error_code'] == 'create_worker_interrupted' and s['remote_tunnel_id'] and s['home_tunnel_id'] is None, s
    return home, actor, op, s


def save(label, **evidence):
    results.append({'case': label, 'passed': True, **evidence})
    (ROOT / 'result.json').write_text(json.dumps({'prototype': True, 'commercial_c1_fixed': False, 'fake_boundary': 'operator-shaped localhost HTTP incl. /api/reconcile/inventory; not Cloudflare', 'db': DB, 'cases': results}, indent=2, default=str) + '\n')


def converge(op, label):
    result = worker([op], label + '-converge')
    s = snapshot(op)
    assert s['phase'] == 'completed' and s['home_state'] == 'active', (result, s)
    return s


def case_r01():
    """Reconcile flag off (prototype on) never reads the provider inventory."""
    set_param(RECONCILE_FLAG, 'False')
    home, actor, op, name = make_unknown('r01-flag-off', 'redirect307')
    before = fake.count('INVENTORY')
    cron(10, 'r01-cron')
    assert reconcile('r01-reconcile') == {}
    s = snapshot(op)
    assert s['phase'] == 'recovery_required' and s['error_code'] == 'create_outcome_unknown' and s['home_tunnel_id'] is None
    assert fake.count('INVENTORY') == before and fake.count('CREATE', name) == 1
    save('flag-off-no-inventory-read', inventory_reads=0, phase=s['phase'])
    set_param(RECONCILE_FLAG, 'True')
    cron(10, 'r01-enabled-cron')  # adopt + worker convergence in one pass
    s = snapshot(op)
    assert s['phase'] == 'completed' and s['home_tunnel_id'] == fake.ids_for(name)[0] and fake.count('CREATE', name) == 1
    save('enabled-cron-adopts-and-converges-in-one-pass', remote_creates=1, phase='completed')


def case_r02():
    """Worker SIGKILLed after provider create, before the response."""
    home, actor = fixture('r02-kill-create')
    op, _ = enqueue(home, actor)
    fake.barrier(); fake.create_mode = 'pause_create'
    p = spawn(['worker', op], 'r02-kill-create')
    assert fake.entered.wait(30)
    assert snapshot(op)['phase'] == 'create_unknown'
    p.kill(); p.wait(timeout=10); fake.release.set(); fake.create_mode = 'normal'
    write_op(op, lease_until=now() - timedelta(seconds=1))
    worker([op], 'r02-restart')
    s = snapshot(op); name = s['name']
    assert s['phase'] == 'recovery_required' and s['error_code'] == 'create_worker_interrupted' and s['remote_tunnel_id'] is None
    outcome = reconcile('r02-reconcile')
    assert outcome == {op: 'adopted'}, outcome
    s = snapshot(op)
    provider_id = fake.ids_for(name)
    assert len(provider_id) == 1 and s['phase'] == 'anchored' and s['home_tunnel_id'] == s['remote_tunnel_id'] == provider_id[0]
    converge(op, 'r02')
    assert fake.count('CREATE', name) == 1 and fake.token_reads(provider_id[0]) == 1
    save('kill-after-create-adopt-exactly-one', remote_creates=1, adopted_equals_provider_id=True, phase='completed')


def case_r03():
    for mode in ('redirect307', 'malformed201'):
        home, actor, op, name = make_unknown('r03-' + mode, mode)
        assert reconcile('r03-' + mode + '-reconcile') == {op: 'adopted'}
        s = converge(op, 'r03-' + mode)
        assert s['home_tunnel_id'] == fake.ids_for(name)[0] and fake.count('CREATE', name) == 1
        assert not any(c['path'] == '/api/tunnels/redirect-target' for c in fake.calls)
        save(mode + '-unknown-adopted', remote_creates=1, redirect_target_requests=0, phase='completed')


def case_r05():
    """Late reply already stored the provider id; inventory agrees."""
    home, actor, op, s = make_late_response('r05-late')
    received = s['remote_tunnel_id']
    assert reconcile('r05-reconcile') == {op: 'adopted'}
    s = converge(op, 'r05')
    assert s['home_tunnel_id'] == received and fake.count('CREATE', s['name']) == 1
    save('late-received-id-confirmed-and-adopted', remote_creates=1, same_id=True)


def case_r06():
    """A create that never happened: two spaced absences, then one re-create."""
    home, actor, op, name = make_unknown('r06-absent', 'refuse_unknown')
    assert fake.count('CREATE_ATTEMPT_NOT_CREATED', name) == 1 and fake.count('CREATE', name) == 0
    assert reconcile('r06-first') == {op: 'absent_wait'}
    s = snapshot(op); first_absence = s['absent_since']
    assert s['phase'] == 'recovery_required' and first_absence and s['next_attempt_at'] >= first_absence + timedelta(minutes=15)
    _, retry = enqueue(home, actor)
    assert not retry['accepted'] and retry['error_code'] == 'provision_confirmation_required'
    reads = fake.count('INVENTORY')
    assert reconcile('r06-not-due') == {} and fake.count('INVENTORY') == reads
    due(op)
    assert reconcile('r06-inside-window') == {op: 'absent_wait'}
    assert snapshot(op)['absent_since'] == first_absence and snapshot(op)['phase'] == 'recovery_required'
    settle_absence(op)
    assert reconcile('r06-confirmed') == {op: 'requeued'}
    s = snapshot(op)
    assert s['phase'] == 'queued' and s['home_state'] == 'provisioning' and fake.count('CREATE', name) == 0
    s = converge(op, 'r06')
    assert fake.count('CREATE', name) == 1 and s['create_attempts'] == 2 and len(fake.ids_for(name)) == 1
    save('confirmed-absence-single-recreate', remote_creates=1, post_attempts=2, absence_observations=3, phase='completed')


def case_r07():
    """Second unknown with absence: manual, never a third POST."""
    home, actor, op, name = make_unknown('r07-absent-twice', 'refuse_unknown')
    assert reconcile('r07-a1') == {op: 'absent_wait'}
    settle_absence(op)
    assert reconcile('r07-a2') == {op: 'requeued'}
    fake.create_mode = 'refuse_unknown'
    worker([op], 'r07-second-create')
    fake.create_mode = 'normal'
    assert snapshot(op)['error_code'] == 'create_outcome_unknown' and snapshot(op)['create_attempts'] == 2
    assert reconcile('r07-b1') == {op: 'absent_wait'}
    settle_absence(op)
    assert reconcile('r07-b2') == {op: 'reconcile_absent_after_retry'}
    cron(10, 'r07-cron')
    s = snapshot(op)
    assert s['phase'] == 'recovery_required' and s['error_code'] == 'reconcile_absent_after_retry'
    assert fake.count('CREATE_ATTEMPT_NOT_CREATED', name) == 2 and fake.count('CREATE', name) == 0
    assert_not_due(op)
    save('second-absence-manual-bounded-posts', post_attempts=2, remote_creates=0, phase=s['phase'], error_code=s['error_code'])


def case_r08():
    """Two live tunnels carry the intent name: manual, nothing deleted."""
    home, actor, op, name = make_unknown('r08-duplicate', 'redirect307')
    fake.live[str(uuid.uuid4())] = name
    assert reconcile('r08-reconcile') == {op: 'reconcile_duplicate'}
    cron(10, 'r08-cron')
    s = snapshot(op)
    assert s['phase'] == 'recovery_required' and s['home_tunnel_id'] is None and s['remote_tunnel_id'] is None
    assert all(fake.token_reads(t) == 0 for t in fake.ids_for(name)) and fake.count('DELETE') == 0
    assert_not_due(op)
    save('duplicate-provider-resources-manual', provider_resources=2, adopted=False, remote_deletes=0)


def case_r09():
    """Received id differs from the single live tunnel: manual."""
    home, actor, op, s = make_late_response('r09-conflict')
    del fake.live[s['remote_tunnel_id']]
    fake.live[str(uuid.uuid4())] = s['name']
    assert reconcile('r09-reconcile') == {op: 'reconcile_identity_conflict'}
    s2 = snapshot(op)
    assert s2['remote_tunnel_id'] == s['remote_tunnel_id'] and s2['home_tunnel_id'] is None
    assert_not_due(op)
    save('received-id-conflict-manual', adopted=False, received_id_kept=True)


def case_r10():
    """Received id vanished: manual, never re-created."""
    home, actor, op, s = make_late_response('r10-vanished')
    del fake.live[s['remote_tunnel_id']]
    assert reconcile('r10-reconcile') == {op: 'reconcile_anchor_missing'}
    cron(10, 'r10-cron')
    assert snapshot(op)['phase'] == 'recovery_required' and fake.count('CREATE', s['name']) == 1
    assert_not_due(op)
    save('received-id-vanished-manual-no-recreate', remote_creates=1)


def case_r11():
    """Authority revoked before reconcile: evidence only, home unbound."""
    home, actor, op, name = make_unknown('r11-revoked', 'redirect307')
    with registry.cursor() as cr:
        api.Environment(cr, SUPERUSER_ID, {})['res.users'].browse(actor).write({'active': False})
    assert reconcile('r11-reconcile') == {op: 'reconcile_authority_invalid'}
    s = snapshot(op); provider_id = fake.ids_for(name)[0]
    assert s['remote_tunnel_id'] == provider_id and s['home_tunnel_id'] is None and fake.token_reads(provider_id) == 0
    assert_not_due(op)
    save('revoked-authority-evidence-only', remote_creates=1, received_id_recorded=True, home_unbound=True, token_reads=0)


def case_r12():
    """Unavailable/partial provider reads back off, then exhaust to manual."""
    home, actor, op, name = make_unknown('r12-backoff', 'redirect307')
    fake.inventory_script = ['503']
    start = now()
    assert reconcile('r12-1') == {op: 'backoff'}
    s = snapshot(op)
    assert s['retry_count'] == 1 and start + timedelta(minutes=4) < s['next_attempt_at'] < now() + timedelta(minutes=6)
    reads = fake.count('INVENTORY')
    assert reconcile('r12-not-due') == {} and fake.count('INVENTORY') == reads
    outcomes = []
    for i in range(2, 9):
        due(op)
        fake.inventory_script = ['partial' if i % 2 else '503']
        outcomes.append(reconcile('r12-%d' % i).get(op))
    s = snapshot(op)
    assert outcomes == ['backoff'] * 6 + ['reconcile_exhausted'], outcomes
    assert s['home_tunnel_id'] is None and s['error_code'] == 'reconcile_exhausted' and fake.count('INVENTORY') == reads + 7
    assert_not_due(op)
    save('unavailable-and-partial-backoff-then-exhausted', observations=8, adopted=False, partial_reads=3)


def case_r13():
    """Provider binding flips during the observation: manual."""
    home, actor, op, name = make_unknown('r13-binding', 'redirect307')
    fake.binding_script = ['synthetic-account', 'other-synthetic-account']
    assert reconcile('r13-reconcile') == {op: 'reconcile_binding_changed'}
    assert snapshot(op)['home_tunnel_id'] is None
    assert_not_due(op)
    save('binding-drift-during-observation-manual', adopted=False)


def case_r14():
    """Environment mismatch backs off; recovers once the pinned env matches."""
    home, actor, op, name = make_unknown('r14-env', 'redirect307')
    fake.paas_env = 'stg'
    assert reconcile('r14-mismatch') == {op: 'backoff'}
    fake.paas_env = 'prod'
    due(op)
    assert reconcile('r14-match') == {op: 'adopted'}
    converge(op, 'r14')
    assert fake.count('CREATE', name) == 1
    save('environment-mismatch-backoff-then-adopt', remote_creates=1)


def case_r15():
    """Reconciler process SIGKILLed after its claim commit, mid provider read."""
    home, actor, op, name = make_unknown('r15-kill-reconcile', 'redirect307')
    fake.barrier(); fake.inventory_script = ['pause']
    p = spawn(['reconcile', 50], 'r15-killed')
    assert fake.entered.wait(60)
    s = snapshot(op)
    assert s['claim'] and s['next_attempt_at'] > now() and s['phase'] == 'recovery_required'
    p.kill(); p.wait(timeout=10); fake.release.set()
    reads = fake.count('INVENTORY')
    assert reconcile('r15-while-claimed') == {} and fake.count('INVENTORY') == reads
    due(op)
    assert reconcile('r15-after-lease') == {op: 'adopted'}
    converge(op, 'r15')
    assert fake.count('CREATE', name) == 1
    save('kill-reconciler-after-claim-then-takeover', remote_creates=1, double_observation=False)


def case_r16():
    """Two reconciler processes: single observer; a superseded claim applies nothing."""
    home, actor, op, name = make_unknown('r16a-concurrent', 'redirect307')
    fake.barrier(); fake.inventory_script = ['pause']
    a = spawn(['reconcile', 50], 'r16a-first')
    assert fake.entered.wait(60)
    reads = fake.count('INVENTORY')
    b = run(['reconcile', 50], 'r16a-second')
    assert b == {'outcomes': {}} and fake.count('INVENTORY') == reads, b
    fake.release.set()
    first = finish(a, 'r16a-first')
    assert first == {'outcomes': {str(op): 'adopted'}}, first
    converge(op, 'r16a')
    save('concurrent-reconcilers-single-observer', provider_reads_during_claim=1, adoptions=1)

    home, actor, op, name = make_unknown('r16b-stale', 'redirect307')
    fake.barrier(); fake.inventory_script = ['pause', '503']
    a = spawn(['reconcile', 50], 'r16b-stale')
    assert fake.entered.wait(60)
    due(op)
    superseding = run(['reconcile', 50], 'r16b-superseding')
    assert superseding == {'outcomes': {str(op): 'backoff'}}, superseding
    fake.release.set()
    stale = finish(a, 'r16b-stale')
    s = snapshot(op)
    assert stale == {'outcomes': {str(op): 'stale'}}, stale
    assert s['phase'] == 'recovery_required' and s['retry_count'] == 1 and s['home_tunnel_id'] is None, s
    due(op)
    assert reconcile('r16b-final') == {op: 'adopted'}
    converge(op, 'r16b')
    save('superseded-claim-applies-nothing', stale_outcome='stale', adoptions=1)


def case_r17():
    """Fairness: due, permanently failing anchored rows cannot starve a new intent."""
    failing = []
    for i in range(10):
        home, actor = fixture('r17-failing-%d' % i)
        op, _ = enqueue(home, actor)
        fake.poisoned.add(snapshot(op)['name'])
        failing.append(op)
    worker(failing, 'r17-initial-failures')
    for op in failing:
        s = snapshot(op)
        assert s['phase'] == 'anchored' and s['retry_count'] == 1 and s['next_attempt_at'] > now(), s
        due(op)
    home, actor = fixture('r17-fresh')
    fresh, _ = enqueue(home, actor)
    cron(10, 'r17-cron')
    s = snapshot(fresh)
    assert s['phase'] == 'completed' and s['home_state'] == 'active', s
    retried = [op for op in failing if snapshot(op)['retry_count'] == 2]
    assert len(retried) == 9, len(retried)
    assert all(snapshot(op)['next_attempt_at'] > now() for op in retried)
    fake.poisoned.clear()
    for op in failing:
        due(op)
    cron(20, 'r17-recovery-cron')
    assert all(snapshot(op)['phase'] == 'completed' for op in failing)
    save('backoff-fairness-no-starvation', failing_rows=10, fresh_completed_first_pass=True, failing_retried_in_pass=len(retried))


def case_r18():
    """Retention: old completed rows are purged; manual rows are kept."""
    home, actor, done, name = make_unknown('r18-completed', 'redirect307')
    assert reconcile('r18-adopt') == {done: 'adopted'}
    converge(done, 'r18')
    home, actor, manual, name = make_unknown('r18-manual', 'redirect307')
    fake.live[str(uuid.uuid4())] = name
    assert reconcile('r18-duplicate') == {manual: 'reconcile_duplicate'}
    manual_home = home
    with registry.cursor() as cr:
        cr.execute("UPDATE woow_paas_platform_smart_home_provision_operation SET write_date = now() at time zone 'UTC' - interval '31 days' WHERE id IN %s", [(done, manual)])
    cron(10, 'r18-cron')
    assert snapshot(done) == {'exists': False}
    assert snapshot(manual)['error_code'] == 'reconcile_duplicate'
    with registry.cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {})
        blocked = False
        try:
            env[HOME].browse(manual_home).unlink()
        except Exception as exc:
            blocked = type(exc).__name__ == 'UserError'
        cr.rollback()
    with registry.cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {})
        assert blocked and env[HOME].browse(manual_home).exists()
    save('retention-purges-completed-keeps-manual', purged=1, manual_kept=True, manual_home_delete_blocked=True)


CASES = {
    'r01': case_r01, 'r02': case_r02, 'r03': case_r03, 'r05': case_r05,
    'r06': case_r06, 'r07': case_r07, 'r08': case_r08, 'r09': case_r09,
    'r10': case_r10, 'r11': case_r11, 'r12': case_r12, 'r13': case_r13,
    'r14': case_r14, 'r15': case_r15, 'r16': case_r16, 'r17': case_r17,
    'r18': case_r18,
}

try:
    selected = [c for c in os.environ.get('SHC_RECONCILE_QA_CASES', '').split(',') if c] or list(CASES)
    set_param(RECONCILE_FLAG, 'True')
    for key in selected:
        print('CASE_START=' + key, flush=True)
        CASES[key]()
    assert fake.count('DELETE') == 0
    save('no-automatic-compensation', remote_deletes=0)
    print(json.dumps({'result_path': str(ROOT / 'result.json'), 'passed_cases': len(results)}), flush=True)
finally:
    fake.release.set()
    fake.server.shutdown()
    fake.server.server_close()

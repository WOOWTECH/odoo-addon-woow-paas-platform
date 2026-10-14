# Isolated QA only: no production clone, keys, namespaces, or provider endpoints.
# Caller must verify namespace/Pod UID before exec; this process has no SA token.
"""Own empty QA DB only: real ORM/worker processes, committed cursors, localhost fake provider.
The provider is operator-shaped in this first slice, not a live Cloudflare API.
"""
from pathlib import Path
from datetime import timedelta
import json, os, signal, subprocess, sys, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB=os.environ.get('SHC_PROTOTYPE_QA_DB', '')
assert DB.startswith('woow_shc_c1_proto_') and DB.replace('_','').isalnum(), 'explicit own empty prototype QA DB required'
assert os.environ.get('SHC_PROTOTYPE_QA_NAMESPACE', '').startswith('dev-sandbox-shc-audit-'), 'explicit isolated QA namespace marker required'
MODEL='woow_paas_platform.smart_home_provision_operation'
ROOT=Path('/tmp/shc-c1-process-evidence-r2')
ROOT.mkdir(exist_ok=True)
import odoo
from odoo import api, SUPERUSER_ID, fields
odoo.tools.config.parse_config(['-c','/etc/odoo/odoo.conf','-d',DB,'--addons-path=/tmp/shc-c1-addons,/usr/lib/python3/dist-packages/odoo/addons,/opt/woow/addons','--max-cron-threads=0','--workers=0','--no-http','--smtp=127.0.0.1','--smtp-port=9'])
assert odoo.tools.config['db_host'].startswith('shc-audit-') and odoo.tools.config['db_host'].endswith('-odoo-dev-sandbox-postgres'), 'own QA postgres service required'
registry=odoo.registry(DB)

def invoke(operation_id):
    with registry.cursor() as cr:
        env=api.Environment(cr,SUPERUSER_ID,{})
        return env[MODEL]._process_one(operation_id)

if len(sys.argv)>1 and sys.argv[1]=='worker':
    result=invoke(int(sys.argv[2]));print(json.dumps({'worker_result':result}),flush=True);sys.exit(0)

from odoo.addons.woow_paas_platform.tests.smart_home_fixture import sh_model
from odoo.addons.woow_paas_platform.models.smart_home_provision_operation import _internal_operation

# Real independent user process. The stale-cache case commits its read barrier
# before a new cached read and waits for another process to commit. Actual
# Odoo HTTP retrying handles serialization; no controller commits early.
if len(sys.argv)>1 and sys.argv[1] in ('retry-after-completion', 'retry-while-worker-locked'):
    from odoo.exceptions import UserError
    mode=sys.argv[1];home_id=int(sys.argv[2]);actor_id=int(sys.argv[3])
    label=sys.argv[4];assert label.replace('-','').isalnum()
    result_path=ROOT/(label+'-result.json')
    with registry.cursor() as cr:
        env=api.Environment(cr,actor_id,{})
        home=env['woow_paas_platform.smart_home'].sudo().browse(home_id)
        op=env[MODEL]._for_home(home)
        initial={'phase':op.phase,'home_state':home.state}
        if mode=='retry-after-completion':
            assert initial=={'phase':'anchored','home_state':'error'}
            cr.commit()
            # Odoo clears caches on commit. Read anchored/error again in the
            # new repeatable-read transaction before the other worker commits.
            initial={'phase':op.phase,'home_state':home.state}
            assert initial=={'phase':'anchored','home_state':'error'}
            (ROOT/(label+'-ready')).write_text('committed barrier then cached anchored/error in new transaction')
            deadline=time.monotonic()+45
            while not (ROOT/(label+'-release')).exists():
                assert time.monotonic()<deadline,'bounded process barrier timed out'
                time.sleep(.05)
        start=time.monotonic();busy=False;outcome=None
        try:
            if mode=='retry-after-completion':
                from odoo.service.model import retrying
                outcome=retrying(lambda:home.action_provision(),env)
            else:outcome=home.action_provision()
        except UserError:
            assert mode=='retry-while-worker-locked'
            busy=True
        evidence={'initial':initial,'busy':busy,'seconds':round(time.monotonic()-start,3),
                  'success':outcome.get('success') if outcome else False,
                  'accepted':outcome.get('accepted') if outcome else False,
                  'home_state':home.state,'phase':op.phase}
        if mode=='retry-after-completion':
            assert evidence['success'] and not evidence['accepted']
            assert home.state=='active' and op.phase=='completed'
        else:
            assert busy and evidence['seconds']<5,'retry must not wait behind worker then deadlock'
        cr.commit();result_path.write_text(json.dumps(evidence,indent=2))
    sys.exit(0)

class FakeOperator:
    def __init__(self):
        self.calls=[];self.tunnels={};self.account='synthetic-account'
        self.lock=threading.Lock();self.mode='normal';self.entered=threading.Event();self.release=threading.Event()
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def send(self,data,code=200):
                body=json.dumps(data).encode();self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers()
                try:self.wfile.write(body)
                except (BrokenPipeError,ConnectionResetError):pass
            def record(self,method,path,**values):
                with owner.lock:
                    event={'method':method,'path':path,**values};owner.calls.append(event)
                    with (ROOT/'fake-provider-ledger.jsonl').open('a') as f:
                        f.write(json.dumps(event)+'\n');f.flush();os.fsync(f.fileno())
            def do_GET(self):
                self.record('GET',self.path)
                if self.path=='/api/tunnels/provider-binding':return self.send({'provider_account_id':owner.account})
                if self.path.endswith('/token'):
                    tunnel_id=self.path.split('/')[-2]
                    if tunnel_id not in owner.tunnels:return self.send({'detail':'unknown synthetic tunnel'},404)
                    if owner.mode=='pause_token':owner.entered.set();owner.release.wait(45)
                    if owner.mode=='token_error':return self.send({'detail':'synthetic token unavailable'},503)
                    return self.send({'token':'synthetic-only-token'})
                return self.send({'detail':'synthetic unavailable'},404)
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path!='/api/tunnels/bare':
                    self.record('POST',self.path);return self.send({'detail':'legacy mutations forbidden'},503)
                if body['expected_account_id']!=owner.account:
                    self.record('POST_PREFLIGHT_REFUSED',self.path);return self.send({'detail':{'error':'provider_binding_changed','outcome':'not_created'}},409)
                tunnel_id=str(uuid.uuid4());owner.tunnels[tunnel_id]=body['name']
                self.record('CREATE',self.path,tunnel_id=tunnel_id,name=body['name'])
                if owner.mode in ('redirect307','redirect308'):
                    self.send_response(int(owner.mode[-3:]));self.send_header('Location','/api/tunnels/redirect-target');self.send_header('Content-Length','0');self.end_headers();return
                if owner.mode=='malformed201':return self.send({'unexpected':'synthetic-created-but-unusable-response'},201)
                if owner.mode=='pause_create':owner.entered.set();owner.release.wait(45)
                return self.send({'tunnel_id':tunnel_id,'tunnel_name':body['name'],'provider_account_id':owner.account},201)
            def do_PUT(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                self.record('PUT',self.path)
                if self.path.endswith('/config'):
                    tunnel_id=self.path.split('/')[-2]
                    if tunnel_id not in owner.tunnels or body.get('tunnel_name')!=owner.tunnels[tunnel_id]:
                        return self.send({'detail':'synthetic ownership mismatch'},409)
                    assert body['hostname'].endswith('.shc-prototype.invalid')
                    assert body['service_url'] in ('http_status:503','http://localhost:8123')
                    return self.send({'message':'Synthetic configuration accepted','hostname':body['hostname'],'service_url':body['service_url']})
                if self.path.endswith('/routes'):
                    tunnel_id=self.path.split('/')[-2]
                    if tunnel_id not in owner.tunnels:return self.send({'detail':'unknown synthetic tunnel'},404)
                    if owner.mode=='routes_error':return self.send({'detail':'synthetic route unavailable'},503)
                    return self.send({'tunnel_id':tunnel_id,'routes':[{'subdomain':r['subdomain'],'hostname':r['subdomain']+'.shc-prototype.invalid','dns_record_id':'synthetic-dns-id'} for r in body['routes']],'dns_failed':False})
                if self.path=='/api/access/sync':return self.send({'app_id':str(uuid.uuid4()),'policy_ids':[],'orphaned_policy_ids':[]})
                return self.send({'detail':'unexpected mutation'},503)
            def do_DELETE(self):
                self.record('DELETE',self.path);return self.send({'detail':'automatic compensation forbidden'},503)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url='http://127.0.0.1:'+str(self.server.server_port)
    def barrier(self,mode):
        self.mode=mode;self.entered.clear();self.release.clear()
    def count_create(self,home_name=None):
        return sum(c['method']=='CREATE' and (not home_name or c['name']==home_name) for c in self.calls)

fake=FakeOperator();results=[]
def committed_env():
    return registry.cursor()
def snapshot(op_id):
    with registry.cursor() as cr:
        env=api.Environment(cr,SUPERUSER_ID,{})
        op=env[MODEL].browse(op_id);h=op.home_id
        return {'operation_id':op.id,'phase':op.phase,'home_id':h.id,'home_state':h.state,'remote_tunnel_id':op.remote_tunnel_id or None,'home_tunnel_id':h.tunnel_id or None,'name':op.tunnel_name,'uuid':op.operation_uuid}
def child(op_id,label):
    p=ROOT/(label+'.log')
    with p.open('w') as log:
        return subprocess.Popen([sys.executable,__file__,'worker',str(op_id)],stdout=log,stderr=subprocess.STDOUT)
def finish(p):
    p.wait(timeout=45);assert p.returncode==0,('worker exit',p.returncode)
def expire(op_id):
    with registry.cursor() as cr:
        env=api.Environment(cr,SUPERUSER_ID,{})
        _internal_operation(env[MODEL].browse(op_id)).write({'lease_until':fields.Datetime.now()-timedelta(seconds=1)})
def fixture(label):
    with registry.cursor() as cr:
        env=api.Environment(cr,SUPERUSER_ID,{})
        params=env['ir.config_parameter'].sudo()
        params.set_param('woow_paas_platform.shc_durable_provision_prototype_enabled','True')
        params.set_param('woow_paas_platform.operator_url',fake.url)
        params.set_param('woow_paas_platform.operator_api_key','synthetic-only-key')
        params.set_param('woow_paas_platform.paas_domain','shc-prototype.invalid')
        portal=env['res.users'].with_context(no_reset_password=True).create({'name':'C1 '+label,'login':'c1-'+str(uuid.uuid4())+'@example.invalid','groups_id':[(6,0,[env.ref('base.group_portal').id])]})
        plan=env['sh.subscription.plan'].create({'name':'C1 '+label,'sh_plan_price':0,'sh_duration':1,'sh_unit':'month','sh_no_of_billing_cycle':-1})
        product=env['product.template'].create({'name':'C1 '+label,'type':'service','sh_product_subscribe':True,'sh_subscription_plan_id':plan.id})
        ws=env['woow_paas_platform.workspace'].create({'name':'C1 '+label,'owner_id':portal.id})
        sub=env['sh.subscription.subscription'].create({'sh_partner_id':portal.partner_id.id,'product_id':product.product_variant_id.id,'sh_subscription_plan_id':plan.id,'sh_source':'manual','state':'in_progress'})
        home=sh_model(env).create({'name':'C1 '+label,'workspace_id':ws.id,'subscription_id':sub.id})
        return home.id,portal.id

def enqueue(home_id,actor_id):
    with registry.cursor() as cr:
        env=api.Environment(cr,actor_id,{})
        home=env['woow_paas_platform.smart_home'].sudo().browse(home_id)
        outcome=home.action_provision();op=env[MODEL]._for_home(home)
        return op.id,outcome

def save(label,**evidence):
    results.append({'case':label,'passed':True,**evidence})
    (ROOT/'result.json').write_text(json.dumps({'prototype':True,'commercial_c1_fixed':False,'fake_boundary':'operator-shaped localhost HTTP; not actual Cloudflare router in this first slice','db':DB,'cases':results},indent=2)+'\n')

try:
    home,actor=fixture('rollback-before-commit')
    try:
        with registry.cursor() as cr:
            env=api.Environment(cr,actor,{})
            outcome=env['woow_paas_platform.smart_home'].sudo().browse(home).action_provision()
            assert outcome['accepted'] and not outcome['success']
            raise RuntimeError('intentional request rollback')
    except RuntimeError:pass
    with registry.cursor() as cr:
        env=api.Environment(cr,SUPERUSER_ID,{})
        assert not env[MODEL].search_count([('home_id','=',home)])
        assert env['woow_paas_platform.smart_home'].browse(home).state=='pending'
    assert fake.count_create()==0
    save('rollback-before-commit',remote_creates=0,committed_intent=False)

    home,actor=fixture('durable-intent-lost-user-response')
    op,outcome=enqueue(home,actor);again,repeated=enqueue(home,actor)
    assert op==again and outcome['accepted'] and repeated['accepted']
    assert snapshot(op)['phase']=='queued' and fake.count_create()==0
    save('durable-intent-lost-user-response',same_operation=True,remote_creates=0)

    fake.barrier('pause_create')
    p=child(op,'parallel-first');assert fake.entered.wait(30)
    assert snapshot(op)['phase']=='create_unknown' and snapshot(op)['home_tunnel_id'] is None
    second=child(op,'parallel-second');finish(second)
    assert fake.count_create()==1
    fake.release.set();finish(p);fake.mode='normal'
    s=snapshot(op);assert s['phase']=='completed' and s['home_state']=='active'
    assert s['home_tunnel_id']==s['remote_tunnel_id'] and fake.count_create(s['name'])==1
    save('parallel-workers-one-create',remote_creates=1,committed_marker_before_response=True,completed=True)

    home,actor=fixture('kill-after-provider-create-before-response');op,_=enqueue(home,actor)
    fake.barrier('pause_create');p=child(op,'kill-create');assert fake.entered.wait(30)
    assert snapshot(op)['phase']=='create_unknown'
    p.kill();p.wait(timeout=10);fake.release.set();fake.mode='normal';expire(op)
    restart=child(op,'kill-create-restarted');finish(restart)
    s=snapshot(op);_,retry=enqueue(home,actor)
    assert s['phase']=='recovery_required' and s['home_tunnel_id'] is None
    assert not retry['accepted'] and fake.count_create(s['name'])==1
    with registry.cursor() as cr:
        env=api.Environment(cr,SUPERUSER_ID,{})
        env['ir.config_parameter'].set_param('woow_paas_platform.shc_durable_provision_prototype_enabled','False')
    _,retry_disabled=enqueue(home,actor)
    assert not retry_disabled['accepted'] and fake.count_create(s['name'])==1
    save('kill-after-provider-create-before-response',remote_creates=1,unknown_fail_closed=True,guard_survives_flag_disabled=True)

    home,actor=fixture('kill-after-anchor-before-token');op,_=enqueue(home,actor)
    fake.barrier('pause_token');p=child(op,'kill-token');assert fake.entered.wait(30)
    before=snapshot(op);assert before['home_tunnel_id'] and before['home_tunnel_id']==before['remote_tunnel_id']
    p.kill();p.wait(timeout=10);fake.release.set();fake.mode='normal';expire(op)
    restart=child(op,'kill-token-restarted');finish(restart)
    after=snapshot(op);assert after['phase']=='completed' and after['home_tunnel_id']==before['home_tunnel_id']
    assert fake.count_create(after['name'])==1
    save('kill-after-anchor-before-token',remote_creates=1,anchor_committed_before_token=True,same_id_after_restart=True)

    for mode in ['token_error','routes_error']:
        home,actor=fixture(mode);op,_=enqueue(home,actor);fake.mode=mode
        p=child(op,mode);finish(p);failed=snapshot(op)
        assert failed['phase']=='anchored' and failed['home_state']=='error'
        assert failed['home_tunnel_id']==failed['remote_tunnel_id'] and failed['home_tunnel_id']
        repeated,outcome=enqueue(home,actor);queued=snapshot(op)
        assert repeated==op and outcome['accepted'] and not outcome['success']
        assert queued['home_state']=='provisioning' and queued['phase']=='anchored'
        assert queued['home_tunnel_id']==failed['home_tunnel_id'] and fake.count_create(queued['name'])==1
        fake.mode='normal';restart=child(op,mode+'-restarted');finish(restart);after=snapshot(op)
        assert after['phase']=='completed' and after['home_tunnel_id']==failed['home_tunnel_id']
        assert fake.count_create(after['name'])==1
        save(mode+'-same-id-retry',remote_creates=1,same_id_after_retry=True,committed_user_retry_state='provisioning')

    home,actor=fixture('retry-interleaves-worker-completion');op,_=enqueue(home,actor)
    fake.mode='token_error';p=child(op,'retry-race-initial-failure');finish(p)
    assert snapshot(op)['phase']=='anchored' and snapshot(op)['home_state']=='error'
    label='retry-completed-cache'
    for suffix in ['-ready','-release','-result.json']:
        (ROOT/(label+suffix)).unlink(missing_ok=True)
    with (ROOT/(label+'.log')).open('w') as log:
        stale=subprocess.Popen([sys.executable,__file__,'retry-after-completion',str(home),str(actor),label],stdout=log,stderr=subprocess.STDOUT)
    deadline=time.monotonic()+30
    while not (ROOT/(label+'-ready')).exists():
        assert stale.poll() is None and time.monotonic()<deadline
        time.sleep(.05)
    fake.barrier('pause_token');worker=child(op,'retry-race-completing-worker')
    assert fake.entered.wait(30)
    busy_label='retry-while-locked'
    with (ROOT/(busy_label+'.log')).open('w') as log:
        busy=subprocess.Popen([sys.executable,__file__,'retry-while-worker-locked',str(home),str(actor),busy_label],stdout=log,stderr=subprocess.STDOUT)
    busy.wait(timeout=15);assert busy.returncode==0
    busy_evidence=json.loads((ROOT/(busy_label+'-result.json')).read_text())
    assert busy_evidence['busy'] and busy_evidence['seconds']<5
    fake.release.set();finish(worker);fake.mode='normal'
    completed=snapshot(op);assert completed['phase']=='completed' and completed['home_state']=='active'
    (ROOT/(label+'-release')).write_text('worker completed in a separate committed process')
    finish(stale)
    stale_evidence=json.loads((ROOT/(label+'-result.json')).read_text())
    after=snapshot(op);assert after['home_state']=='active' and after['phase']=='completed'
    assert after['home_tunnel_id']==completed['home_tunnel_id'] and fake.count_create(after['name'])==1
    save('retry-while-worker-locked',remote_creates=1,deadlock=False,controlled_busy=True,seconds=busy_evidence['seconds'])
    save('retry-after-worker-completion-with-stale-cache',remote_creates=1,completed_not_downgraded=True,actual_odoo_http_serialization_retry=True,
         stale_cached_phase=stale_evidence['initial']['phase'],fresh_phase=stale_evidence['phase'],success=True)

    home,actor=fixture('requester-revoked-before-create');op,_=enqueue(home,actor)
    with registry.cursor() as cr:
        env=api.Environment(cr,SUPERUSER_ID,{})
        env['res.users'].browse(actor).write({'active':False})
    p=child(op,'revoked-preflight');finish(p);s=snapshot(op)
    assert s['phase']=='recovery_required' and fake.count_create(s['name'])==0
    save('requester-revoked-before-create',remote_creates=0,phase='recovery_required')

    home,actor=fixture('provider-binding-drift-before-create');op,_=enqueue(home,actor)
    fake.account='different-synthetic-account'
    p=child(op,'binding-preflight');finish(p);s=snapshot(op)
    assert s['phase']=='recovery_required' and fake.count_create(s['name'])==0
    fake.account='synthetic-account'
    save('provider-binding-drift-before-create',remote_creates=0,phase='recovery_required')

    home,actor=fixture('revoked-after-provider-create');op,_=enqueue(home,actor)
    fake.barrier('pause_create');p=child(op,'revoked-after-create');assert fake.entered.wait(30)
    with registry.cursor() as cr:
        env=api.Environment(cr,SUPERUSER_ID,{})
        env['res.users'].browse(actor).write({'active':False})
    fake.release.set();finish(p);fake.mode='normal';s=snapshot(op)
    assert s['phase']=='recovery_required' and s['remote_tunnel_id'] and s['home_tunnel_id'] is None
    assert fake.count_create(s['name'])==1
    assert not any(c['path'].endswith('/token') and s['remote_tunnel_id'] in c['path'] for c in fake.calls)
    save('revoked-after-provider-create',remote_creates=1,received_id_durable=True,home_unbound=True,no_token_or_configuration=True)

    home,actor=fixture('late-reply-after-lease-quarantine');op,_=enqueue(home,actor)
    fake.barrier('pause_create');p=child(op,'late-response-original');assert fake.entered.wait(30)
    expire(op);second=child(op,'late-response-expiry');finish(second)
    assert snapshot(op)['phase']=='recovery_required'
    fake.release.set();finish(p);s=snapshot(op)
    assert s['remote_tunnel_id'] and s['home_tunnel_id'] is None
    assert fake.count_create(s['name'])==1
    save('late-response-after-lease-quarantine',received_id_durable=True,home_unbound=True,remote_creates=1)

    for mode in ('redirect307','redirect308','malformed201'):
        home,actor=fixture(mode);op,_=enqueue(home,actor);fake.mode=mode
        first=child(op,mode+'-first');finish(first);s=snapshot(op)
        assert s['phase']=='recovery_required' and s['home_state']=='error'
        assert s['remote_tunnel_id'] is None and s['home_tunnel_id'] is None
        assert fake.count_create(s['name'])==1
        assert not any(c['path']=='/api/tunnels/redirect-target' for c in fake.calls),'redirect was followed'
        fake.mode='normal';restarted=child(op,mode+'-restarted');finish(restarted)
        _,outcome=enqueue(home,actor);after=snapshot(op)
        assert not outcome['accepted'] and after['phase']=='recovery_required'
        assert fake.count_create(s['name'])==1,'unknown outcome caused a duplicate create'
        save(mode+'-durable-quarantine',remote_creates=1,phase=after['phase'],redirect_target_requests=0,no_recreate_after_worker_restart=True,no_recreate_after_user_retry=True)

    assert not any(c['method']=='DELETE' for c in fake.calls)
    save('no-automatic-adoption-or-compensation',remote_deletes=0)
    print(json.dumps({'result_path':str(ROOT/'result.json'),'passed_cases':len(results)}),flush=True)
finally:
    fake.release.set();fake.server.shutdown();fake.server.server_close()

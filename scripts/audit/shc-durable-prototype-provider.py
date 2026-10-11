# Isolated QA only: no production clone, keys, namespaces, or provider endpoints.
# Caller must verify namespace/Pod UID before exec; this process has no SA token.
"""Own empty QA DB only: real ORM/worker processes, committed cursors, localhost fake provider.
The provider is operator-shaped in this first slice, not a live Cloudflare API.
"""
from pathlib import Path
from datetime import timedelta
import json, os, signal, socket, subprocess, sys, threading, time, uuid
from urllib.request import Request,urlopen
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB=os.environ.get('SHC_PROTOTYPE_QA_DB', '')
assert DB.startswith('woow_shc_c1_proto_') and DB.replace('_','').isalnum(), 'explicit own empty prototype QA DB required'
assert os.environ.get('SHC_PROTOTYPE_QA_NAMESPACE', '').startswith('dev-sandbox-shc-audit-'), 'explicit isolated QA namespace marker required'
MODEL='woow_paas_platform.smart_home_provision_operation'
ROOT=Path('/tmp/shc-c1-actual-cf-evidence-r2')
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

class FakeCloudflare:
    def __init__(self):
        self.calls=[];self.tunnels={};self.lock=threading.Lock();self.mode='normal'
        self.entered=threading.Event();self.release=threading.Event();owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def send(self,data,code=200):
                payload=json.dumps(data).encode();self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(payload)));self.end_headers()
                try:self.wfile.write(payload)
                except (BrokenPipeError,ConnectionResetError):pass
            def record(self,method,**extra):
                with owner.lock:
                    event={'method':method,'path':self.path,**extra};owner.calls.append(event)
                    with (ROOT/'fake-cf-ledger.jsonl').open('a') as f:
                        f.write(json.dumps(event)+'\n');f.flush();os.fsync(f.fileno())
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path!='/client/v4/accounts/synthetic-account/cfd_tunnel':
                    self.record('UNEXPECTED_POST');return self.send({'success':False,'errors':['unexpected fixture mutation']},503)
                assert body['tunnel_secret'] and body['name']
                tunnel_id=str(uuid.uuid4());owner.tunnels[tunnel_id]=body['name']
                self.record('CREATE',tunnel_id=tunnel_id,name=body['name'])
                if owner.mode=='pause_create':owner.entered.set();owner.release.wait(45)
                return self.send({'success':True,'result':{'id':tunnel_id,'name':body['name']}},201)
            def do_GET(self):
                self.record('GET')
                if self.path.endswith('/token'):
                    tunnel_id=self.path.split('/')[-2]
                    if owner.mode=='token_error':return self.send({'success':False,'errors':['synthetic token unavailable']},503)
                    if tunnel_id not in owner.tunnels:return self.send({'success':False,'errors':['unknown fixture tunnel']},404)
                    return self.send({'success':True,'result':'synthetic-only-token'})
                tunnel_id=self.path.split('/')[-1]
                if tunnel_id in owner.tunnels:return self.send({'success':True,'result':{'id':tunnel_id,'name':owner.tunnels[tunnel_id],'status':'inactive','deleted_at':None}})
                return self.send({'success':False,'errors':['unexpected fixture read']},503)
            def do_PUT(self):
                self.rfile.read(int(self.headers['Content-Length']));self.record('PUT')
                return self.send({'success':False,'errors':['synthetic ingress unavailable']},503)
            def do_DELETE(self):
                self.record('DELETE');return self.send({'success':False,'errors':['compensation forbidden']},503)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.cf_url='http://127.0.0.1:'+str(self.server.server_port)
    def barrier(self,mode):
        self.mode=mode;self.entered.clear();self.release.clear()
    def count_create(self,home_name=None):
        return sum(c['method']=='CREATE' and (not home_name or c['name']==home_name) for c in self.calls)

fake=FakeCloudflare();results=[]
with socket.socket() as sock:
    sock.bind(('127.0.0.1',0));operator_port=sock.getsockname()[1]
fake.url='http://127.0.0.1:'+str(operator_port)
operator_env=dict(os.environ,PYTHONPATH='/tmp/shc-c1-operator/deps:/tmp/shc-c1-operator/operator',PYTHONDONTWRITEBYTECODE='1')
with (ROOT/'actual-operator.log').open('w') as log:
    operator_process=subprocess.Popen([sys.executable,'/tmp/shc-c1-operator/operator-child.py',fake.cf_url,str(operator_port)],env=operator_env,stdout=log,stderr=subprocess.STDOUT)
for attempt in range(150):
    if operator_process.poll() is not None:raise RuntimeError('actual operator fixture startup failed; inspect private local log')
    try:
        req=Request(fake.url+'/api/tunnels/provider-binding',headers={'X-API-Key':'synthetic-only-key'})
        with urlopen(req,timeout=1) as response:assert json.load(response)=={'provider_account_id':'synthetic-account'}
        break
    except Exception:time.sleep(0.1)
else:raise RuntimeError('actual operator fixture startup timed out')

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
    (ROOT/'result.json').write_text(json.dumps({'prototype':True,'commercial_c1_fixed':False,'fake_boundary':'actual full FastAPI app/middleware/router/CloudflareService via real localhost TCP to fsynced own FakeCF ledger; lifespan disabled, no Helm/K8s startup claim','db':DB,'cases':results},indent=2)+'\n')

try:
    home,actor=fixture('actual-cf-token-error');op,_=enqueue(home,actor)
    fake.mode='token_error';p=child(op,'actual-cf-token-error');finish(p);failed=snapshot(op)
    assert failed['phase']=='anchored' and failed['home_state']=='error'
    assert failed['home_tunnel_id']==failed['remote_tunnel_id'] and failed['home_tunnel_id']
    assert fake.count_create(failed['name'])==1
    assert not any(c['method']=='PUT' for c in fake.calls)
    save('actual-provider-token-error-after-committed-anchor',remote_creates=1,anchor_durable=True,configuration_writes_before_token=0)

    fake.mode='routes_error';restart=child(op,'actual-cf-routes-error');finish(restart);after=snapshot(op)
    assert after['phase']=='anchored' and after['home_tunnel_id']==failed['home_tunnel_id']
    assert fake.count_create(after['name'])==1 and any(c['method']=='PUT' for c in fake.calls)
    save('actual-provider-ingress-error-same-id-retry',remote_creates=1,same_id_after_retry=True)

    home,actor=fixture('actual-cf-kill-create-response');op,_=enqueue(home,actor)
    fake.barrier('pause_create');p=child(op,'actual-cf-kill-create');assert fake.entered.wait(30)
    assert snapshot(op)['phase']=='create_unknown' and snapshot(op)['home_tunnel_id'] is None
    p.kill();p.wait(timeout=10);fake.release.set();fake.mode='normal';expire(op)
    restart=child(op,'actual-cf-kill-create-restarted');finish(restart);s=snapshot(op)
    assert s['phase']=='recovery_required' and s['home_tunnel_id'] is None and fake.count_create(s['name'])==1
    save('actual-provider-create-response-loss-fail-closed',remote_creates=1,no_automatic_adoption=True)

    assert not any(c['method'] in ['DELETE','UNEXPECTED_POST'] for c in fake.calls)
    save('actual-provider-no-automatic-compensation',remote_deletes=0)
    print(json.dumps({'result_path':str(ROOT/'result.json'),'passed_cases':len(results)}),flush=True)
finally:
    fake.release.set();operator_process.terminate()
    try:operator_process.wait(timeout=10)
    except subprocess.TimeoutExpired:operator_process.kill();operator_process.wait(timeout=5)
    fake.server.shutdown();fake.server.server_close()

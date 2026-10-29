"""Native ORM/HTTP caller boundaries, isolated fake external operator only."""
from copy import deepcopy
import json,uuid
from urllib.parse import urlparse
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase,HttpCase,tagged
from ..models.cloud_service import _internal_provision,_cs_skip_access_sync,_internal_cs_access_identity
from ..models.smart_home import _internal_identity as _sh_identity
from ..models.smart_home_route import _sm_skip_access_sync,_internal_sm_route_identity
from ..services.naming import make_service_subdomain,make_namespace
from ..services.paas_operator import PaaSOperatorAPIError,PaaSOperatorError
from .smart_home_fixture import sh_model
from .ns_quota_fixture import zero_managed_quota

CLIENT_PATHS=(
 'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client',
 'odoo.addons.woow_paas_platform.models.smart_home.get_paas_operator_client',
)

class FakeOperator:
    def __init__(self):
        self.events=[];self.origin=False;self.app=False;self.history=[]
        self.fail_access=False;self.fail_park=False;self.fail_uninstall=False;self.malformed=False;self.access_status=None;self.park_status='parked';self.fail_unpark=False;self.fail_publish=False
        # Opt-in provider model (default off): one Access app per domain, a repeated
        # create is refused with 409, as measured on the live account.
        self.unique_domains=False;self.domains={};self.die_after_landing=False;self.die_before_provider=False;self.next_app_no=0
    def _publish(self,target,event):
        self.origin=not target.startswith('http_status:')
        self.events.append((event,target));self.history.append((self.origin,self.app))
    def sync_access_app(self,payload):
        self.events.append(('access',deepcopy(payload)))
        if self.fail_access and payload['enabled']:
            raise PaaSOperatorError('Selected Access login method unavailable',status_code=409)
        if self.die_before_provider and payload['enabled'] and not payload.get('existing_app_id'):
            self.die_before_provider=False
            raise RuntimeError('simulated worker death before the provider was reached')
        self.app=bool(payload['enabled'])
        app_id='fake-native-app'
        if self.unique_domains and payload['enabled']:
            domain=payload['primary_hostname'];existing=payload.get('existing_app_id')
            if not existing and domain in self.domains:
                raise PaaSOperatorAPIError('application_already_exists',status_code=409)
            if existing and existing!=self.domains.get(domain,{}).get('app_id'):
                raise PaaSOperatorAPIError('application not found',status_code=404)
            if not existing:
                self.next_app_no+=1
                self.domains[domain]={'app_id':'fake-uniq-app-%d'%self.next_app_no,'name':payload['app_name']}
            app_id=self.domains[domain]['app_id']
        elif self.unique_domains and not payload['enabled']:
            self.domains={d:r for d,r in self.domains.items() if d!=payload.get('primary_hostname')}
        result = {'app_id':app_id if self.app and not self.malformed else None,
                'policy_ids':(['fake-policy-%s' % index for index, _policy
                               in enumerate(payload.get('policies', []), 1)]
                              if self.app else []),
                'allowed_idps':(['fake-idp-'+kind for kind in
                                 payload.get('login_idp_types', [])]
                                if self.app else []),
                'orphaned_policy_ids':[], 'unresolved_idp_types':[]}
        if self.access_status:
            result['status'] = self.access_status
        if self.die_after_landing and self.unique_domains and payload['enabled'] and not payload.get('existing_app_id'):
            self.die_after_landing=False
            raise RuntimeError('simulated worker death after the provider applied the create')
        return result
    def review_access_app(self,app_name,primary_hostname):
        row=self.domains.get(primary_hostname)
        if row and row['name']==app_name:
            return {'status':'unique','app_id':row['app_id'],'match_count':1}
        return {'status':'absent','app_id':None,'match_count':0}
    def park_release_route(self,*args):
        self.events.append(('park-release',None))
        if self.fail_park:raise PaaSOperatorError('fake park failed',status_code=503)
        if self.park_status in ('parked','already_parked','not_found','no_service_rule'):self.origin=False
        return {'status':self.park_status}
    def unpark_release_route(self,*args):
        if self.fail_unpark:
            self.events.append(('unpark-failed',None));raise PaaSOperatorError('fake unpark failure',status_code=502)
        self._publish('http://fake-service:80','unpark');return {'status':'unparked'}
    def configure_tunnel(self,*args,**kwargs):
        if self.fail_park and kwargs['service_url']=='http_status:503':
            self.events.append(('park-failed',None));raise PaaSOperatorError('fake park failed',status_code=503)
        if self.fail_publish and not kwargs['service_url'].startswith('http_status:'):
            self.events.append(('publish-failed',None));raise PaaSOperatorError('fake publish failure',status_code=502)
        self._publish(kwargs['service_url'],'configure');return configure_receipt(args[0], **kwargs)
    def sync_tunnel_routes(self,*args,**kwargs):
        if self.fail_park and kwargs['routes'] and all(
                r['service_url'].startswith('http_status:')
                for r in kwargs['routes']):
            self.events.append(('park-failed',None));raise PaaSOperatorError('fake park failed',status_code=503)
        if self.fail_publish and any(not r['service_url'].startswith('http_status:') for r in kwargs['routes']):
            self.events.append(('publish-failed',None));raise PaaSOperatorError('fake publish failure',status_code=502)
        for route in kwargs['routes']:self._publish(route['service_url'],'routes')
        return routes_receipt(args[0], kwargs['routes'], domain='example.test')
    def create_tunnel(self,**kwargs):
        self._publish(kwargs['service_url'],'create-tunnel')
        return {'tunnel_id':'fake-native-tunnel','tunnel_name':kwargs['name'],'tunnel_token':'fake-isolated-token'}
    def get_tunnel_status(self,*args,**kwargs):return {'status':'inactive','connections':[]}
    def delete_tunnel(self,*args,**kwargs):self.events.append(('delete-tunnel',None));self.origin=False;return {}
    def get_namespace_quota(self,namespace,**kwargs):
        # #1679：開通前先讀叢集 quota；全 0 的受管 quota 讓 create_namespace 照送目標值。
        return {'managed_quota':zero_managed_quota(namespace)} if kwargs.get('include_managed') else {}
    def create_namespace(self,*args,**kwargs):self.events.append(('namespace',None));return {}
    def install_release(self,*args,**kwargs):
        self.events.append(('install',None))
        if kwargs.get('expose'):self._publish('http://fake-service:80','install-expose')
        return {'revision':1,'chart_version':'1.0.0'}
    def scale_release(self,*args,**kwargs):self.events.append(('scale',kwargs['replicas']));return {}
    def uninstall_release(self,*args,**kwargs):
        self.events.append(('uninstall',None))
        if self.fail_uninstall:raise PaaSOperatorError('fake uninstall failure',status_code=502)
        self.origin=False;return {}
    def update_namespace_quota(self,*args,**kwargs):return {}


def blocked_http(original):
    def request(session,method,url,*args,**kwargs):
        if urlparse(url).hostname not in ('127.0.0.1','localhost','::1'):
            raise AssertionError('AC1011 external HTTP forbidden')
        return original(session,method,url,*args,**kwargs)
    return request


class Fixture:
    def install_fake(self):
        import requests
        self.client=FakeOperator()
        for path in CLIENT_PATHS:
            mock=patch(path,return_value=self.client);mock.start();self.addCleanup(mock.stop)
        guard=patch('requests.sessions.Session.request',new=blocked_http(requests.sessions.Session.request));guard.start();self.addCleanup(guard.stop)
        self.env['ir.config_parameter'].sudo().set_param('woow_paas_platform.paas_domain','example.test')
    def resources(self,owner=None):
        self.workspace=self.env['woow_paas_platform.workspace'].sudo().create({'name':'AC1011 caller isolated WS',**({'owner_id':owner.id} if owner else {})})
        self.template=self.env['woow_paas_platform.cloud_app_template'].sudo().create({
            'name':'AC1011 isolated chart','slug':'ac1011-native-'+uuid.uuid4().hex[:8],
            'helm_repo_url':'','helm_chart_name':'oci://example.invalid/ac1011',
            'helm_chart_version':'1.0.0','workload_type':'container',
            'post_deploy_init_type':'none','mcp_enabled':False,'ingress_enabled':True})
        reference=str(uuid.uuid4())
        self.cs=_internal_provision(self.env['woow_paas_platform.cloud_service'].sudo()).create({
            'name':'AC1011 native CS','workspace_id':self.workspace.id,'template_id':self.template.id,
            'reference_id':reference,'subdomain':make_service_subdomain(self.workspace.ws_hash,reference,'AC1011 native CS'),
            'helm_namespace':make_namespace(self.workspace.ws_hash),'helm_release_name':'svc-'+uuid.uuid4().hex[:8],
            'state':'running','is_protected':True,'login_google':True,'login_email_otp':False,
        }).with_env(self.env)
        self.sh=sh_model(self.env).sudo().create({'name':'AC1011 native SH','workspace_id':self.workspace.id,'state':'pending'}).with_env(self.env)
        self.sh.subdomain=self.sh._generate_subdomain()
        self.route=_sm_skip_access_sync(self.env['woow_paas_platform.smart_home_route'].sudo()).create({
            'smart_home_id':self.sh.id,'is_primary':True,'is_protected':True,'login_google':True,'login_email_otp':False}).with_env(self.env)
    def sh_active(self,app=False):
        _sh_identity(self.sh).write({'state':'active','tunnel_id':'fake-native-tunnel','tunnel_name':'fake-native-sh'})
        if app:_internal_sm_route_identity(self.route).access_app_id='fake-old-app';self.client.app=True
        self.client.origin=True;self.client.events.clear();self.client.history.clear()
    def cs_active(self,app=False):
        if app:_internal_cs_access_identity(self.cs).access_app_id='fake-old-app';self.client.app=True
        self.client.origin=True;self.client.events.clear();self.client.history.clear()
    def assert_safe_history(self):
        self.assertTrue(all(not origin or app for origin,app in self.client.history),self.client.history)
    def names(self):return [event[0] for event in self.client.events]


@tagged('post_install','-at_install')
class TestCsShFailClosedAC1011(Fixture,TransactionCase):
    def setUp(self):
        super().setUp();self.install_fake();self.resources()
    def test_cs_enable_failure_rolls_back_db_and_keeps_origin_parked(self):
        _cs_skip_access_sync(self.cs).is_protected=False
        self.cs_active();self.client.fail_access=True
        with self.assertRaises(PaaSOperatorError):self.cs.write({'is_protected':True})
        self.cs.invalidate_recordset();self.assertFalse(self.cs.is_protected)
        self.assertFalse(self.client.origin);self.assertEqual(self.names()[:2],['park-release','access'])
    def test_cs_existing_app_failed_login_edit_preserves_db_and_app(self):
        self.cs_active(app=True);self.client.fail_access=True
        old=self.cs.session_duration
        # A longer session only widens access: no revocation, so no park before the call
        # (a narrowing edit parks first: test_access_authorization_change_revoke).
        with self.assertRaises(PaaSOperatorError):self.cs.write({'session_duration':'168h'})
        self.cs.invalidate_recordset();self.assertEqual(self.cs.session_duration,old)
        self.assertEqual(self.cs.access_app_id,'fake-old-app');self.assertTrue(self.client.app)
        self.assertEqual(self.names(),['access'])
    def test_cs_missing_app_direct_repair_error_keeps_origin_parked(self):
        self.cs_active();self.cs.access_protection_lost=True;self.client.fail_access=True
        with self.assertRaises(PaaSOperatorError):self.cs._sync_access_to_operator()
        self.assertFalse(self.client.origin);self.assertTrue(self.cs.access_protection_lost)
    def test_cs_malformed_app_result_does_not_unpark(self):
        self.cs_active();self.client.malformed=True
        with self.assertRaisesRegex(UserError,'not established'):self.cs._sync_access_to_operator()
        self.assertFalse(self.client.origin);self.assertFalse(self.cs.access_app_id)
    def test_cs_protected_rename_rejects_before_mutation(self):
        self.cs_active(app=True);old=self.cs.subdomain
        with self.assertRaisesRegex(UserError,'hostnames cannot be changed'):self.cs.write({'subdomain':'ac1011-new'})
        self.assertEqual(self.cs.subdomain,old);self.assertEqual(self.names(),[])
    def test_cs_resume_preflight_failure_does_not_scale_or_unpark(self):
        self.cs.write({'state':'suspended','pre_suspend_state':'running'})
        self.client.origin=False;self.client.fail_access=True
        self.cs._do_resume()
        self.assertEqual(self.cs.state,'error');self.assertEqual(self.cs.pre_suspend_state,'running')
        self.assertFalse(self.client.origin);self.assertNotIn('scale',self.names());self.assertNotIn('unpark',self.names())
    def test_cs_successful_resume_access_precedes_scale_and_unpark(self):
        self.cs.write({'state':'suspended','pre_suspend_state':'running'});self.client.origin=False
        self.cs._do_resume()
        self.assertEqual(self.cs.state,'running');self.assertFalse(self.cs.pre_suspend_state)
        self.assertIn('access',self.names());self.assertLess(self.names().index('access'),self.names().index('scale'));self.assert_safe_history()
    def test_cs_protected_deploy_failure_does_not_install(self):
        self.cs.write({'state':'pending'});self.client.origin=False;self.client.fail_access=True
        self.cs._execute_deploy()
        self.assertEqual(self.cs.state,'error');self.assertNotIn('install',self.names());self.assertFalse(self.client.origin)
    def test_cs_protected_deploy_success_access_precedes_exposure(self):
        self.cs.write({'state':'pending'});self.client.origin=False
        self.cs._execute_deploy()
        self.assertIn('install',self.names());self.assertIn('access',self.names());self.assertLess(self.names().index('access'),self.names().index('install'))
        self.assert_safe_history()
    def test_cs_cleanup_uninstall_failure_preserves_app(self):
        self.cs_active(app=True);self.client.fail_uninstall=True
        self.cs._do_cleanup()
        self.assertEqual(self.cs.state,'error');self.assertTrue(self.client.app);self.assertNotIn('access',self.names())
    def test_cs_cleanup_uninstalls_before_access_teardown(self):
        self.cs_active(app=True);self.cs._do_cleanup()
        self.assertEqual(self.cs.state,'deleting');self.assertFalse(self.client.origin);self.assertFalse(self.client.app)
        self.assertLess(self.names().index('uninstall'),self.names().index('access'))
    def test_cs_park_noop_is_rejected_before_access(self):
        self.cs_active();self.client.park_status='disabled'
        with self.assertRaisesRegex(UserError,'could not be blocked'):self.cs._sync_access_to_operator()
        self.assertNotIn('access',self.names())
    def test_sh_retry_failure_never_publishes_origin(self):
        self.sh_active();self.sh.state='error';self.client.origin=False;self.client.fail_access=True
        self.sh._converge_existing_tunnel()
        self.assertEqual(self.sh.state,'error');self.assertFalse(self.client.origin);self.assert_safe_history()
    def test_sh_resume_failure_keeps_503_and_retry_marker(self):
        self.sh_active();self.sh.write({'state':'suspended','pre_suspend_state':'active'});self.client.origin=False;self.client.fail_access=True
        try:self.sh._do_resume()
        except PaaSOperatorError:pass
        self.assertEqual(self.sh.state,'error');self.assertEqual(self.sh.pre_suspend_state,'active')
        self.assertFalse(self.client.origin);self.assert_safe_history()
    def test_sh_fresh_protected_provision_failure_retains_blocked_tunnel(self):
        self.client.origin=False;self.client.fail_access=True
        self.sh.action_provision()
        self.assertEqual(self.sh.state,'error');self.assertTrue(self.sh.tunnel_id)
        self.assertFalse(self.client.origin);self.assert_safe_history()
    def test_sh_direct_missing_app_repair_error_parks_before_access(self):
        self.sh_active();self.route.access_protection_lost=True;self.client.fail_access=True
        with self.assertRaises(PaaSOperatorError):self.route._sync_access_to_operator()
        self.assertFalse(self.client.origin);self.assertEqual(self.names()[:2],['configure','access'])
    def test_sh_existing_app_failed_edit_preserves_db_and_app(self):
        self.sh_active(app=True);self.client.fail_access=True;old=self.route.session_duration
        # A longer session only widens access: no revocation, so no park before the call.
        with self.assertRaises(PaaSOperatorError):self.route.write({'session_duration':'168h'})
        self.route.invalidate_recordset();self.assertEqual(self.route.session_duration,old);self.assertEqual(self.route.access_app_id,'fake-old-app')
        self.assertTrue(self.client.app);self.assertEqual(self.names(),['access'])
    def test_sh_malformed_app_result_does_not_release(self):
        self.sh_active();self.client.malformed=True
        with self.assertRaisesRegex(UserError,'not established'):self.route._sync_access_to_operator()
        self.assertFalse(self.client.origin);self.assertFalse(self.route.access_app_id)
    def test_sh_protected_rename_rejects_before_mutation(self):
        self.sh_active(app=True);old=self.sh.subdomain
        with self.assertRaisesRegex(UserError,'hostnames cannot be changed'):self.sh.write({'subdomain':'ac1011-new'})
        self.assertEqual(self.sh.subdomain,old);self.assertEqual(self.names(),[])
    def test_sh_teardown_parks_before_access_removal(self):
        self.sh_active(app=True);self.assertEqual(self.sh._teardown_routes_access(),[])
        self.assertFalse(self.client.origin);self.assertFalse(self.client.app)
        self.assertLess(self.names().index('configure'),self.names().index('access'))
    def test_sh_teardown_park_failure_preserves_app(self):
        self.sh_active(app=True);self.client.fail_park=True
        with self.assertRaises(PaaSOperatorError):self.sh._teardown_routes_access()
        self.assertTrue(self.client.app);self.assertEqual(self.route.access_app_id,'fake-old-app');self.assertNotIn('access',self.names())
    def test_sh_successful_resume_access_before_original_origin(self):
        self.sh_active();self.sh.write({'state':'suspended','pre_suspend_state':'active','original_service_url':'http://localhost:9999'});self.client.origin=False
        self.sh._do_resume()
        self.assertEqual(self.sh.state,'active');self.assertFalse(self.sh.pre_suspend_state)
        self.assertEqual(self.client.events[-1],('configure','http://localhost:9999'));self.assert_safe_history()
    def test_sh_suspended_direct_sync_does_not_restore_origin(self):
        self.sh_active();self.sh.state='suspended';self.client.origin=False
        self.assertFalse(self.route._sync_access_to_operator());self.assertFalse(self.client.origin);self.assertEqual(self.names(),[])


@tagged('post_install','-at_install')
class TestCsShBoundaryHttpAC1011(Fixture,HttpCase):
    def setUp(self):
        super().setUp();self.install_fake()
        suffix=uuid.uuid4().hex[:8];self.login='ac1011_boundary_'+suffix
        self.user=self.env['res.users'].sudo().create({'name':'AC1011 native boundary owner','login':self.login,'password':self.login,'email':self.login+'@example.test','groups_id':[(6,0,[self.env.ref('base.group_user').id])]})
        self.resources(self.user)
        self.authenticate(self.login,self.login)
    def rpc(self,path,params):
        response=self.url_open(path,data=json.dumps({'jsonrpc':'2.0','method':'call','params':params}),headers={'Content-Type':'application/json'})
        body=response.json();self.assertNotIn('error',body);return body['result']
    def test_http_cs_enable_error_rolls_back_and_keeps_origin_parked(self):
        _cs_skip_access_sync(self.cs).is_protected=False;self.client.origin=True;self.client.fail_access=True
        result=self.rpc(f'/api/workspaces/{self.workspace.id}/services/{self.cs.id}/access-config',{'action':'update','is_protected':True})
        self.assertFalse(result['success']);self.cs.invalidate_recordset();self.assertFalse(self.cs.is_protected);self.assertFalse(self.client.origin)
    def test_http_cs_existing_app_failure_preserves_old_protection(self):
        self.cs_active(app=True);self.client.fail_access=True
        result=self.rpc(f'/api/workspaces/{self.workspace.id}/services/{self.cs.id}/access-config',{'action':'update','session_duration':'8h'})
        self.assertFalse(result['success']);self.cs.invalidate_recordset();self.assertEqual(self.cs.access_app_id,'fake-old-app');self.assertTrue(self.client.app)
    def test_http_sh_retry_error_reports_false_and_stays_blocked(self):
        self.sh_active();self.sh.state='error';self.client.origin=False;self.client.fail_access=True
        result=self.rpc(f'/api/workspaces/{self.workspace.id}/smarthomes/{self.sh.id}',{'action':'provision'})
        self.assertFalse(result['success']);self.assertEqual(result['data']['state'],'error');self.assertFalse(self.client.origin);self.assert_safe_history()




@tagged('post_install','-at_install')
class TestShMalformedProvisionRecoveryAC1011(Fixture,TransactionCase):
    def setUp(self):
        super().setUp();self.install_fake();self.resources()
    def test_malformed_new_provision_records_error_then_retries_same_tunnel(self):
        self.client.malformed=True
        try:self.sh.action_provision()
        except UserError:pass
        self.assertEqual(self.sh.state,'error')
        self.assertEqual(self.sh.tunnel_id,'fake-native-tunnel')
        self.assertFalse(self.client.origin)
        self.client.malformed=False;self.sh.action_provision()
        self.assertEqual(self.sh.state,'active')
        self.assertEqual(sum(event[0]=='create-tunnel' for event in self.client.events),1)
        self.assert_safe_history()

from .publication_receipt_fixture import configure_receipt, routes_receipt

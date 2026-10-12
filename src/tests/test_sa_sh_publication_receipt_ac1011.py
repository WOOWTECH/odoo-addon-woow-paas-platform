"""Exact native caller receipt checks; all resources/operator transport synthetic."""
from copy import deepcopy
from unittest.mock import patch
from odoo.tests.common import TransactionCase, tagged
from ..services.access_state import access_configuration_ready
from .test_access_publish_pending_ac1011 import PendingFixture, SA_CLIENT
from ..models.security_access import _internal_identity as sa_identity
from ..models.smart_home import _internal_identity as sh_identity

@tagged('post_install', '-at_install')
class TestSaShPublicationReceiptAc1011(PendingFixture, TransactionCase):
    def setUp(self):
        super().setUp()
        self.install_fake()
        self.resources()
        p=patch(SA_CLIENT, return_value=self.client);p.start();self.addCleanup(p.stop)

    def _receipt(self, kind, args, kwargs):
        if kind == 'sh':
            return {'message': 'configured', 'hostname': kwargs['hostname'],
                    'service_url': kwargs['service_url']}
        return {'tunnel_id': args[0], 'routes': [
            {'subdomain': r['subdomain'], 'hostname': r['subdomain']+'.example.test',
             'dns_record_id': None} for r in kwargs['routes']]}

    def _late_matrix(self, kind, public=False):
        anchor=self.anchor(kind)
        method='configure_tunnel' if kind=='sh' else 'sync_tunnel_routes'
        original=getattr(self.client,method)
        def successful(*args,**kwargs):
            original(*args,**kwargs)
            return self._receipt(kind,args,kwargs)
        setattr(self.client,method,successful)
        if public:
            anchor.write({'is_protected': True})
        bad = ({}, {'status':'disabled'}, [], None)
        if kind=='sh':
            bad += ({'hostname':'wrong.example.test','service_url':'http://localhost:8123'},
                    {'hostname':self.sh.subdomain+'.example.test','service_url':'http://wrong-origin:80'},
                    {'hostname':True,'service_url':'http://localhost:8123'},
                    {'hostname':self.sh.subdomain+'.example.test','service_url':8123})
        else:
            bad += ({'tunnel_id':'wrong-tunnel','routes':[]},
                    {'tunnel_id':anchor.security_access_id.tunnel_id,'routes':[]},
                    {'tunnel_id':anchor.security_access_id.tunnel_id,'routes':[
                        {'subdomain':anchor.subdomain_prefix,'hostname':'wrong.example.test'}]},
                    {'tunnel_id':anchor.security_access_id.tunnel_id,'routes':{}},
                    {'tunnel_id':anchor.security_access_id.tunnel_id,'routes':[
                        {'subdomain':anchor.subdomain_prefix,'hostname':anchor.subdomain_prefix+'.example.test'}]*2},
                    {'tunnel_id':anchor.security_access_id.tunnel_id,'routes':[
                        {'subdomain':'unknown','hostname':'unknown.example.test'}]},
                    {'tunnel_id':anchor.security_access_id.tunnel_id,'routes':[
                        {'subdomain':anchor.subdomain_prefix,'hostname':anchor.subdomain_prefix+'.example.test','dns_record_id':True}]})
        valid_body = (self._receipt('sh', (), {
            'hostname': self.sh.subdomain + '.example.test',
            'service_url': 'http://localhost:8123'}) if kind == 'sh' else
            {'tunnel_id': anchor.security_access_id.tunnel_id, 'routes': [{
                'subdomain': anchor.subdomain_prefix,
                'hostname': anchor.subdomain_prefix + '.example.test',
                'dns_record_id': None}]})
        bad += tuple(dict(valid_body, **metadata) for metadata in (
            {'success': False}, {'error': 'publication rejected'},
            {'status': 'error'}, {'status': 'disabled'}))
        if kind == 'sh':
            missing_message = dict(valid_body)
            missing_message.pop('message')
            bad += (missing_message, dict(valid_body, message=False))
        for body in bad:
            with self.subTest(kind=kind, public=public, response=body):
                setattr(self.client,method,successful)
                anchor.write({'is_protected': False})
                if public:
                    self.client.fail_publish = True
                    anchor.write({'is_protected': True})
                    self.assertTrue(anchor.access_publish_pending)
                    self.client.fail_publish = False
                consumed=[]
                def malformed(*args,**kwargs):
                    live=(not kwargs['service_url'].startswith('http_status:') if kind=='sh'
                          else any(not r['service_url'].startswith('http_status:') for r in kwargs['routes']))
                    result=successful(*args,**kwargs)
                    if live:
                        consumed.append(True)
                        return deepcopy(body)
                    return result
                setattr(self.client,method,malformed)
                self.assertTrue(anchor.write({'is_protected':not public}))
                anchor.invalidate_recordset()
                self.assertTrue(consumed,'late publication path was not reached')
                self.assertTrue(anchor.access_publish_pending)
                self.assertFalse(access_configuration_ready(anchor))
                self.assertEqual(bool(anchor.access_app_id),not public)
                setattr(self.client,method,successful)
                self.assertTrue(anchor.write({'is_protected':not public}))
                self.assertFalse(anchor.access_publish_pending)
                self.assertTrue(access_configuration_ready(anchor))

    def test_sa_invalid2xx_preserves_anchors_until_exact_receipt(self):
        self._late_matrix('sa')
    def test_sh_invalid2xx_preserves_anchors_until_exact_receipt(self):
        self._late_matrix('sh')
    def test_sa_public_pending_requires_exact_receipt_after_teardown(self):
        self._late_matrix('sa',public=True)
    def test_sh_public_pending_requires_exact_receipt_after_teardown(self):
        self._late_matrix('sh',public=True)
    def test_sa_no_tunnel_remains_no_mutation(self):
        sa,route=self.sa_resource();sa_identity(sa).tunnel_id=False
        self.client.events.clear();sa._sync_routes_to_operator()
        self.assertEqual(self.client.events,[])
    def test_sh_no_tunnel_remains_no_mutation(self):
        sh_identity(self.sh).tunnel_id=False
        self.client.events.clear();self.sh._sync_access_ingress()
        self.assertEqual(self.client.events,[])

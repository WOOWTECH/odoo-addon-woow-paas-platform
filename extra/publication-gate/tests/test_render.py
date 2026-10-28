import importlib.util
import unittest


spec = importlib.util.spec_from_file_location('render', 'extra/publication-gate/deploy/render.py')
render_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render_module)


class RenderTest(unittest.TestCase):
    def config(self):
        return {
            'namespace': 'b38-rev01-live', 'workload': 'owned-ha',
            'owner_uid': 'owned-uid', 'image': 'registry.test/gate@sha256:' + 'a' * 64,
            'secret_name': 'rev01-gate-tls', 'pvc_name': 'rev01-gate-state',
            'resource_identity': 'cf-tunnel:v1:account:tunnel',
            'installation_id': 'origin-owned', 'canonical_name': 'sa48-owned',
            'authority_host': 'candidate-odoo.b38-rev01-live.svc',
            'authority_port': 18444, 'authority_server_sha256': 'b' * 64,
            'control_client_sha256': 'c' * 64,
            'issuer': 'https://owned.cloudflareaccess.com', 'audience': 'd' * 64,
        }

    def test_secret_name_only_sidecar_and_exact_ports(self):
        output = render_module.render(self.config())
        encoded = str(output)
        self.assertNotIn('privateKey', encoded)
        deployment = [item for item in output['items'] if item['kind'] == 'Deployment'][0]
        pod = deployment['spec']['template']['spec']
        self.assertNotIn('initContainers', pod)
        jobs = [item for item in output['items'] if item['kind'] == 'Job']
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['spec']['backoffLimit'], 0)
        gate = pod['containers'][0]
        self.assertEqual(gate['ports'][0]['containerPort'], 18303)
        self.assertEqual(pod['volumes'][2]['secret']['secretName'], 'rev01-gate-tls')
        configmap = [item for item in output['items'] if item['kind'] == 'ConfigMap'][0]
        self.assertIn('http://127.0.0.1:8123/manifest.json', configmap['data']['gate.json'])

    def test_unpinned_image_and_extra_key_rejected(self):
        config = self.config()
        config['image'] = 'registry.test/gate:latest'
        with self.assertRaisesRegex(ValueError, 'digest-pinned'):
            render_module.render(config)
        config = self.config()
        config['token'] = 'must-not-be-accepted'
        with self.assertRaisesRegex(ValueError, 'exact deployment'):
            render_module.render(config)


if __name__ == '__main__':
    unittest.main()

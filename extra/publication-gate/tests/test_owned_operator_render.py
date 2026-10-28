import importlib.util
from pathlib import Path
import unittest


PATH = Path(__file__).parents[1] / 'deploy' / 'render_owned_operator.py'
SPEC = importlib.util.spec_from_file_location('owned_operator_render', PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class OwnedOperatorRenderTest(unittest.TestCase):
    def config(self):
        return {'namespace': 'b38-fault02', 'name': 'fault02-operator',
                'owner_uid': 'owned-uid',
                'image': 'registry.example/operator@sha256:' + 'a' * 64,
                'credential_secret': 'fault02-operator-credentials',
                'api_key_name': 'api-key', 'account_id_name': 'account-id',
                'tunnel_token_name': 'tunnel-token'}

    def test_exact_isolated_secret_reference_only(self):
        rendered = MODULE.render(self.config())
        self.assertEqual([item['kind'] for item in rendered['items']],
                         ['Deployment', 'Service', 'NetworkPolicy'])
        encoded = str(rendered)
        self.assertIn('fault02-operator-credentials', encoded)
        self.assertNotIn('kind\': \'Secret', encoded)
        self.assertFalse(rendered['x-fault02']['provider_resources_created'])

    def test_rejects_mutable_image(self):
        value = self.config()
        value['image'] = 'registry.example/operator:latest'
        with self.assertRaises(ValueError):
            MODULE.render(value)


if __name__ == '__main__':
    unittest.main()

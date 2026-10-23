"""Offline contract checks for the default-off, read-only tenant endpoint."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


class TestDormantPriceNoticeRoute(unittest.TestCase):
    def test_authenticated_readonly_json_post_reads_publications_only(self):
        source = Path(__file__).resolve().parents[1] / 'src/controllers/llm.py'
        tree = ast.parse(source.read_text())
        handler = next(node for node in ast.walk(tree)
                       if isinstance(node, ast.FunctionDef) and node.name == 'llm_price_notices')
        route, = handler.decorator_list
        self.assertEqual(ast.literal_eval(route.args[0]), '/api/llm/price-notices')
        self.assertEqual({kw.arg: ast.literal_eval(kw.value) for kw in route.keywords},
                         dict(auth='user', methods=['POST'], type='json', readonly=True))

        class PublicModel:
            def __init__(self):
                self.result = [{'id': 'pc_' + 'a' * 32}]
                self.error = None

            def _llm_public_notices(self):
                if self.error:
                    raise self.error
                return self.result

        class PublicEnv:
            uid = 23

            def __init__(self, model):
                self.model = model
                self.accessed = []

            def __getitem__(self, key):
                self.accessed.append(key)
                if key != 'publications':
                    raise AssertionError(f'unexpected model access: {key}')
                return self.model

        class OperationalError(Exception):
            pass

        model = PublicModel()
        env = PublicEnv(model)
        logger = Mock()
        handler.decorator_list = []
        handler.returns = None
        module = ast.Module(body=[handler], type_ignores=[])
        namespace = {
            'request': SimpleNamespace(env=env),
            'PUBLICATION_MODEL': 'publications',
            'OperationalError': OperationalError,
            '_logger': logger,
        }
        exec(compile(ast.fix_missing_locations(module), str(source), 'exec'), namespace)
        route_handler = namespace['llm_price_notices']
        self.assertEqual(route_handler(object()), {'notices': model.result})
        self.assertEqual(env.accessed, ['publications'])

        model.error = ValueError('bad stored notice')
        self.assertEqual(route_handler(object()), {'notices': []})
        logger.error.assert_called_once()

        model.error = OperationalError('retry transaction')
        with self.assertRaises(OperationalError):
            route_handler(object())


if __name__ == '__main__':
    unittest.main()

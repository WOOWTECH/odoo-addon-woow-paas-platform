"""Offline contract checks for the deliberately inert tenant endpoint."""
import ast
from pathlib import Path
import unittest


class TestDormantPriceNoticeRoute(unittest.TestCase):
    def test_authenticated_readonly_json_post_without_model_access(self):
        source = Path(__file__).resolve().parents[1] / 'src/controllers/llm.py'
        tree = ast.parse(source.read_text())
        handler = next(node for node in ast.walk(tree)
                       if isinstance(node, ast.FunctionDef) and node.name == 'llm_price_notices')
        route, = handler.decorator_list
        self.assertEqual(ast.literal_eval(route.args[0]), '/api/llm/price-notices')
        self.assertEqual({kw.arg: ast.literal_eval(kw.value) for kw in route.keywords},
                         dict(auth='user', methods=['POST'], type='json', readonly=True))
        # Only a docstring and a literal return are allowed: even a draft read fails this gate.
        docstring, result = handler.body
        self.assertIsInstance(docstring, ast.Expr)
        self.assertIsInstance(result, ast.Return)
        self.assertEqual(ast.literal_eval(result.value), {'notices': []})
        handler.decorator_list = []
        handler.returns = None
        module = ast.Module(body=[handler], type_ignores=[])
        namespace = {}
        exec(compile(ast.fix_missing_locations(module), str(source), 'exec'), namespace)
        self.assertEqual(namespace['llm_price_notices'](object()), {'notices': []})


if __name__ == '__main__':
    unittest.main()

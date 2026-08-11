#!/usr/bin/env python3
"""``check_one_time_credential_render`` 的 A/B 注入測試（#672）。

這條 gate 守的是一次性憑證在**後端 → 前端**那一段的涵蓋：後端
``PaasController._gateway_credentials`` 交付的每個 key，前端
``buildOneTimeCredentials`` 都要讀得到。少讀一個，那一格永遠不顯示——而一次性憑證
錯過就沒有第二次（平台刻意不做回讀端點）。

**先注入紅燈，再相信綠燈**：一條新 gate 第一個要證明的不是「現在是綠的」，而是
「它會紅」。`TestBaselineIsGreen.test_probe_actually_sees_the_real_payload` 是那個
陽性對照——不能只驗 `violations == []`，那個值在「探針從沒抽到任何 key」時一模一樣
（memory: feedback_negative_verification_needs_positive_control）。

跑法：
    python3 scripts/audit/test_repo_lint_one_time_credential_render.py
    # 或 python3 -m pytest scripts/audit/test_repo_lint_one_time_credential_render.py

契約（.gitea/workflows/repo-lint.yml 的「scripts/ 自我測試」步驟）：不得有外部
依賴——不碰 ~/.claude/、kubeconfig、helm、網路、Odoo，也不依賴 cwd。
"""
import importlib.util
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_GATE_PATH = pathlib.Path(__file__).with_name("repo-lint.py")

_spec = importlib.util.spec_from_file_location("repo_lint_one_time_cred_ut", _GATE_PATH)
rl = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rl
_spec.loader.exec_module(rl)


BACKEND_OK = '''
class PaasController(object):
    @staticmethod
    def _gateway_credentials(service):
        creds = {}
        secret = service._get_secret_record()
        gateway_url = service._gateway_public_url()
        if secret and secret.gateway_api_key and gateway_url:
            creds['gateway_api_key'] = secret.gateway_api_key
            creds['gateway_url'] = gateway_url
        dashboard_url = service._dashboard_public_url()
        if dashboard_url:
            creds['dashboard_url'] = dashboard_url
        return creds
'''

# #672 之前的形狀：整包 dict literal 一次 return。抽 key 的邏輯必須兩種都認得，
# 否則有人把實作改回 dict literal 時，gate 會在「抽不到 key」的分支紅在錯的理由上
# （或更糟：若當初寫成抽不到就回 []，會靜默恆綠）。
BACKEND_DICT_RETURN = '''
class PaasController(object):
    @staticmethod
    def _gateway_credentials(service):
        return {
            'gateway_api_key': 'x',
            'gateway_url': 'y',
            'dashboard_url': 'z',
        }
'''

FRONTEND_OK = '''
export function buildOneTimeCredentials(data) {
    const credentials = [];
    if (data && data.admin_password) {
        credentials.push({ label: "Admin Password", value: data.admin_password });
    }
    if (data && data.gateway_api_key) {
        credentials.push({ label: "Gateway API Key", value: data.gateway_api_key });
    }
    if (data && data.gateway_url) {
        credentials.push({ label: "Gateway API URL", value: data.gateway_url });
    }
    if (data && data.dashboard_url) {
        credentials.push({ label: "Dashboard API URL", value: data.dashboard_url });
    }
    return credentials;
}
'''

# 少讀 dashboard_url ＝ #672 修好之後最容易復發的形態（後端補了一項、前端沒跟上）。
FRONTEND_MISSING_DASHBOARD = FRONTEND_OK.replace(
    '''    if (data && data.dashboard_url) {
        credentials.push({ label: "Dashboard API URL", value: data.dashboard_url });
    }
''', '')


class _FixtureRoot:
    """把後端 .py 與前端 .js 擺進一個最小的 repo root。"""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="one-time-cred-gate-")

    def write(self, rel, text):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return self

    def run(self):
        return rl.check_one_time_credential_render(self.dir)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.dir, ignore_errors=True)
        return False


def _pair(backend=BACKEND_OK, frontend=FRONTEND_OK):
    with _FixtureRoot() as fx:
        fx.write(rl.ONE_TIME_CRED_BACKEND_FILE, backend)
        fx.write(rl.ONE_TIME_CRED_MODAL_FILE, frontend)
        return fx.run()


# ------------------------------------------------------------ 陽性對照

class TestBaselineIsGreen(unittest.TestCase):
    """未突變時必須綠——而且綠得有前提（探針真的抽到了東西）。"""

    def test_real_repo_is_green(self):
        self.assertEqual(rl.check_one_time_credential_render(str(_REPO_ROOT)), [])

    def test_probe_actually_sees_the_real_payload(self):
        """真 repo 的後端 payload 至少要抽得出 dashboard_url。

        「violations == []」在探針壞掉（函式改名、AST 形態改變、regex 抓不到 body）
        時是同一個值。這條直接盯真檔案抽出來的集合，讓「綠」與「沒掃到」分得開。
        """
        import ast
        with open(os.path.join(str(_REPO_ROOT),
                               rl.ONE_TIME_CRED_BACKEND_FILE), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        func = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef)
                    and n.name == rl.ONE_TIME_CRED_BACKEND_FUNC)
        keys = rl._one_time_cred_backend_keys(func)
        self.assertIn('dashboard_url', keys,
                      '探針沒從真檔案抽到 dashboard_url —— 這條 gate 可能對任何內容'
                      '都回 []（#672 修的就是這一格）')
        self.assertIn('gateway_api_key', keys)

    def test_minimal_clean_fixture_passes(self):
        self.assertEqual(_pair(), [])


# ------------------------------------------------------------ 注入紅燈（主案）

class TestMissingKeyIsBlocked(unittest.TestCase):
    """後端交付、前端沒讀 ⇒ 必須紅，而且要點名是哪一個 key。"""

    def test_frontend_missing_a_delivered_key(self):
        violations = _pair(frontend=FRONTEND_MISSING_DASHBOARD)
        self.assertTrue(violations, '前端漏讀 dashboard_url 卻放行了')
        self.assertIn('dashboard_url', violations[0])
        self.assertIn('#672', violations[0])

    def test_message_names_which_key_not_how_many(self):
        """訊息要說「是哪一個」——「差幾個」是沒法行動的訊號。"""
        frontend = FRONTEND_MISSING_DASHBOARD.replace(
            '''    if (data && data.gateway_url) {
        credentials.push({ label: "Gateway API URL", value: data.gateway_url });
    }
''', '')
        violations = _pair(frontend=frontend)
        self.assertTrue(violations)
        self.assertIn('dashboard_url', violations[0])
        self.assertIn('gateway_url', violations[0])

    def test_dict_literal_return_form_is_also_covered(self):
        """實作改回「一次 return dict literal」時，涵蓋判準不得失效。"""
        self.assertEqual(_pair(backend=BACKEND_DICT_RETURN), [])
        violations = _pair(backend=BACKEND_DICT_RETURN,
                           frontend=FRONTEND_MISSING_DASHBOARD)
        self.assertTrue(violations, 'dict literal 形態下 gate 失去鑑別力')
        self.assertIn('dashboard_url', violations[0])


# ------------------------------------------------------------ 覆蓋方向（不是相等）

class TestCoverageDirection(unittest.TestCase):
    """判準是後端 ⊆ 前端，不是相等。"""

    def test_frontend_reading_extra_keys_is_harmless(self):
        # 前端多讀一個後端不再送的 key ⇒ 那段 if 永遠不成立，無害，不得誤報。
        frontend = FRONTEND_OK.replace(
            '    return credentials;',
            '    if (data && data.legacy_token) {\n'
            '        credentials.push({ label: "Legacy", value: data.legacy_token });\n'
            '    }\n    return credentials;')
        self.assertEqual(_pair(frontend=frontend), [])


# ------------------------------------------------------------ fail-loud / fail-open

class TestProbeIntegrity(unittest.TestCase):
    """探針壞掉必須紅；輸入不存在才可以沉默。"""

    def test_backend_function_renamed_is_blocked(self):
        backend = BACKEND_OK.replace('_gateway_credentials', '_mcp_credentials')
        violations = _pair(backend=backend)
        self.assertTrue(violations, '後端函式改名後 gate 靜默＝從此永久失效')
        self.assertIn(rl.ONE_TIME_CRED_BACKEND_FUNC, violations[0])

    def test_backend_with_no_string_keys_is_blocked(self):
        """抽不到任何 key ⇒ 「後端 ⊆ 前端」在空集合上恆真，必須當違規。"""
        backend = '''
class PaasController(object):
    @staticmethod
    def _gateway_credentials(service):
        return {}
'''
        violations = _pair(backend=backend)
        self.assertTrue(violations)
        self.assertIn('鑑別力', violations[0])

    def test_builder_function_removed_is_blocked(self):
        frontend = FRONTEND_OK.replace('export function buildOneTimeCredentials',
                                       'function somethingElse')
        violations = _pair(frontend=frontend)
        self.assertTrue(violations, 'builder 被拆掉後 gate 靜默＝憑證清單散回各頁各寫一份')
        self.assertIn(rl.ONE_TIME_CRED_BUILDER, violations[0])

    def test_unparseable_backend_is_blocked_not_silently_allowed(self):
        violations = _pair(backend='def broken(:\n')
        self.assertTrue(violations)
        self.assertIn(rl.ONE_TIME_CRED_BACKEND_FILE, violations[0])

    def test_missing_files_are_fail_open(self):
        # 檔案搬家／不存在由其他 gate 與測試負責，這裡不重複報（同 availability）。
        with _FixtureRoot() as fx:
            self.assertEqual(fx.run(), [])
            fx.write(rl.ONE_TIME_CRED_BACKEND_FILE, BACKEND_OK)
            self.assertEqual(fx.run(), [], '只有後端在時不得誤報')


# ------------------------------------------------------------ 登記完整性

class TestRegistryWiring(unittest.TestCase):
    """新 check 必須四處都掛上，否則就是 #596 的形態（CI 會抓、hook 完全沉默）。"""

    def test_rule_is_registered_and_dispatched(self):
        self.assertIn('one-time-credential-render', rl.REPO_LINT_RULES)
        self.assertEqual(rl.REPO_LINT_RULES['one-time-credential-render'],
                         ('check_one_time_credential_render',))
        # 檔頭 ↔ REPO_LINT_RULES ↔ run_all ↔ run_hook 的四處對帳由第 17 條自己驗；
        # 這裡只確認它對本 repo 現況是綠的（漏掛任一處這行就會紅）。
        self.assertEqual(
            rl.check_repo_lint_docstring_registry(str(_REPO_ROOT)), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

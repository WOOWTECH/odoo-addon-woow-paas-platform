# -*- coding: utf-8 -*-
"""QA CS-02／CS-03：code-server 的 git 作者身分與 Git Token 適用主機開放給租戶設定。

chart（woow-paas-charts charts/code-server）早就支援 ``git.userName``、``git.userEmail``、
``git.tokenHosts``（list，預設 ``[https://github.com]``），而且把三者**原樣**寫進
/etc/gitconfig。平台這一側：

1. seed：code-server template 的 helm_value_specs 多三個 ``type: text`` 選填欄位，
   帶 ``format``（git_name／email／git_hosts）；tokenHosts 預設 github.com。
2. 存檔（create／reconfigure）：``_validate_spec_formats`` 擋下會弄壞 gitconfig 的值。
3. 部署組裝：``_apply_spec_formats`` 把租戶的逗號清單轉成 chart 要的 https URL list；
   helm_values 保留原文；不合法的存值丟掉、chart 預設補位。
"""
import json
import logging
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from ..models.cloud_app_template import (
    SPEC_FORMAT_EMAIL,
    SPEC_FORMAT_GIT_HOSTS,
    SPEC_FORMAT_GIT_NAME,
    parse_email,
    parse_git_hosts,
    parse_git_name,
)
from ..models.cloud_service import _internal_provision

_CONTROLLER = 'odoo.addons.woow_paas_platform.controllers.paas'
_TEMPLATE_LOGGER = 'odoo.addons.woow_paas_platform.models.cloud_app_template'
GIT_KEYS = {
    'git.tokenHosts': SPEC_FORMAT_GIT_HOSTS,
    'git.userName': SPEC_FORMAT_GIT_NAME,
    'git.userEmail': SPEC_FORMAT_EMAIL,
}


@tagged('post_install', '-at_install')
class TestGitSpecParsers(TransactionCase):
    """純函式：合法值的正規化與每一種會弄壞 gitconfig 的輸入。"""

    def test_hosts_normalised_to_https_urls(self):
        cases = {
            'github.com': ['https://github.com'],
            'github.com, git.example.com:3000': [
                'https://github.com', 'https://git.example.com:3000'],
            ' GitHub.com ,https://Git-Prod.WoowTech.io/ ,, ': [
                'https://github.com', 'https://git-prod.woowtech.io'],
            # 全形逗號也是分隔符（中文輸入法常打出來）；重複的去掉
            'github.com，gitlab.com,github.com': ['https://github.com', 'https://gitlab.com'],
            '10.0.0.5:3000': ['https://10.0.0.5:3000'],
        }
        for raw, expected in cases.items():
            self.assertEqual(parse_git_hosts(raw), (expected, None), repr(raw))
        # 管理員直接把 list 寫進 helm_values 也收
        self.assertEqual(parse_git_hosts(['github.com', 'https://gitlab.com']),
                         (['https://github.com', 'https://gitlab.com'], None))

    def test_hosts_blank_means_chart_default(self):
        for raw in (None, '', '  ,  ', []):
            self.assertEqual(parse_git_hosts(raw), (None, None), repr(raw))

    def test_hosts_rejected(self):
        cases = {
            'http://github.com': 'hosts_not_https',
            'ssh://git@github.com': 'hosts_not_https',
            'github.com/org/repo': 'hosts_invalid',
            'user:pass@github.com': 'hosts_invalid',
            'github.com:0': 'hosts_invalid',
            'github.com:70000': 'hosts_invalid',
            '-bad.example.com': 'hosts_invalid',
            'gith"ub.com': 'hosts_invalid',
            'a_b.example.com': 'hosts_invalid',
            'git\u202e.example.com': 'hosts_invalid',
            # 空白／換行不是分隔符：打錯的「git example.com」不能變成兩台主機
            # （https://git 會經 pod 的 DNS search path 解析到 namespace 裡的東西）
            'git example.com': 'hosts_whitespace',
            'github.com gitlab.com': 'hosts_whitespace',
            'github.com github.com\ngitlab.com': 'hosts_whitespace',
            'github.com]\n[core]': 'hosts_whitespace',
            'github.com\tgitlab.com': 'hosts_whitespace',
            'a\u2028b.example.com': 'hosts_whitespace',
            'github.com, git lab.com': 'hosts_whitespace',
            # 單一 label：Git Token 不交給 DNS search path 上的同名東西
            'gitea': 'hosts_single_label',
            'git:3000': 'hosts_single_label',
            'https://localhost/': 'hosts_single_label',
            'github.com, gitea': 'hosts_single_label',
            ','.join('h%d.example.com' % i for i in range(11)): 'hosts_too_many',
        }
        for raw, code in cases.items():
            value, error = parse_git_hosts(raw)
            self.assertIsNone(value, repr(raw))
            self.assertEqual(error and error[0], code, repr(raw))
        self.assertEqual(parse_git_hosts(42)[1][0], 'not_text')
        self.assertEqual(parse_git_hosts(['github.com', 3])[1][0], 'not_text')
        # 管理員直接寫 list 也走同一套規則
        self.assertEqual(parse_git_hosts(['github.com', 'gitea'])[1][0], 'hosts_single_label')
        self.assertEqual(parse_git_hosts(['git example.com'])[1][0], 'hosts_whitespace')

    def test_ten_hosts_is_the_limit(self):
        raw = ','.join('h%d.example.com' % i for i in range(10))
        value, error = parse_git_hosts(raw)
        self.assertIsNone(error)
        self.assertEqual(len(value), 10)

    def test_name(self):
        self.assertEqual(parse_git_name('  王小明 '), ('王小明', None))
        self.assertEqual(parse_git_name('Mary-Jane O\'Neil'), ("Mary-Jane O'Neil", None))
        for blank in (None, '', '   '):
            self.assertEqual(parse_git_name(blank), (None, None))
        # 非 ASCII 的一般空白（全形空白）照收：它是可見的空白，YAML 也不當換行
        self.assertEqual(parse_git_name('王\u3000小明'), ('王\u3000小明', None))
        for bad in ('a\nb', 'a"b', 'a\\b', 'a;b', 'a#b', '<a>', 'x' * 101, '\x00'):
            self.assertEqual(parse_git_name(bad)[1][0], 'name_invalid', repr(bad))
        # YAML 1.1（goyaml.v2／PyYAML）把 NEL、U+2028、U+2029 當換行：chart 的
        # ``name = {{ . }}`` 會長出新的 gitconfig section（round-3 review 實測）
        for bad in ('x\u0085[core]', 'a\u0085b', 'a\u2028b', 'a\u2029b',
                    'x\u2028    [core]\u2028    pager = evil',
                    # 其他看不見的字元：C1 控制、bidi 控制、零寬字元、BOM、私用區、未指派
                    'a\x9bb', 'a\u202eb', 'a\u202ab', 'a\u2066b', 'a\u2069b',
                    'a\u200eb', 'a\u200fb', 'a\u200bb', 'a\ufeffb', 'a\ue000b',
                    'a\uffffb'):
            self.assertEqual(parse_git_name(bad)[1][0], 'name_invalid', repr(bad))
        self.assertEqual(parse_git_name(1)[1][0], 'not_text')

    def test_email(self):
        for good in ('you@example.com', 'a.b+tag@sub.example.co',
                     '12345+user@users.noreply.github.com'):
            self.assertEqual(parse_email(' %s ' % good), (good, None), good)
        for blank in (None, '', ' '):
            self.assertEqual(parse_email(blank), (None, None))
        for bad in ('no-at.example.com', 'a@b', 'a@@b.com', 'a b@c.com', 'a@b.com\n[core]',
                    'a@b.com\u0085[core]', 'a\u2028b@c.com', 'a\u202e@c.com',
                    'a"b@c.com', 'a;b@c.com', 'a@-b.com', 'x' * 250 + '@e.com'):
            self.assertEqual(parse_email(bad)[1][0], 'email_invalid', repr(bad))


@tagged('post_install', '-at_install')
class TestCodeServerGitSpecsSeed(TransactionCase):
    """seed 與 chart 的對照。"""

    def _tpl(self):
        return self.env.ref('woow_paas_platform.cloud_app_code_server')

    def test_git_specs_are_optional_text_with_formats(self):
        specs = self._tpl()._parse_helm_value_specs()
        optional = {s['key']: s for s in specs['optional']}
        for key, fmt in GIT_KEYS.items():
            self.assertIn(key, optional)
            self.assertEqual(optional[key]['type'], 'text', key)
            self.assertEqual(optional[key]['format'], fmt, key)
            self.assertTrue(optional[key]['label'], key)
            self.assertTrue(optional[key]['description'], key)
        self.assertEqual(optional['git.tokenHosts']['default'], 'github.com')
        self.assertEqual(self._tpl()._formatted_value_specs(), GIT_KEYS)
        for spec in specs['required']:
            self.assertNotIn(spec['key'], GIT_KEYS)

    def test_token_host_default_matches_chart_default(self):
        """spec 預設（github.com）正規化後＝chart values.yaml 的 [https://github.com]：
        租戶沒改過就存檔，gitconfig 不變、pod 不會因此重啟。"""
        specs = {s['key']: s for s in self._tpl()._parse_helm_value_specs()['optional']}
        self.assertEqual(parse_git_hosts(specs['git.tokenHosts']['default']),
                         (['https://github.com'], None))

    def test_labels_are_traditional_chinese(self):
        specs = {s['key']: s for s in self._tpl()._parse_helm_value_specs()['optional']}
        self.assertEqual(specs['git.tokenHosts']['label'], 'Git Token 適用主機')
        self.assertEqual(specs['git.userName']['label'], 'Git 作者名稱')
        self.assertEqual(specs['git.userEmail']['label'], 'Git 作者 Email')

    def test_git_settings_are_not_masked_as_credentials(self):
        """不是憑證：服務頁要看得到原文（遮罩只看 type: password）。"""
        git = {'tokenHosts': 'github.com', 'userName': '王小明',
               'userEmail': 'xm@example.com'}
        out = PaasController._sanitize_helm_values(
            {'git': dict(git), 'auth': {'gitToken': 'ghp_must_be_masked_0123'}}, self._tpl())
        self.assertEqual(out['git'], git)
        self.assertNotEqual(out['auth']['gitToken'], 'ghp_must_be_masked_0123',
                            '對照：真正的憑證仍然遮罩')


@tagged('post_install', '-at_install')
class TestCodeServerGitSettingsDeploy(TransactionCase):
    """存檔驗證與部署組裝。"""

    def setUp(self):
        super().setUp()
        self.tpl = self.env.ref('woow_paas_platform.cloud_app_code_server')
        self.ws = self.env['woow_paas_platform.workspace'].create({'name': 'git settings WS'})
        self.svc = _internal_provision(self.env['woow_paas_platform.cloud_service'].with_context(
            mcp_admin_password='old-password-0000',
        )).create({
            'name': 'svc-gitset01', 'workspace_id': self.ws.id,
            'template_id': self.tpl.id, 'state': 'running',
            'helm_release_name': 'svc-gitset01', 'helm_namespace': 'paas-ws-gitset01',
            'helm_values': json.dumps({'git': {
                'tokenHosts': 'github.com, Git.Example.com:3000/',
                'userName': ' 王小明 ',
                'userEmail': 'xm@example.com',
            }}),
        })

    def _git(self, svc=None):
        return (svc or self.svc)._assemble_deploy_values()['values'].get('git')

    def test_assembled_values_have_the_chart_shape(self):
        stored = self.svc._helm_values_dict()
        self.assertEqual(self._git(), {
            'tokenHosts': ['https://github.com', 'https://git.example.com:3000'],
            'userName': '王小明',
            'userEmail': 'xm@example.com',
        })
        self.assertEqual(self.svc._helm_values_dict(), stored,
                         'helm_values 保留租戶原文（設定頁顯示與再存檔不變形）')

    def test_blank_values_fall_back_to_chart_defaults(self):
        self.svc._apply_helm_values({'git': {
            'tokenHosts': '', 'userName': '', 'userEmail': '  '}})
        self.assertEqual(self._git(), {})

    def test_unicode_line_separator_in_stored_name_is_dropped(self):
        """NEL／U+2028 在 YAML 裡是換行：存值帶它也不能進 deploy values。"""
        self.svc._apply_helm_values({'git': {
            'tokenHosts': 'git example.com',
            'userName': 'x\u0085    [core]\u0085    pager = evil',
            'userEmail': 'ok@example.com',
        }})
        with self.assertLogs(_TEMPLATE_LOGGER, level=logging.WARNING):
            git = self._git()
        self.assertEqual(git, {'userEmail': 'ok@example.com'})

    def test_invalid_stored_values_are_dropped_with_a_warning(self):
        """繞過存檔驗證（例如管理員直接改 helm_values）也不能把 gitconfig 寫壞。"""
        self.svc._apply_helm_values({'git': {
            'tokenHosts': 'http://evil.example.com',
            'userName': 'x\n[core]\n\tsshCommand = touch /tmp/pwned',
            'userEmail': 'ok@example.com',
        }})
        with self.assertLogs(_TEMPLATE_LOGGER, level=logging.WARNING) as logs:
            git = self._git()
        self.assertEqual(git, {'userEmail': 'ok@example.com'})
        joined = '\n'.join(logs.output)
        self.assertIn('git.tokenHosts', joined)
        self.assertIn('git.userName', joined)
        self.assertNotIn('evil.example.com', joined, '存值不進 log')
        self.assertNotIn('sshCommand', joined)

    def test_other_templates_untouched(self):
        """沒有 format spec 的 template：同名鍵原樣送出（不是 code-server 的 chart）。"""
        other = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'no-format', 'slug': 'no-format-git', 'helm_repo_url': '',
            'helm_chart_name': 'oci://example/x', 'helm_chart_version': '1.0.0',
        })
        svc = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'name': 'svc-nofmt', 'workspace_id': self.ws.id, 'template_id': other.id,
            'helm_release_name': 'svc-nofmt', 'helm_namespace': 'paas-ws-gitset01',
            'helm_values': json.dumps({'git': {'tokenHosts': 'http://x'}}),
        })
        self.assertEqual(self._git(svc), {'tokenHosts': 'http://x'})

    def test_validate_returns_tenant_message(self):
        ok = {'git.tokenHosts': 'github.com', 'git.userName': '', 'git.userEmail': ''}
        self.assertIsNone(self.tpl._validate_spec_formats(ok))
        self.assertIsNone(self.tpl._validate_spec_formats({}))
        for flat, needle in (
                ({'git.tokenHosts': 'http://gitea.local'}, 'https'),
                ({'git.tokenHosts': 'bad_host.example.com'}, 'host'),
                ({'git.tokenHosts': 'git example.com'}, 'comma'),
                ({'git.tokenHosts': 'gitea'}, 'github.com'),
                ({'git.userName': 'a\u2028b'}, '100'),
                ({'git.userName': 'a;b'}, '100'),
                ({'git.userEmail': 'nope'}, 'email'),
        ):
            msg = self.tpl._validate_spec_formats(dict(ok, **flat))
            self.assertTrue(msg, flat)
            self.assertIn(needle.lower(), msg.lower(), (flat, msg))

    def test_validate_messages_are_translated_for_zh_tw(self):
        self.env['res.lang']._activate_lang('zh_TW')
        tpl = self.tpl.with_context(lang='zh_TW')
        msg = tpl._validate_spec_formats({'git.tokenHosts': 'http://gitea.local'})
        self.assertIn('只接受 https', msg)
        msg = tpl._validate_spec_formats({'git.tokenHosts': 'git example.com'})
        self.assertIn('逗號分隔', msg)
        msg = tpl._validate_spec_formats({'git.tokenHosts': 'gitea'})
        self.assertIn('完整的主機名稱', msg)
        msg = tpl._validate_spec_formats({'git.userName': 'a\u202eb'})
        self.assertIn('控制字元', msg)

    def _update(self, values):
        client = MagicMock()
        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client), \
                patch(f'{_CONTROLLER}.request', new=MagicMock(env=self.env)):
            result = PaasController()._update_service(self.svc, values=values, version=None)
        return result, client

    def test_reconfigure_rejects_invalid_git_settings_before_any_write(self):
        before = self.svc._helm_values_dict()
        for values, needle in (
                ({'git.tokenHosts': 'http://gitea.local'}, 'https'),
                ({'git.tokenHosts': 'git example.com'}, 'comma'),
                ({'git.userName': 'x\u2028[core]'}, 'control'),
        ):
            result, client = self._update(values)
            self.assertFalse(result.get('success'), (values, result))
            self.assertIn(needle, result.get('error', ''), values)
            client.upgrade_release.assert_not_called()
            self.svc.invalidate_recordset()
            self.assertEqual(self.svc._helm_values_dict(), before, values)
            self.assertEqual(self.svc.state, 'running', values)

# -*- coding: utf-8 -*-
"""商用驗收 X-SEC-004-2／X-SEC-005-7：n8n 容器達到 PSA restricted、不掛 SA token。

n8n 用 8gears 上游 chart 2.0.1（chart 不在我們手上）。chart 預設 ``main.securityContext``
是 ``{}``、pod 沒有 seccomp，所以 n8n 容器帶著 runtime 的預設 capability（含 NET_RAW）、
可以提權、沒有 seccomp。chart 會建自己的 ServiceAccount，但 SA 和 pod 都沒有設
``automountServiceAccountToken``，連最新的 2.1.1 也沒有這個欄位，結果每個 n8n 容器都拿到
一顆 token。n8n 不呼叫 Kubernetes API。

平台範本 ``helm_default_values`` 補上：

- ``main.securityContext``：allowPrivilegeEscalation=false、drop ALL、runAsNonRoot、
  seccomp RuntimeDefault（chart 預設的 podSecurityContext 已經是 uid/gid 1000、runAsNonRoot）。
- ``main.podSecurityContext``：只寫 ``seccompProfile``，helm 會和 chart 預設的
  runAsUser／fsGroup 合併，不會蓋掉。
- ``main.extraVolumes``／``main.extraVolumeMounts``：在
  ``/var/run/secrets/kubernetes.io/serviceaccount`` 唯讀掛一個空的 emptyDir。Kubernetes 的
  ServiceAccount admission 遇到容器在這個路徑已經有掛載時，就不注入 token volume（上游行為）。
  chart 沒有 automount 欄位，這是 values 唯一做得到的方法。沙盒實測：pod 沒有
  kube-api-access volume，目錄是空的且唯讀，n8n 2.43.6 正常開機、/healthz/readiness 200。

這四個鍵也列進 ``_TEMPLATE_POLICY_VALUE_PATHS``，既有服務 Redeploy 時以範本為準。
"""
import copy
import json

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.woow_paas_platform.models.cloud_service import _TEMPLATE_POLICY_VALUE_PATHS

TOKEN_DIR = '/var/run/secrets/kubernetes.io/serviceaccount'
POLICY_PATHS = [
    ('main', 'securityContext'),
    ('main', 'podSecurityContext'),
    ('main', 'extraVolumes'),
    ('main', 'extraVolumeMounts'),
]


@tagged('post_install', '-at_install')
class TestN8nPodSecurity(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Svc = self.env['woow_paas_platform.cloud_service']
        self.tpl = self.env.ref('woow_paas_platform.cloud_app_n8n')
        self.base = json.loads(self.tpl.helm_default_values)
        self.main = self.base['main']

    def test_container_security_context_is_restricted(self):
        sc = self.main.get('securityContext') or {}
        self.assertIs(sc.get('allowPrivilegeEscalation'), False)
        self.assertEqual((sc.get('capabilities') or {}).get('drop'), ['ALL'])
        self.assertFalse((sc.get('capabilities') or {}).get('add'))
        self.assertIs(sc.get('runAsNonRoot'), True)
        self.assertEqual(sc.get('seccompProfile'), {'type': 'RuntimeDefault'})
        self.assertNotIn('privileged', sc)

    def test_pod_seccomp_only_so_chart_identity_survives(self):
        """只寫 seccompProfile：helm 把它和 chart 預設的 runAsUser 1000／fsGroup 1000 合併。"""
        self.assertEqual(self.main.get('podSecurityContext'),
                         {'seccompProfile': {'type': 'RuntimeDefault'}})

    def test_service_account_token_is_shadowed(self):
        vols = self.main.get('extraVolumes') or []
        mounts = self.main.get('extraVolumeMounts') or []
        token_mounts = [m for m in mounts if m.get('mountPath') == TOKEN_DIR]
        self.assertEqual(len(token_mounts), 1, mounts)
        mount = token_mounts[0]
        self.assertIs(mount.get('readOnly'), True)
        vol = [v for v in vols if v.get('name') == mount.get('name')]
        self.assertEqual(len(vol), 1, vols)
        # emptyDir（PSA restricted 允許的 volume 類型），不是 secret／projected／hostPath
        self.assertEqual(set(vol[0]) - {'name'}, {'emptyDir'})
        self.assertEqual(vol[0]['emptyDir'].get('sizeLimit'), '1Mi')

    def test_hardening_keys_are_template_policy(self):
        for path in POLICY_PATHS:
            self.assertIn(path, _TEMPLATE_POLICY_VALUE_PATHS['n8n'], path)
        # 既有的 #1637 探針仍在
        self.assertIn(('main', 'livenessProbe'), _TEMPLATE_POLICY_VALUE_PATHS['n8n'])
        self.assertIn(('main', 'readinessProbe'), _TEMPLATE_POLICY_VALUE_PATHS['n8n'])

    def test_redeploy_brings_hardening_to_an_existing_service(self):
        """修補前開通的服務：存的 values 沒有這四個鍵（或 securityContext={}）⇒ Redeploy 補上，
        租戶的 main.config／main.secret 不動。"""
        stored = copy.deepcopy(self.base)
        for key in ('securityContext', 'podSecurityContext', 'extraVolumes', 'extraVolumeMounts'):
            stored['main'].pop(key, None)
        stored['main']['securityContext'] = {}
        stored['main']['config']['N8N_SMTP_HOST'] = 'smtp.example.com'
        stored['main']['secret'] = {'N8N_API_KEY': 'real-key-kept', 'N8N_SMTP_PASS': 'kept'}
        out = self.Svc._inject_template_policy_keys(stored, self.tpl)
        for key in ('securityContext', 'podSecurityContext', 'extraVolumes', 'extraVolumeMounts'):
            self.assertEqual(out['main'][key], self.main[key], key)
        self.assertEqual(out['main']['config']['N8N_SMTP_HOST'], 'smtp.example.com')
        self.assertEqual(out['main']['secret'],
                         {'N8N_API_KEY': 'real-key-kept', 'N8N_SMTP_PASS': 'kept'})

    def test_tenant_cannot_set_the_hardening_keys(self):
        specs = json.loads(self.tpl.helm_value_specs or '{}')
        rows = (specs.get('required') or []) + (specs.get('optional') or []) \
            if isinstance(specs, dict) else specs
        keys = [s.get('key', '') for s in rows if isinstance(s, dict)]
        self.assertTrue(keys, 'n8n helm_value_specs parsed to nothing')
        for key in keys:
            for path in POLICY_PATHS:
                prefix = '.'.join(path)
                self.assertFalse(key == prefix or key.startswith(prefix + '.'),
                                 '%s is tenant-settable' % key)

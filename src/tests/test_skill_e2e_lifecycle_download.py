"""E2E — skill lifecycle ↔ download endpoint visibility ↔ is_latest invariant.

Spec: docs/superpowers/specs/2026-05-28-skill-e2e-and-is-latest-drift-fix.md §④-a

測什麼：
- lifecycle transition（action_publish / deprecate / archive_skill）→
  既有 download endpoint /api/agent-runtimes/skill-bundle 可見性切換
- drift 防護的 HTTP 鏈路驗證：直接 create/write({'state':'published'}) →
  download 立刻 200（state gate 過）+ is_latest=True（修法後）
- migration 對 drift 樣本的修補

不測什麼（信任既有覆蓋）：
- SB.1-6 / C-1（token auth / token_use scope / 跨 project 防護）→
  既有 TestPhaseCSkillBundleEndpoint 覆蓋
- lifecycle 純單元邏輯（多版本 recompute / illegal guards）→
  PR #81 TestSkillAdminLifecycle 覆蓋
- HTTP 上傳 API → 不存在（scope 不擴）
"""
import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from urllib.parse import urlencode

from odoo.tests.common import HttpCase, tagged


# 借 Phase C 的 HS256 signer / token-crafter（pattern 對齊，不重造）
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _sign_hs256(payload: dict, secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    seg = (
        _b64url(json.dumps(header).encode())
        + "."
        + _b64url(json.dumps(payload).encode())
    )
    sig = hmac.new(secret.encode(), seg.encode(), hashlib.sha256).digest()
    return seg + "." + _b64url(sig)


def _skill_bundle_token(project_id, secret, exp_offset=3600):
    now = int(time.time())
    return _sign_hs256({
        "iss": "paas-operator",
        "project_id": project_id,
        "workspace_ids": [],
        "iat": now,
        "exp": now + exp_offset,
        "jti": uuid.uuid4().hex,
        "token_use": "skill_bundle",
    }, secret)


_AUTH_SECRET = "e2e-skill-lifecycle-secret-0123456789"
_ENV_SECRET_CURRENT = "AGENT_RUNTIME_AUTH_SECRET_CURRENT"
_ENV_SECRET_PREVIOUS = "AGENT_RUNTIME_AUTH_SECRET_PREVIOUS"


@tagged('post_install', '-at_install')
class TestSkillE2ELifecycleDownload(HttpCase):
    """E2E lifecycle ↔ download visibility ↔ is_latest."""

    _ENDPOINT = '/api/agent-runtimes/skill-bundle'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for required in ('woow_paas_platform.skill',):
            if required not in cls.env.registry:
                raise RuntimeError(
                    f"prerequisite: {required} not registered."
                )
        cls.Project = cls.env['project.project']
        cls.Skill = cls.env['woow_paas_platform.skill']
        cls.Attachment = cls.env['ir.attachment']

    def _set_env(self, key, value):
        prev = os.environ.get(key)
        os.environ[key] = value
        self.addCleanup(
            lambda: os.environ.__setitem__(key, prev) if prev is not None
            else os.environ.pop(key, None)
        )

    def setUp(self):
        super().setUp()
        self.suffix = uuid.uuid4().hex[:8]
        self._set_env(_ENV_SECRET_CURRENT, _AUTH_SECRET)
        self._set_env(_ENV_SECRET_PREVIOUS, "previous-not-used")
        self.project = self.Project.sudo().create(
            {'name': f'E2E-Proj {self.suffix}'}
        )

    def _make_skill(self, name, version, state='draft', bundle_bytes=None):
        """Build a skill + attached bundle. state default draft."""
        if bundle_bytes is None:
            bundle_bytes = (
                b'\x1f\x8b\x08RAW-' + name.encode()
                + b'-' + version.encode()
            )
        skill = self.Skill.sudo().create({
            'name': name,
            'version': version,
            'project_id': self.project.id,
            'bundle_sha256': hashlib.sha256(bundle_bytes).hexdigest(),
            'state': state,
        })
        att = self.Attachment.sudo().create({
            'name': f'{name}-{version}.tar.gz',
            'datas': base64.b64encode(bundle_bytes),
            'res_model': 'woow_paas_platform.skill',
            'res_id': skill.id,
            'mimetype': 'application/gzip',
            'public': False,
        })
        skill.sudo().write({'bundle_attachment_id': att.id})
        return skill, bundle_bytes

    def _get_bundle(self, skill_name, version):
        token = _skill_bundle_token(self.project.id, _AUTH_SECRET)
        url = f"{self._ENDPOINT}?" + urlencode({
            'project_hash': 'p-irrelevant',
            'skill_name': skill_name,
            'version': version,
        })
        return self.url_open(
            url,
            headers={'Authorization': f'Bearer {token}'},
            timeout=15,
        )

    # -- lifecycle ↔ download visibility ------------------------------- #

    def test_draft_skill_download_returns_404(self):
        """state=draft → download endpoint 404（state gate 過濾）。"""
        skill, _ = self._make_skill('alpha', '1.0.0', state='draft')
        resp = self._get_bundle('alpha', '1.0.0')
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(skill.is_latest)

    def test_action_publish_then_download_200_and_is_latest_true(self):
        """draft → action_publish → download 200 + raw bytes + is_latest=True。"""
        skill, raw = self._make_skill('beta', '1.0.0', state='draft')
        skill.action_publish()
        resp = self._get_bundle('beta', '1.0.0')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.content, raw,
            "body 必為 raw bundle bytes（非 base64）",
        )
        skill.invalidate_recordset()
        self.assertTrue(skill.is_latest)
        self.assertTrue(skill.published_at)

    def test_deprecate_then_download_404_and_latest_reassigned(self):
        """v1+v2 publish（v2 latest）→ deprecate v2 → v2 download 404、
        v2 is_latest=False、v1 接手 is_latest=True。"""
        v1, _ = self._make_skill('gamma', '1.0.0')
        v1.action_publish()
        v2, _ = self._make_skill('gamma', '2.0.0')
        v2.action_publish()
        v2.action_deprecate()
        resp = self._get_bundle('gamma', '2.0.0')
        self.assertEqual(resp.status_code, 404)
        v1.invalidate_recordset()
        v2.invalidate_recordset()
        self.assertFalse(v2.is_latest)
        self.assertTrue(v1.is_latest, "is_latest must fall back to v1")

    def test_archive_then_download_404(self):
        """publish → archive → download 404。"""
        skill, _ = self._make_skill('delta', '1.0.0')
        skill.action_publish()
        skill.action_archive_skill()
        resp = self._get_bundle('delta', '1.0.0')
        self.assertEqual(resp.status_code, 404)
        skill.invalidate_recordset()
        self.assertFalse(skill.is_latest)

    # -- drift 防護 HTTP 鏈路 ---------------------------------------- #

    def test_create_published_directly_drift_via_http(self):
        """直接 create({'state':'published'}) → download 200 + is_latest=True
        （修法後 create override 觸發 recompute）。"""
        skill, raw = self._make_skill('eps', '1.0.0', state='published')
        # 修法後 create override 已補 published_at + recompute
        skill.invalidate_recordset()
        self.assertTrue(skill.is_latest, "drift 修法後必為 latest")
        self.assertTrue(skill.published_at)
        resp = self._get_bundle('eps', '1.0.0')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, raw)

    def test_write_state_to_published_drift_via_http(self):
        """create(draft) → write({'state':'published'}) → download 200
        + is_latest=True（修法後 write override 觸發 recompute）。"""
        skill, raw = self._make_skill('zeta', '1.0.0', state='draft')
        skill.write({'state': 'published'})
        skill.invalidate_recordset()
        self.assertTrue(skill.is_latest)
        self.assertTrue(skill.published_at)
        resp = self._get_bundle('zeta', '1.0.0')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, raw)

    # -- migration drift-fix 驗證 ----------------------------------- #

    def test_migration_fixes_existing_drift_sample(self):
        """純 SQL 製造 drift（published skill 但 is_latest=False）→
        呼叫 migration 重算 → drift 修復。

        Migration 載入用 ``importlib.util.spec_from_file_location``，
        路徑由 addon 根目錄推導；目錄名 ``18.0.1.0.28`` 含 ``.``、檔名
        ``post-fix-is-latest.py`` 含 ``-``，但 ``spec_from_file_location``
        對任意路徑都 work（不依賴 Python module 名規則）。
        """
        skill, _ = self._make_skill('eta', '1.0.0', state='draft')
        skill.action_publish()
        self.assertTrue(skill.is_latest)
        # 製造 drift：直接 SQL 把 is_latest 改 False（繞過任何 hook）
        self.env.cr.execute(
            "UPDATE woow_paas_platform_skill "
            "SET is_latest = FALSE WHERE id = %s",
            [skill.id],
        )
        skill.invalidate_recordset()
        self.assertFalse(skill.is_latest, "drift sample 製造成功")
        # 呼叫 migration（動態載入避免硬綁 module path）
        import importlib.util
        import odoo.addons.woow_paas_platform as _addon
        manifest_dir = os.path.dirname(_addon.__file__)
        mig_path = os.path.join(
            manifest_dir, 'migrations', '18.0.1.0.28',
            'post-fix-is-latest.py',
        )
        spec = importlib.util.spec_from_file_location("mig28", mig_path)
        mig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mig)
        mig.migrate(self.env.cr, '18.0.1.0.28')
        skill.invalidate_recordset()
        self.assertTrue(skill.is_latest, "migration 應修復 drift")

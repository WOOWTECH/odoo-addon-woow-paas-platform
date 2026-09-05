# -*- coding: utf-8 -*-
"""#1052：``src/migrations/*/post-migrate.py`` 呼叫的每個 private delegate
（``env['<model>']._xxx(...)``）都必須還活在它的 model 上。

背景
----
本 repo 的 migration 慣例是 delegate-to-model（``.52/.54/.59/.69/.84/.87`` 的 post-migrate.py
都寫「循 delegate 慣例」）：migration 檔只負責呼叫，邏輯放 model method 供單元測試直驅——
``src/migrations/<ver>/post-migrate.py`` 帶連字號、目錄無 ``__init__.py``，本來就 import
不進來。代價是那些 method 在 manifest 已走到 ``18.0.1.0.177`` 時看起來像 dead code
（它們住在 ``.52``–``.87``），一張「清理」PR 刪掉任一支，PR CI 全綠（odoo-tests 不掛
pull_request），只在舊 DB 升級時 ``AttributeError``。架構覆核 #1052 量到 8 支 ``_woow_migrate_*`` delegate 只有
1 支有刪除守衛；review 時再撈出 5 支不帶那個前綴的（``.40/.42/.43/.69/.176``），一併納入（``test_plan_visibility_336.py`` 的 ``hasattr`` 斷言）。本檔把那個守衛
一般化：掃 migration 檔的呼叫點，逐一 ``hasattr``。

判準與陽性對照
--------------
* 掃描是**否定性**判準（「找不到失蹤的 delegate」）——掃描器壞了就恆綠。所以另有一條
  ``test_scanner_finds_known_delegates`` 斷言 ``_KNOWN`` 的 13 個 (model, method) 對**都被掃到**：
  migration 檔只增不減（append-only），這個集合是 monotonic 的，用「⊇ 已知集合」當
  下界符合 docs/decisions/patterns/phase-versioned-test-assertion.md 的前提；它**不是**
  docs/decisions/patterns/seed-topology-invariants.md 講的會下降的量。
* method 名只要求是 private（單底線開頭、非 dunder）——不限 ``_woow_migrate_`` 前綴，
  因為 ``.40/.42/.43/.69/.176`` 的 delegate 都不帶它。呼叫語法兩種都認：
  ``env['<model>']._x()`` 與先綁別名的 ``Plan = env['<model>']`` / ``Plan._x()``（``.79`` 用後者）。別名解析只在
  同一檔內、只認 ``<alias> = env['...']`` 這一種綁法；其他形狀會讓那個呼叫點掃不到——
  而「掃不到」會被上面那條已知集合斷言抓住（新增的 delegate 若用了第三種語法，請把它
  加進 ``_KNOWN`` 並擴充 regex）。

RED 示範：把 ``_woow_migrate_hide_hermes_336`` 改名或刪掉 ⇒ ``test_every_delegate_resolves``
在那個 subTest 紅，訊息沿用 ``test_plan_visibility_336.py`` 的措辭。
"""
import re
from pathlib import Path

from odoo.tests.common import TransactionCase, tagged

_MIGRATIONS = Path(__file__).resolve().parents[1] / 'migrations'

_DIRECT_CALL = re.compile(
    r"env\[(['\"])(?P<model>[\w.]+)\1\]\s*\.\s*(?P<method>_(?!_)\w+)\s*\(")
_ALIAS_BIND = re.compile(
    r"^\s*(?P<alias>\w+)\s*=\s*env\[(['\"])(?P<model>[\w.]+)\2\]\s*$", re.M)
_ALIAS_CALL = re.compile(
    r"\b(?P<alias>\w+)\s*\.\s*(?P<method>_(?!_)\w+)\s*\(")

#: 2026-09-05 的已知 delegate（陽性對照）。migration append-only ⇒ 只會變多。
_KNOWN = frozenset({
    ('sh.subscription.plan', '_woow_migrate_pricing_330'),
    ('sh.subscription.plan', '_woow_migrate_hide_hermes_336'),
    ('sh.subscription.plan', '_woow_migrate_cs_spec_332'),
    ('sh.subscription.plan', '_woow_migrate_reseed_b_330'),
    ('sh.subscription.plan', '_woow_migrate_unhide_hermes_431'),
    ('sh.subscription.plan', '_woow_migrate_hermes_cs_spec_431'),
    ('woow_paas_platform.cloud_service', '_woow_migrate_refresh_transition_anchor_489'),
    ('woow_paas_platform.cloud_service', '_woow_migrate_converge_stuck_suspended_497'),
    # 不帶 _woow_migrate_ 前綴的五支（#1052 review 補）。
    ('woow_paas_platform.agent_session', '_backfill_reaped_since_orphans'),
    ('woow_paas_platform.cloud_service', '_backfill_reach_isolation_p5'),
    ('woow_paas_platform.mcp_server', '_heal_sidecar_backed_non_cluster_url'),
    ('product.template', '_woow_clear_subscription_product_taxes_366'),
    ('res.lang', '_ensure_zh_tw_lang_active'),
})


def _scan_delegate_calls():
    """回 ``[(migration rel path, model, method)]``。"""
    sites = []
    for path in sorted(_MIGRATIONS.glob('*/post-migrate.py')):
        text = path.read_text(encoding='utf-8')
        rel = '%s/%s' % (path.parent.name, path.name)
        for m in _DIRECT_CALL.finditer(text):
            sites.append((rel, m.group('model'), m.group('method')))
        aliases = {m.group('alias'): m.group('model')
                   for m in _ALIAS_BIND.finditer(text)}
        for m in _ALIAS_CALL.finditer(text):
            model = aliases.get(m.group('alias'))
            if model:
                sites.append((rel, model, m.group('method')))
    return sites


@tagged('post_install', '-at_install')
class TestMigrationDelegateLiveness(TransactionCase):

    def test_scanner_finds_known_delegates(self):
        """陽性對照：掃描器必須掃到 ``_KNOWN`` 的每一對，否則後面那條恆綠。"""
        self.assertTrue(_MIGRATIONS.is_dir(), 'migrations 目錄不在：%s' % _MIGRATIONS)
        found = {(model, method) for _rel, model, method in _scan_delegate_calls()}
        missing = sorted(_KNOWN - found)
        self.assertFalse(
            missing,
            '掃描器沒掃到已知的 delegate 呼叫點 %s——不是 delegate 不見了（那是下一條的事），'
            '是 regex 對不上新的呼叫語法；先修掃描器' % missing)

    def test_every_delegate_resolves_on_its_model(self):
        """每個被 migration 呼叫的 delegate 都必須還在 model 上（舊 DB 升級的承重點）。"""
        sites = _scan_delegate_calls()
        self.assertGreaterEqual(len(sites), len(_KNOWN))
        for rel, model, method in sites:
            with self.subTest(migration=rel, model=model, method=method):
                self.assertIn(model, self.env, '%s 呼叫了不存在的 model %s' % (rel, model))
                self.assertTrue(
                    hasattr(self.env[model], method),
                    '%s.%s 被刪除——src/migrations/%s 仍在呼叫它，舊 DB 升級會 '
                    'AttributeError。delegate 要跟著 migration 一起活到那個版本退場'
                    % (model, method, rel))

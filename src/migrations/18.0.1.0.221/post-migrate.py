"""omnigent chart 0.1.7（上游 v0.17.0）：既有服務的 ``runner.image`` 改成範本的新釘。

Redeploy 的 #1408 只刷 ``{repository, tag}`` 形式的 image，omnigent 的 ``runner.image`` 是
字串，刷不到 ⇒ 既有服務 Redeploy 後雲端工作機會停在 0.16。本檔在 seed 已更新後（post）把
仍指向平台 ``omnigent-host`` 鏡像的 ``runner.image`` 改成範本值；叢集要等管理員 Redeploy
才換。規則見 ``CloudService._woow_migrate_omnigent_runner_image_017``。

⚠ 目錄版號必須**大於**升級前的 ``__manifest__.py`` 版號，否則整支靜默 no-op。本檔配
manifest 由 ``18.0.1.0.220`` bump 到 ``18.0.1.0.221``（.220 已被 release PR 佔用）；若 develop 先有別的 migration／release
用掉這個號，要一起改名（``scripts/ci/manifest-version-gate.py`` 規則 B）。

邏輯放 model（delegate-to-model 慣例），本檔只負責呼叫，單元測試直接驗那個 method。
"""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['woow_paas_platform.cloud_service']._woow_migrate_omnigent_runner_image_017()

"""Host-side asset/entrypoint guard; not a substitute for Odoo asset compilation.

Run: python3 tests/test_portal_paas_asset_contract_20261001.py
"""
import ast
from pathlib import Path
import re
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
PAAS = ROOT / "src/static/src/paas"
BUNDLE = "woow_paas_platform.assets_paas"
# Standalone OWL stack plus the installed Bootstrap 5.3 map prerequisites.
# Omitting variables-dark/maps can silently reuse stale CSS on an upgraded DB.
STANDALONE_STACK = [
    ("include", "web._assets_helpers"),
    "web/static/src/scss/pre_variables.scss",
    "web/static/lib/bootstrap/scss/_variables.scss",
    "web/static/lib/bootstrap/scss/_variables-dark.scss",
    "web/static/lib/bootstrap/scss/_maps.scss",
    ("include", "web._assets_bootstrap"),
    ("include", "web._assets_core"),
]


class TestPortalPaaSAssetContract20261001(unittest.TestCase):
    def setUp(self):
        self.manifest = ast.literal_eval((ROOT / "src/__manifest__.py").read_text())
        self.assets = self.manifest["assets"]
        self.view = ET.parse(ROOT / "src/views/paas_app.xml")

    def test_standalone_stack_and_only_paas_files_after_it(self):
        bundle = self.assets[BUNDLE]
        self.assertEqual(bundle[:len(STANDALONE_STACK)], STANDALONE_STACK)
        # No backend/website/frontend includes, direct webclient files, or
        # third-party ERP bundles hidden after the approved standalone stack.
        for entry in bundle[len(STANDALONE_STACK):]:
            self.assertIsInstance(entry, str)
            self.assertTrue(entry.startswith("woow_paas_platform/static/src/paas/"), entry)
        self.assertLess(
            bundle.index("woow_paas_platform/static/src/paas/styles/00_variables.scss"),
            bundle.index("woow_paas_platform/static/src/paas/styles/10_base.scss"),
        )

    def test_external_imports_stay_within_core_contract(self):
        allowed = {
            "@odoo/owl", "@web/core/assets", "@web/core/l10n/dates",
            "@web/core/l10n/localization", "@web/core/l10n/translation",
            "@web/core/utils/numbers", "@web/env", "@web/session",
        }
        imports = set()
        for path in PAAS.rglob("*.js"):
            # Also catch bare and dynamic external imports, not only `from`.
            imports.update(re.findall(r'[\'"](@[^\'"\s]+)[\'"]', path.read_text()))
        self.assertTrue(imports)
        self.assertFalse(imports - allowed, imports - allowed)

    def test_boot_entry_is_not_in_backend_or_website_bundles(self):
        for name in ("web.assets_backend", "web.assets_frontend"):
            for entry in self.assets[name]:
                if not isinstance(entry, str):
                    self.assertNotEqual(entry, ("include", BUNDLE))
                    continue
                pattern = entry.removeprefix("woow_paas_platform/")
                matches = set((ROOT / "src").glob(pattern))
                self.assertFalse(matches & {PAAS / "app.js", PAAS / "boot.js"}, name)

    def test_server_fallback_and_separate_mount_target(self):
        body = self.view.find(".//body")
        fallback = body.find("./section[@id='woow-paas-boot']")
        target = body.find("./div[@id='woow-paas-root']")
        self.assertIsNotNone(fallback)
        self.assertIsNotNone(target)
        self.assertEqual(target.attrib["data-boot-state"], "loading")
        self.assertTrue(fallback.find(".//*[@data-boot-message]").text.strip())
        self.assertTrue(fallback.find(".//*[@data-boot-failure]").text.strip())
        self.assertIsNotNone(fallback.find("./noscript/p"))
        self.assertEqual(fallback.find("./a").attrib["href"], "/woow")
        self.assertEqual(self.view.find(".//*[@t-call-assets]").attrib["t-call-assets"], BUNDLE)
        app = (PAAS / "app.js").read_text()
        self.assertIn("bootPaaS(", app)
        self.assertIn("mountComponent(Root, target)", app)
        self.assertIn('getElementById("woow-paas-root")', app)
        self.assertIn('getElementById("woow-paas-boot")', app)
        for path in (PAAS / "app.js", PAAS / "boot.js"):
            source = path.read_text()
            self.assertNotIn("innerHTML", source)
            self.assertNotIn("user_companies", source)
            self.assertNotIn("is_internal_user", source)


if __name__ == "__main__":
    unittest.main()

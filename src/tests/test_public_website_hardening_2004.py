"""Pure regression tests for the database-backed website patch (#2004)."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

from odoo.tests.common import BaseCase


MODULE_ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = MODULE_ROOT / "services" / "public_website_hardening.py"
MIGRATION_PATH = (
    MODULE_ROOT / "migrations" / "18.0.1.0.266" / "post-migrate.py"
)

spec = importlib.util.spec_from_file_location("public_website_hardening", HELPER_PATH)
hardening = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(hardening)


GLOBAL_ARCH = """<data>
<style>.existing { color: red; }</style>
<script>
var mObj={
"start_url":"/",
"icons":[
{"src":"/web/image/website/1/logo","sizes":"192x192","type":"image/png"},
{"src":"/web/image/website/1/logo","sizes":"512x512","type":"image/png"}
]};
if(!document.querySelector("meta[name=apple-mobile-web-app-capable]")){
var am=document.createElement("meta");am.name="apple-mobile-web-app-capable";
am.content="yes";document.head.appendChild(am);}
</script>
</data>"""

NAVBAR_ARCH = """<data>
<button class="wt-toggle-btn" id="wtNavThemeToggle"
        title="Toggle light/dark mode" aria-label="Toggle theme"/>
</data>"""

HOMEPAGE_ARCH = """<div>
<section data-name="Hero"><h1>Hero</h1>
<h3 class="mb-1">One</h3><h3 class="mb-1">Two</h3><h3 class="mb-1">Three</h3>
</section>
<section data-name="Columns and Flash"><h2>News</h2>
<h4>A</h4><h4>B</h4><h4>C</h4><h2>Columns</h2>
<h4>D</h4><h4>E</h4><h4>F</h4>
</section>
<section data-name="Latest Articles"><h2>Latest</h2><h3>Popular</h3><h4>Keep me</h4></section>
</div>"""


class PublicWebsiteHardeningTest(BaseCase):
    def test_global_patch_is_valid_idempotent_and_scoped(self):
        patched = hardening.patch_global_view(GLOBAL_ARCH)
        self.assertNotIn('"start_url":"/"', patched)
        self.assertIn('"start_url":window.location.origin+"/"', patched)
        self.assertEqual(
            patched.count(
                '{"src":window.location.origin+"/web/image/website/1/logo"'
            ),
            2,
        )
        self.assertIn("meta[name=mobile-web-app-capable]", patched)
        self.assertIn("WOOW #2004 mobile row overflow", patched)
        self.assertIn('section[data-name="Columns and Flash"] &gt;', patched)
        self.assertIn('section[data-name="Latest Articles"] &gt;', patched)
        self.assertNotIn("body { overflow-x", patched)
        ET.fromstring(patched)
        self.assertEqual(hardening.patch_global_view(patched), patched)

    def test_navbar_patch_removes_runtime_duplicate_id(self):
        patched = hardening.patch_navbar_view(NAVBAR_ARCH)
        self.assertNotIn('id="wtNavThemeToggle"', patched)
        self.assertIn('class="wt-toggle-btn"', patched)
        self.assertIn('data-wt-theme-toggle="nav"', patched)
        self.assertIn('aria-label="Toggle theme"', patched)
        ET.fromstring(patched)
        self.assertEqual(hardening.patch_navbar_view(patched), patched)

    def test_heading_patch_changes_only_the_two_reproduced_skips(self):
        patched = hardening.patch_homepage_view(HOMEPAGE_ARCH)
        self.assertEqual(patched.count('<h2 class="mb-1">'), 3)
        columns = patched.split('data-name="Columns and Flash"', 1)[1].split(
            "</section>", 1
        )[0]
        self.assertEqual(columns.count("<h3"), 6)
        latest = patched.split('data-name="Latest Articles"', 1)[1]
        self.assertIn("<h3>Popular</h3><h4>Keep me</h4>", latest)
        ET.fromstring(patched)
        self.assertEqual(hardening.patch_homepage_view(patched), patched)

    def test_every_translation_is_patched_without_changing_keys(self):
        original = {"en_US": GLOBAL_ARCH, "zh_TW": GLOBAL_ARCH}
        patched, changed = hardening.patch_arch_db(
            original, hardening.patch_global_view
        )
        self.assertEqual(set(patched), set(original))
        self.assertEqual(changed, 2)
        self.assertEqual(
            hardening.patch_arch_db(patched, hardening.patch_global_view),
            (patched, 0),
        )

    def test_unreviewed_drift_fails_closed(self):
        with self.assertRaises(hardening.WebsiteViewDrift):
            hardening.patch_global_view(GLOBAL_ARCH.replace('"start_url":"/"', ""))
        with self.assertRaises(hardening.WebsiteViewDrift):
            hardening.patch_homepage_view(HOMEPAGE_ARCH.replace("<h4>A</h4>", ""))

    def test_release_version_and_migration_are_paired(self):
        manifest = ast.literal_eval((MODULE_ROOT / "__manifest__.py").read_text())
        self.assertEqual(manifest["version"], "18.0.1.0.266")
        self.assertTrue(MIGRATION_PATH.is_file())


if __name__ == "__main__":
    unittest.main()

"""Real /woow bundle smoke tests (requires Odoo + Chromium in the sandbox).

No fake session/company payload and no RPC or asset stubs. These test bootstrap,
not the separate Portal ACL/workspace/billing acceptance matrix. Browser console
errors and uncaught exceptions remain fatal, including during page startup.

Registered in tests/__init__.py. These browser cases are not executed by the
host-side static/JavaScript checks.

Opt-out of the standard run (`-standard`, tag `portal_browser_1300`): every
method drives `HttpCase.browser_js`, and the paas-odoo image used by CI has no
Chromium and no `websocket-client`, so in CI they can only SkipTest (C4 then
rejects the unlisted skip) or pass vacuously inside subTests. Run them where a
browser exists with `--test-tags portal_browser_1300`.
"""
from odoo.tests import HttpCase, tagged
from odoo.tests.common import new_test_user


@tagged("-standard", "post_install", "-at_install", "portal_browser_1300")
class TestPortalPaaSBootstrap20261001(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        context = {"no_reset_password": True, "mail_create_nosubscribe": True}
        cls.portal = new_test_user(
            cls.env,
            login="portal_bootstrap_20261001",
            groups="base.group_portal",
            context=context,
        )
        cls.internal = new_test_user(
            cls.env,
            login="internal_bootstrap_20261001",
            groups="base.group_user",
            context=context,
        )

    def _assert_portal_identity(self):
        self.assertIn(self.env.ref("base.group_portal"), self.portal.groups_id)
        self.assertNotIn(self.env.ref("base.group_user"), self.portal.groups_id)
        self.assertNotIn(self.env.ref("base.group_system"), self.portal.groups_id)
        self.assertTrue(self.portal.share)

    def _smoke(self, user, internal):
        code = r"""
            (async () => {
                const assert = (condition, message) => {
                    if (!condition) throw new Error(message);
                };
                const modules = odoo.loader.modules;
                const { session } = modules.get("@web/session");
                const { user } = modules.get("@web/core/user");
                // Odoo consumes/deletes identity fields from session at boot.
                assert(user.isInternalUser === EXPECT_INTERNAL, "Wrong session identity");
                if (!EXPECT_INTERNAL) {
                    assert(!session.user_companies, "Portal must exercise missing user_companies");
                }
                const { registry } = modules.get("@web/core/registry");
                assert(!registry.category("services").contains("company"), "ERP company service leaked");
                assert(![...modules.keys()].some(name =>
                    name.startsWith("@web/webclient/") || name.startsWith("@website/")),
                    "Backend/website modules leaked into standalone app");
                const root = document.getElementById("woow-paas-root");
                assert(root.dataset.bootState === "ready", "Actual mountComponent did not succeed");
                assert(!document.getElementById("woow-paas-boot"), "Stale recovery panel");
                assert(root.querySelector(".o_woow_layout"), "Actual PaaS AppShell missing");
                assert(root.innerText.trim().length > 0, "Empty mounted app");
                assert(root.getBoundingClientRect().height > 0, "Invisible mounted app");
                // On an upgraded DB, Odoo may silently reuse OLD CSS after a
                // Sass failure. A mounted/styled-looking shell is not enough.
                const styles = [...document.querySelectorAll('link[rel="stylesheet"]')]
                    .filter(link => link.href.includes('woow_paas_platform.assets_paas'));
                assert(styles.length > 0, "Missing PaaS stylesheet");
                for (const style of styles) {
                    const response = await fetch(style.href);
                    assert(response.ok, "PaaS stylesheet did not load");
                    assert(!/A css error/i.test(await response.text()), "SCSS fallback served stale CSS");
                }
                // Cold-cache flash: with display=swap the icon ligature names
                // ("notifications", "dashboard"…) render as text until the font
                // arrives. Keep the icon font on block and every icon one glyph wide.
                const iconFont = document.querySelector('link[href*="Material+Symbols+Outlined"]');
                assert(iconFont && iconFont.href.includes("display=block"), "Icon font must not swap in ligature text");
                const probe = document.createElement("span");
                probe.className = "material-symbols-outlined";
                probe.style.fontSize = "24px";
                probe.textContent = "account_balance_wallet";
                document.body.append(probe);
                assert(Math.round(probe.getBoundingClientRect().width) === 24, "Icon box wider than one glyph");
                probe.remove();
                for (const element of [document.body, document.documentElement]) {
                    for (const pseudo of ['::before', '::after']) {
                        assert(!/css error/i.test(getComputedStyle(element, pseudo).content),
                            "Visible SCSS error banner");
                    }
                }
                // Let the initial dashboard request and render settle. Data/ACL
                // outcomes belong to Task B; they must not destroy the shell.
                const { dashboardService } = modules.get("@woow_paas_platform/paas/services/dashboard_service");
                await dashboardService._fetchPromise;
                await new Promise(requestAnimationFrame);
                await new Promise(requestAnimationFrame);
                assert(root.querySelector(".o_woow_layout"), "Shell disappeared after initial RPC");
                assert(root.innerText.trim().length > 0, "App became blank after initial RPC");
                console.log("test successful");
            })().catch(error => console.error(error));
        """.replace("EXPECT_INTERNAL", "true" if internal else "false")
        # Both the production bundle and debug asset ordering must work.
        for path in ("/woow", "/woow?debug=assets"):
            with self.subTest(path=path, login=user.login):
                self.browser_js(
                    path, code, login=user.login, timeout=90,
                    ready="Boolean(document.querySelector('#woow-paas-root[data-boot-state=ready], #woow-paas-root[data-boot-state=failed]'))",
                    error_checker=lambda message: True,
                )

    def test_portal_real_bundle_mounts_without_internal_session(self):
        self._assert_portal_identity()
        self._smoke(self.portal, internal=False)
        self._assert_portal_identity()

    def test_internal_real_bundle_still_mounts(self):
        self._smoke(self.internal, internal=True)

    def test_real_mount_rejection_has_safe_fallback_and_success_removes_it(self):
        self._assert_portal_identity()
        self.browser_js(
            "/woow",
            r"""
                (async () => {
                    const assert = (condition, message) => {
                        if (!condition) throw new Error(message);
                    };
                    const modules = odoo.loader.modules;
                    const { Component, onWillStart, xml } = modules.get("@odoo/owl");
                    const { mountComponent } = modules.get("@web/env");
                    const { bootPaaS } = modules.get("@woow_paas_platform/paas/boot");
                    // Reuse the real server-rendered fallback, not a hand-built
                    // test double. DOMParser does not execute fetched scripts.
                    const response = await fetch("/woow", { credentials: "same-origin" });
                    assert(response.ok, "Could not fetch boot template");
                    const page = new DOMParser().parseFromString(await response.text(), "text/html");
                    const fallback = page.getElementById("woow-paas-boot");
                    const target = document.createElement("div");
                    document.body.append(fallback, target);
                    // Normal loads must not flash the recovery panel (it is only
                    // revealed after a delay, by the template's inline style).
                    assert(getComputedStyle(fallback).visibility === "hidden", "Recovery panel flashes on normal load");
                    const safeMessage = fallback.querySelector("[data-boot-failure]").textContent;
                    let reachedWillStart = false;
                    class BrokenBoot extends Component {
                        static template = xml`<p>Should never render</p>`;
                        static props = {};
                        setup() {
                            onWillStart(async () => {
                                reachedWillStart = true;
                                throw new Error("<img src=x onerror=alert(1)> PRIVATE_BOOT_SENTINEL");
                            });
                        }
                    }
                    // Use actual mountComponent/Owl, not Promise.reject as a
                    // substitute. Reuse the live env to avoid starting services
                    // twice in this browser; do not fake the Portal session.
                    const env = odoo.__WOWL_DEBUG__.root.env;
                    const failed = await bootPaaS(
                        node => mountComponent(BrokenBoot, node, { env }), target, fallback,
                    );
                    assert(reachedWillStart, "Did not exercise actual Owl rejection");
                    assert(failed === false, "Rejected mount reported success");
                    assert(target.dataset.bootState === "failed", "Missing failed state");
                    assert(target.childElementCount === 0, "Partial app survived failure");
                    assert(fallback.isConnected, "Failure left no recovery panel");
                    assert(fallback.getBoundingClientRect().height > 0, "Recovery panel invisible");
                    assert(getComputedStyle(fallback).visibility === "visible", "Failure panel hidden by reveal delay");
                    assert(fallback.getAttribute("role") === "alert", "Failure not announced");
                    assert(fallback.querySelector("[data-boot-message]").textContent === safeMessage,
                        "Failure did not use safe local copy");
                    assert(!fallback.innerHTML.includes("PRIVATE_BOOT_SENTINEL"), "Exception exposed");
                    assert(!fallback.querySelector("img"), "Error HTML was inserted");
                    const retry = fallback.querySelector("a");
                    assert(retry.getAttribute("href") === "/woow", "Retry must use fresh navigation");
                    class HealthyBoot extends Component {
                        static template = xml`<p>Recovered</p>`;
                        static props = {};
                    }
                    let app;
                    const succeeded = await bootPaaS(async node => {
                        app = await mountComponent(HealthyBoot, node, { env });
                    }, target, fallback);
                    assert(succeeded === true, "Healthy mount failed");
                    assert(target.dataset.bootState === "ready", "Missing ready state");
                    assert(target.innerText === "Recovered", "Healthy component missing");
                    assert(!fallback.isConnected, "Stale fallback survived success");
                    app.destroy();
                    target.remove();
                    console.log("test successful");
                })().catch(error => console.error(error));
            """,
            login=self.portal.login,
            ready="Boolean(document.querySelector('#woow-paas-root[data-boot-state=ready]'))",
            timeout=90,
            # The deliberate Owl lifecycle fault may also reach Odoo's global
            # error logger after mount rejects. Only this unique injected
            # marker is expected; all real boot/assertion errors remain fatal.
            error_checker=lambda message: "PRIVATE_BOOT_SENTINEL" not in message,
        )
        self._assert_portal_identity()

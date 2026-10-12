"""Real workspace routing regression; run in an Odoo sandbox with Chromium.

These cases use the actual /woow bundle, router, components and HTTP endpoints.
No production fixtures or RPC stubs. Like the portal bootstrap browser tests,
opt out of standard CI because its Odoo image has no Chromium/websocket-client.
"""
from odoo.tests import HttpCase, tagged
from odoo.tests.common import new_test_user

from .smart_home_fixture import sh_model


@tagged("-standard", "post_install", "-at_install", "portal_browser_workspace_route")
class TestWorkspaceRouteIdentity20261011(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.owner = new_test_user(
            cls.env,
            login="workspace_route_owner_20261011",
            groups="base.group_portal",
            context={"no_reset_password": True, "mail_create_nosubscribe": True},
        )
        cls.workspaces = cls.env["woow_paas_platform.workspace"].create([
            {"name": "Route identity first", "owner_id": cls.owner.id},
            {"name": "Route identity second", "owner_id": cls.owner.id},
        ])
        cls.homes = sh_model(cls.env).create([
            {"name": "Route identity first home", "workspace_id": cls.workspaces[0].id},
            {"name": "Route identity second home", "workspace_id": cls.workspaces[0].id},
        ])

    def test_workspace_switch_remounts_and_closes_old_settings(self):
        first, second = self.workspaces
        code = r"""
            (async () => {
                const assert = (condition, message) => {
                    if (!condition) throw new Error(message);
                };
                const findPage = () => {
                    const visit = node => {
                        if (node.component.constructor.name === "WorkspaceDetailPage") {
                            return node.component;
                        }
                        for (const child of Object.values(node.children)) {
                            const result = visit(child);
                            if (result) return result;
                        }
                    };
                    return visit(odoo.__WOWL_DEBUG__.root.__owl__);
                };
                const loaded = async (id, previous) => {
                    const deadline = Date.now() + 20000;
                    while (Date.now() < deadline) {
                        const page = findPage();
                        if (page && page.props.workspaceId === id &&
                            page.state.workspace?.id === id && !page.state.loading &&
                            (!previous || page !== previous)) {
                            await new Promise(requestAnimationFrame);
                            assert(document.querySelector(".o_woow_workspace_detail_title h1")
                                ?.textContent === page.state.workspace.name,
                                "Rendered workspace differs from loaded workspace");
                            return page;
                        }
                        await new Promise(resolve => setTimeout(resolve, 50));
                    }
                    throw new Error("Workspace route did not load a fresh matching component: " + id);
                };
                const firstPage = await loaded(FIRST_ID);
                firstPage.goToSettings();
                await new Promise(requestAnimationFrame);
                await new Promise(requestAnimationFrame);
                assert(document.querySelector("#workspace_name"), "Settings did not open");
                location.hash = "/workspace/" + SECOND_ID;
                const secondPage = await loaded(SECOND_ID, firstPage);
                assert(!secondPage.state.showWorkspaceSettings, "Old settings state survived route change");
                assert(!document.querySelector("#workspace_name"), "Old settings modal survived route change");
                for (const timer of ["servicesPollingInterval", "smartHomesPollingInterval",
                    "securityAccessesPollingInterval"]) {
                    assert(firstPage[timer] === null, "Old workspace polling survived: " + timer);
                }
                history.back();
                const backPage = await loaded(FIRST_ID, secondPage);
                history.forward();
                await loaded(SECOND_ID, backPage);
                console.log("test successful");
            })().catch(error => console.error(error));
        """.replace("FIRST_ID", str(first.id)).replace("SECOND_ID", str(second.id))
        self.browser_js(
            f"/woow#/workspace/{first.id}", code, login=self.owner.login, timeout=90,
            ready="Boolean(document.querySelector('#woow-paas-root[data-boot-state=ready]'))",
            error_checker=lambda message: True,
        )

    def test_smart_home_switch_remounts_but_tab_switch_preserves_component(self):
        first, second = self.homes
        code = r"""
            (async () => {
                const assert = (condition, message) => {
                    if (!condition) throw new Error(message);
                };
                const findHome = () => {
                    const visit = node => {
                        if (node.component.constructor.name === "SmartHomeDetailPage") {
                            return node.component;
                        }
                        for (const child of Object.values(node.children)) {
                            const result = visit(child);
                            if (result) return result;
                        }
                    };
                    return visit(odoo.__WOWL_DEBUG__.root.__owl__);
                };
                const loaded = async (id, previous) => {
                    const deadline = Date.now() + 20000;
                    while (Date.now() < deadline) {
                        const page = findHome();
                        if (page && page.props.workspaceId === WORKSPACE_ID &&
                            page.props.homeId === id && page.state.home?.id === id &&
                            !page.state.loading && (!previous || page !== previous)) {
                            await new Promise(requestAnimationFrame);
                            assert(document.querySelector(".o_woow_smart_home_detail_title h1")
                                ?.textContent === page.state.home.name,
                                "Rendered Smart Home differs from loaded Smart Home");
                            return page;
                        }
                        await new Promise(resolve => setTimeout(resolve, 50));
                    }
                    throw new Error("Smart Home route did not load a fresh matching component: " + id);
                };
                const firstPage = await loaded(FIRST_HOME_ID);
                firstPage.showDeleteConfirm();
                await new Promise(requestAnimationFrame);
                await new Promise(requestAnimationFrame);
                assert(firstPage.state.showDeleteConfirm, "Delete confirmation did not open");
                location.hash = "/workspace/WORKSPACE_ID/smarthome/" + SECOND_HOME_ID;
                const secondPage = await loaded(SECOND_HOME_ID, firstPage);
                assert(!secondPage.state.showDeleteConfirm, "Old home's deletion state survived route change");
                assert(!document.querySelector(".o_woow_modal_backdrop"), "Old home's confirmation survived route change");
                assert(firstPage.pollingInterval === null, "Old home's polling survived route change");
                // A tab route changes props, but must keep the same home instance.
                // Pending fixtures have no Access seats; this checks identity,
                // not the separately covered Access capability/content gate.
                location.hash = "/workspace/WORKSPACE_ID/smarthome/SECOND_HOME_ID/access";
                await new Promise(resolve => setTimeout(resolve, 100));
                assert(findHome() === secondPage, "Tab route unnecessarily remounted the Smart Home");
                assert(secondPage.props.initialTab === "access", "Tab route did not update props");
                history.back();
                await new Promise(resolve => setTimeout(resolve, 100));
                assert(findHome() === secondPage, "Back between tabs unnecessarily remounted the Smart Home");
                history.back();
                await loaded(FIRST_HOME_ID, secondPage);
                console.log("test successful");
            })().catch(error => console.error(error));
        """.replace("WORKSPACE_ID", str(self.workspaces[0].id)).replace(
            "FIRST_HOME_ID", str(first.id)).replace("SECOND_HOME_ID", str(second.id))
        self.browser_js(
            f"/woow#/workspace/{self.workspaces[0].id}/smarthome/{first.id}",
            code, login=self.owner.login, timeout=90,
            ready="Boolean(document.querySelector('#woow-paas-root[data-boot-state=ready]'))",
            error_checker=lambda message: True,
        )

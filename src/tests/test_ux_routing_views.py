"""TDD red phase tests — UX / routing / view contracts (AC #20-25).

Plan: docs/refactor/plan-cloud-service-project-decouple.md (D4=C, D5=A)

These tests assert *file-level* contracts on the static assets and views
that the OWL frontend ships.  Full E2E coverage (playwright-cli) is
recommended in the plan's "E2E / UI tests" section but is *out of scope*
for the red phase — running a headed browser per test would dwarf the
rest of the suite.  Asset-content assertions give a deterministic red
signal that implementation must produce specific identifiers, while
preserving fast pytest runs.  E2E may be layered on top later.
"""
import re
from pathlib import Path

from odoo.modules import get_module_path
from odoo.tests.common import TransactionCase


class _AssetReader:
    """Read static assets from the addon path."""

    @classmethod
    def module_path(cls) -> Path:
        return Path(get_module_path('woow_paas_platform'))

    @classmethod
    def read(cls, *rel_parts: str) -> str:
        p = cls.module_path().joinpath(*rel_parts)
        return p.read_text(encoding='utf-8') if p.exists() else ''

    @classmethod
    def exists(cls, *rel_parts: str) -> bool:
        return cls.module_path().joinpath(*rel_parts).is_file()


class TestRouterAlias(TransactionCase):
    """AC #20 — both /woow#/ai-assistant and /woow#/projects route to Projects."""

    def test_ac20_router_declares_projects_path(self):
        """AC #20: router.js must register a ``projects`` route.

        Current router only has ``ai-assistant`` family; ``projects`` is
        missing — implementation must add it as an alias that lands on
        the same Projects list page.
        """
        router_src = _AssetReader.read('static/src/paas/core/router.js')
        self.assertTrue(router_src, "router.js not found")
        # Need either a "projects" path or routeName for alias
        self.assertRegex(
            router_src,
            r"""path\s*:\s*['"]projects['"]""",
            "AC #20: router.js must declare a `projects` path "
            "(alias for `ai-assistant`)",
        )

    def test_ac20_router_keeps_ai_assistant_for_bookmarks(self):
        """AC #20 (updated for commit 1d90953): the *bare* ``/woow#/ai-assistant``
        route was intentionally removed — it now resolves to the not-found page.
        The ``ai-assistant/*`` sub-paths (projects/tasks/chat) stay registered so
        existing deep bookmarks keep resolving to the merged Projects pages.

        We assert the sub-paths survive (that is the live bookmark-compat
        contract); a hard assertion on the obsolete bare route would re-break on
        the legitimate 1d90953 evolution (phase-versioned-test-assertion family,
        F-ARC-1 / gitea #131).
        """
        router_src = _AssetReader.read('static/src/paas/core/router.js')
        self.assertTrue(router_src, "router.js not found")
        for subpath in ('ai-assistant/projects', 'ai-assistant/tasks',
                        'ai-assistant/chat'):
            self.assertRegex(
                router_src,
                rf"""path\s*:\s*['"]{subpath}""",
                f"AC #20: `{subpath}` sub-path must stay registered so "
                "existing bookmarks keep resolving (bare `ai-assistant` was "
                "intentionally removed in 1d90953)",
            )


class TestSidebarEntry(TransactionCase):
    """AC #21 — sidebar entry shows ``Projects`` label."""

    def test_ac21_sidebar_navitem_renamed_to_projects(self):
        """AC #21: sidebar registers a ``Projects`` nav item.

        Open Q4（plan §Open Q4「保留英文字面 Projects」）**已被 #107 Q2(a)
        取代**（2026-08-26 Eugene 裁，載體＝#908 留言）：前端英文一律走 .po
        翻譯，sidebar 這一項照翻。⇒ 本斷言要的是 ``_t()`` 包裝形，**不是**
        裸字面量；Q4 不再是現行依據，別再拿它當「保留英文」的理由。

        為什麼不寫成「裸字面或 ``_t()`` 都收」：那種形狀對「有人把 ``_t()``
        解包回裸字面」完全沒有鑑別力，而那正是讓翻譯**靜默消失**的改動——
        babel jslexer 只抽 ``_t(...)`` 呼叫，解包後 .pot 少一條、畫面永遠
        英文，卻不會有任何 gate 變紅（#903／#906 同族已吃過一次）。守住
        ``_t()`` 形＝同時守住「label 存在」與「label 可譯」兩件事。
        """
        sidebar_src = _AssetReader.read(
            'static/src/paas/layout/sidebar/Sidebar.js',
        )
        self.assertTrue(sidebar_src, "Sidebar.js not found")
        # 找 name 為 _t("Projects") 的 nav item（可譯形；裸字面量不算數）
        self.assertRegex(
            sidebar_src,
            r"""name\s*:\s*_t\(\s*['"]Projects['"]\s*\)""",
            "AC #21: Sidebar.js must declare a nav item with "
            'name: _t("Projects") —— 必須是 _t() 可譯形，裸字面 '
            'name: "Projects" 不算數。Open Q4（保留英文字面）已被 #107 '
            'Q2(a) 取代（2026-08-26 Eugene 裁，載體＝#908 留言）：解包回'
            '裸字面會讓 babel jslexer 抽不到這條，翻譯靜默消失。',
        )


class TestCreateProjectModal(TransactionCase):
    """AC #22, #23 — modal allows unbound, drops the `has_project` filter."""

    def test_ac22_modal_offers_no_service_option(self):
        """AC #22: modal exposes a "No service" first option.

        The implementation may surface this via the OWL template (xml)
        or via a JS-side default; we accept either.  Heuristic: the
        literal "No service" (or zh-tw equivalent) must appear in one
        of the modal asset files.
        """
        js = _AssetReader.read(
            'static/src/paas/components/modal/CreateProjectModal.js',
        )
        xml = _AssetReader.read(
            'static/src/paas/components/modal/CreateProjectModal.xml',
        )
        combined = (js + '\n' + xml).lower()
        self.assertTrue(
            'no service' in combined or 'unbound' in combined,
            "AC #22: CreateProjectModal must expose a 'No service' / "
            "'Unbound' first-class option for unbound project creation",
        )

    def test_ac23_modal_removes_has_project_filter(self):
        """AC #23: the JS filter ``!svc.has_project`` must be gone.

        Currently CreateProjectModal.js lines 71-72 hide services with
        existing projects; the unique constraint server-side is the
        proper enforcement, so the front-end filter must be removed.
        """
        js = _AssetReader.read(
            'static/src/paas/components/modal/CreateProjectModal.js',
        )
        self.assertTrue(js, "CreateProjectModal.js not found")
        # Reject the negation-style filter that hides services
        self.assertNotRegex(
            js,
            r"!\s*svc\.has_project\b|has_project\s*===?\s*false",
            "AC #23: `has_project` client-side filter must be removed "
            "(server-side unique constraint is the source of truth)",
        )

    def test_ac23_modal_no_longer_requires_service_selection(self):
        """AC #23 (paired): submit no longer fails on empty service.

        Current code at onSubmit raises "Please select a cloud service"
        when cloudServiceId is empty — that gate must go away with D5=A.
        """
        js = _AssetReader.read(
            'static/src/paas/components/modal/CreateProjectModal.js',
        )
        self.assertNotIn(
            'Please select a cloud service', js,
            "AC #23: 'Please select a cloud service' gate must be "
            "removed (service is optional after D5=A)",
        )


class TestProjectsListBadge(TransactionCase):
    """AC #24 — Projects list page exposes bound/unbound badge."""

    def test_ac24_projects_page_shows_binding_state(self):
        """AC #24: Projects list renders a Bound/Unbound badge column.

        Heuristic: a literal "Unbound" / "unbound" must appear in either
        the page's JS or XML so the badge has visible text for the user.
        """
        js = _AssetReader.read(
            'static/src/paas/pages/support-projects/SupportProjectsPage.js',
        )
        xml = _AssetReader.read(
            'static/src/paas/pages/support-projects/SupportProjectsPage.xml',
        )
        combined = (js + '\n' + xml).lower()
        self.assertIn(
            'unbound', combined,
            "AC #24: SupportProjectsPage must render 'Unbound' badge "
            "text so users distinguish bound from unbound projects",
        )


class TestTaskFormLinkedServicesPanel(TransactionCase):
    """AC #25 — Task form chatter area exposes Linked Services panel."""

    def test_ac25_task_form_xml_declares_extras_panel(self):
        """AC #25: project_task_views.xml renders ``extra_cloud_service_ids``.

        Implementation chooses the exact widget; the contract is that
        the field is bound somewhere in the form so users can add /
        remove extras.  We also assert ``primary_cloud_service_id`` is
        rendered (read-only chip).
        """
        view_xml = _AssetReader.read('views/project_task_views.xml')
        self.assertTrue(view_xml, "project_task_views.xml not found")
        self.assertIn(
            'extra_cloud_service_ids', view_xml,
            "AC #25: task form must surface extra_cloud_service_ids "
            "(Linked Services panel)",
        )
        self.assertIn(
            'primary_cloud_service_id', view_xml,
            "AC #25: task form must surface primary_cloud_service_id "
            "(readonly chip in Linked Services panel)",
        )


class TestServiceDetailTabRoute(TransactionCase):
    """#746 — service-detail 的 tab 白名單與實際頁籤必須 lock-step。

    為什麼不是「斷言字串 access 有出現在 router.js」：那種寫法只擋得住「這一次
    忘了加」，擋不住下一次。真正的失效形狀是**兩份清單漂移**，而且兩個方向都會
    出事、症狀還不一樣：

    * regex 少一個頁籤 ⇒ 該頁籤的深連結與重新整理掉到 NotFoundPage（#746 就是
      這一邊：Access 頁籤 setTab 會改 hash，改完重整就進不去）。
    * regex 多一個頁籤 ⇒ 路由收得下，但頁面沒有對應的 t-if，租戶看到一個空白
      的分頁內容區。

    所以判準是**集合相等**，不是「包含某個字」。TestRouterAlias 蓋不到這裡——
    覆核實測：把 access 從 regex 拿掉，那個類別照樣全綠。
    """

    _TAB_ROUTE_RE = re.compile(
        r"routeName:\s*\"service-detail\".*?pattern:\s*/\^workspace"
        r".*?\(([a-z0-9|\-]+)\)\$/",
    )

    def _router_tabs(self):
        src = _AssetReader.read('static/src/paas/core/router.js')
        self.assertTrue(src, "router.js not found")
        # 帶 :tab 的那一條（另一條 service-detail 沒有 tab 段），取最後一個捕獲組
        for line in src.splitlines():
            if 'Service Detail Tab' not in line:
                continue
            m = self._TAB_ROUTE_RE.search(line)
            self.assertTrue(
                m, "service-detail tab route 的 pattern 形狀變了，"
                   "本測試的抽取式要跟著更新：%s" % line.strip())
            return set(m.group(1).split('|'))
        self.fail("router.js 沒有 'Service Detail Tab' 這一條 route")

    def _rendered_tabs(self):
        xml = _AssetReader.read(
            'static/src/paas/pages/service/ServiceDetailPage.xml')
        self.assertTrue(xml, "ServiceDetailPage.xml not found")
        return set(re.findall(r"setTab\(\s*'([a-z0-9\-]+)'\s*\)", xml))

    def test_tab_regex_matches_rendered_tabs(self):
        router_tabs = self._router_tabs()
        rendered = self._rendered_tabs()
        self.assertEqual(
            router_tabs, rendered,
            "service-detail 的 tab 白名單與頁面實際渲染的頁籤不一致。"
            "router.js 少的那些，深連結／重新整理會掉到 NotFoundPage；"
            "多的那些，路由收得下但頁面沒有對應內容。"
            "router=%s rendered=%s" % (sorted(router_tabs), sorted(rendered)))

    def test_access_tab_is_routable(self):
        """#746 的具體那一格：Access 頁籤兩邊都要有。

        與上一條刻意重疊——集合相等在「兩邊同時漏掉 access」時會是綠的
        （那正是本票落地前的狀態），這條把該格釘死。
        """
        self.assertIn(
            'access', self._router_tabs(),
            "router.js 的 service-detail tab 白名單少了 access（#746）")
        self.assertIn(
            'access', self._rendered_tabs(),
            "ServiceDetailPage.xml 沒有 Access 頁籤（#746）")

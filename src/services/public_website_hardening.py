"""Deterministic patches for the production website's database-backed views.

The public website was authored through Odoo's website editor, so these views do
not live in this addon as XML data.  Keep the transformation pure and strict:
the release migration may update the known view keys, while tests can exercise
the exact transformation without connecting to an Odoo database.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping


GLOBAL_VIEW_KEY = "website.wt_global_dark_mode_css"
NAVBAR_VIEW_KEY = "website.wt_navbar_theme_controls"
HOMEPAGE_VIEW_KEY = "website.homepage"

_MOBILE_CSS_MARKER = "WOOW #2004 mobile row overflow"
_MOBILE_META_SELECTOR = "meta[name=mobile-web-app-capable]"


class WebsiteViewDrift(RuntimeError):
    """The live view no longer has the shape this migration was reviewed for."""


def _replace_exact(
    value: str,
    old: str,
    new: str,
    expected: int,
    *,
    label: str,
) -> str:
    """Replace an exact reviewed occurrence count and remain idempotent."""
    old_count = value.count(old)
    new_count = value.count(new)
    if old_count == expected and new_count == 0:
        return value.replace(old, new)
    if old_count == 0 and new_count == expected:
        return value
    raise WebsiteViewDrift(
        f"{label}: expected old/new counts {expected}/0 or 0/{expected}; "
        f"got {old_count}/{new_count}"
    )


def patch_global_view(arch: str) -> str:
    """Fix blob-manifest URLs, the modern PWA meta, and two mobile rows."""
    patched = _replace_exact(
        arch,
        '"start_url":"/"',
        '"start_url":window.location.origin+"/"',
        1,
        label="manifest start_url",
    )
    patched = _replace_exact(
        patched,
        '{"src":"/web/image/website/1/logo"',
        '{"src":window.location.origin+"/web/image/website/1/logo"',
        2,
        label="manifest icon URLs",
    )

    mobile_meta_block = (
        'if(!document.querySelector("meta[name=mobile-web-app-capable]")){\n'
        'var mw=document.createElement("meta");mw.name="mobile-web-app-capable";\n'
        'mw.content="yes";document.head.appendChild(mw);}\n\n'
    )
    meta_count = patched.count(_MOBILE_META_SELECTOR)
    if meta_count == 0:
        apple_anchor = (
            'if(!document.querySelector('
            '"meta[name=apple-mobile-web-app-capable]")){'
        )
        if patched.count(apple_anchor) != 1:
            raise WebsiteViewDrift(
                "mobile PWA meta: reviewed Apple meta anchor is missing or duplicated"
            )
        patched = patched.replace(apple_anchor, mobile_meta_block + apple_anchor, 1)
    elif meta_count != 1:
        raise WebsiteViewDrift(
            f"mobile PWA meta: expected zero or one selector, got {meta_count}"
        )

    mobile_css = (
        "\n/* WOOW #2004 mobile row overflow */\n"
        "@media (max-width: 991.98px) {\n"
        '  section[data-name="Columns and Flash"] &gt; .container &gt; .row.g-5,\n'
        '  section[data-name="Latest Articles"] &gt; .container &gt; .row.g-5 {\n'
        "    margin-left: 0 !important;\n"
        "    margin-right: 0 !important;\n"
        "  }\n"
        "}\n"
    )
    marker_count = patched.count(_MOBILE_CSS_MARKER)
    if marker_count == 0:
        if patched.count("</style>") != 1:
            raise WebsiteViewDrift(
                "mobile overflow CSS: expected exactly one closing style tag"
            )
        patched = patched.replace("</style>", mobile_css + "</style>", 1)
    elif marker_count != 1:
        raise WebsiteViewDrift(
            f"mobile overflow CSS: expected zero or one marker, got {marker_count}"
        )
    return patched


def patch_navbar_view(arch: str) -> str:
    """Remove the ID duplicated when the inherited selector matches both navs."""
    return _replace_exact(
        arch,
        ' id="wtNavThemeToggle"',
        ' data-wt-theme-toggle="nav"',
        1,
        label="navbar theme toggle",
    )


def _patch_named_section(
    arch: str,
    section_name: str,
    patcher: Callable[[str], str],
) -> str:
    marker = f'data-name="{section_name}"'
    if arch.count(marker) != 1:
        raise WebsiteViewDrift(
            f"section {section_name!r}: expected exactly one data-name marker"
        )
    marker_at = arch.index(marker)
    start = arch.rfind("<section", 0, marker_at)
    end = arch.find("</section>", marker_at)
    if start < 0 or end < 0:
        raise WebsiteViewDrift(f"section {section_name!r}: boundaries not found")
    end += len("</section>")
    section = arch[start:end]
    return arch[:start] + patcher(section) + arch[end:]


def patch_homepage_view(arch: str) -> str:
    """Repair only the two heading-level skips reproduced in Chrome."""

    def patch_hero(section: str) -> str:
        section = _replace_exact(
            section,
            '<h3 class="mb-1">',
            '<h2 class="mb-1">',
            3,
            label="Hero card heading openings",
        )
        return _replace_exact(
            section,
            "</h3>",
            "</h2>",
            3,
            label="Hero card heading closings",
        )

    def patch_columns(section: str) -> str:
        section = _replace_exact(
            section,
            "<h4",
            "<h3",
            6,
            label="Columns and Flash heading openings",
        )
        return _replace_exact(
            section,
            "</h4>",
            "</h3>",
            6,
            label="Columns and Flash heading closings",
        )

    patched = _patch_named_section(arch, "Hero", patch_hero)
    return _patch_named_section(patched, "Columns and Flash", patch_columns)


PATCHERS: Mapping[str, Callable[[str], str]] = {
    GLOBAL_VIEW_KEY: patch_global_view,
    NAVBAR_VIEW_KEY: patch_navbar_view,
    HOMEPAGE_VIEW_KEY: patch_homepage_view,
}


def patch_arch_db(
    arch_db: Mapping[str, str],
    patcher: Callable[[str], str],
) -> tuple[dict[str, str], int]:
    """Patch every stored language without creating or dropping translations."""
    if not isinstance(arch_db, Mapping) or not arch_db:
        raise WebsiteViewDrift("arch_db must be a non-empty language mapping")
    patched: dict[str, str] = {}
    changed = 0
    for language, arch in arch_db.items():
        if not isinstance(language, str) or not isinstance(arch, str):
            raise WebsiteViewDrift("arch_db contains a non-string language or value")
        new_arch = patcher(arch)
        patched[language] = new_arch
        changed += int(new_arch != arch)
    return patched, changed

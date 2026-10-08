# -*- coding: utf-8 -*-
"""雲端連線服務 manifest → 平台 seed 生成器（Wave B；#375 三型 MCP；#380 Open Design
完整應用；#765 Hermes Agent／HA 完整版／Hermes MCP Server 席數變體）。

（原本這行寫「222 manifest」——manifest 列數會長，不寫進 docstring。）

單一事實源＝docs/plans/2026-Q3/2026-07-19-cloud-services-220-manifest.md。
改價＝改 manifest 後重跑本腳本；輸出冪等（同輸入 byte-identical）。
"""
import re
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape


@dataclass(frozen=True)
class Item:
    no: int
    name: str
    price: int
    kind: str


@dataclass(frozen=True)
class Variant:
    family: str      # odoo | n8n | sa | sh | mcp | opendesign
                     # | hermes_agent | ha_full | mcp_hermes
    tier_key: str    # odoo: t1/t2/t3；n8n: t1..t4；sa: r1/r3/r6/r10；sh: p1/p2；mcp: odoo/manage/n8n
                     # mcp_hermes: t1/t2/t3；hermes_agent／ha_full: app（單級距）
    n: int           # AC 席數（base 列＝0）
    cycle: str       # month | year
    price: int
    name: str = ''   # manifest 原名（單一事實源；classify 從 Item.name 帶入）


_ROW = re.compile(r"\|\s*(\d+)\s*\|\s*(.+?)\s*\|\s*([\d,]+)\s*\|\s*(\S+)\s*\|")

_FAMILY_RULES = [
    ('Odoo訂閱服務', 'odoo', {'（1-3人）': 't1', '（4-7人）': 't2', '（8-11人）': 't3'}),
    ('n8n雲端訂閱服務', 'n8n', {'（1人）': 't1', '（3人）': 't2', '（6人）': 't3', '（10人）': 't4'}),
    ('遠端安全連結訂閱服務', 'sa', {'（1個）': 'r1', '（3個）': 'r3', '（6個）': 'r6', '（10個）': 'r10'}),
    ('智慧家庭連結訂閱服務', 'sh', {'（方案一：對外網址）': 'p1',
                                    '（方案二：對外網址+MCP加密連線）': 'p2'}),
    ('ODOO MCP 標準版', 'mcp', {'': 'odoo'}),
    ('ODOO MCP 管理控制台', 'mcp', {'': 'manage'}),
    ('n8n MCP 工具包', 'mcp', {'': 'n8n'}),
    ('Kubernetes MCP 管理', 'mcp', {'': 'k8s'}),
    ('Vibe Kanban MCP', 'mcp', {'': 'vk'}),
    ('Open Design MCP', 'mcp', {'': 'opendesign'}),
    # #380 Open Design 完整應用（daemon+console）：獨立 family，prefix 與「Open Design
    # MCP」不互為前綴（第 12 字元起「雲端」vs「MCP」）→ startswith 不誤配。
    ('Open Design 雲端訂閱服務', 'opendesign', {'': 'app'}),
    # #765 三個新開加購的家族。各自獨立 family（不併進 'mcp'）的理由：
    #   - hermes_agent／ha_full 是完整應用、單級距，與 MCP 家族無共用屬性。
    #   - mcp_hermes 的年繳基底是月×10（家族異例，其餘家族月×8）——併進 'mcp'
    #     會讓「年繳倍率」這條機械公式在同一 family 內出現兩個值，守線只能改寫成
    #     per-tier 例外表；獨立 family 讓例外留在 family 這一格、可被一眼看見。
    # ⚠ 'Hermes Agent 雲端訂閱服務' 與 'Hermes MCP Server' 不互為前綴（第 8 字元
    #   起「Agent」vs「MCP」）→ startswith 不誤配；兩者是不同商品，勿互掛
    #   （守線＝src/tests/test_hermes_agent_seed_424.py 的
    #   test_distinct_from_mcp_hermes_plans）。
    ('Hermes Agent 雲端訂閱服務', 'hermes_agent', {'': 'app'}),
    ('Home Assistant 完整版 雲端訂閱服務', 'ha_full', {'': 'app'}),
    # ⚠ prefix 的**結尾空格是刻意的**：classify 用 name[len(prefix):] 切 rest，
    #   而本家族的商品名在「Server」與「T1」之間有空格（既有 base plan 名稱如此），
    #   prefix 少了它 rest 就變成 ' T1（…）…'、marker startswith 全部落空。
    #   刪掉它不會靜默錯——classify 會在產出前 raise「無法判 tier」。
    ('Hermes MCP Server ', 'mcp_hermes', {'T1（1–3 人）': 't1',
                                          'T2（3–6 人）': 't2',
                                          'T3（6–10 人）': 't3'}),
]

_VAR = re.compile(r"\+(\d+)access(月|年)繳$")
_BASE = re.compile(r"(月|年)繳$")


def display_name(name):
    """manifest 原名 → seed 寫進 DB 的方案名（2026-10-08 中文化，使用者核可）。

    manifest 仍維持「+Naccess月繳」與「Hermes MCP Server」原格式——classify 靠
    `_VAR` 與 `_FAMILY_RULES` 的 prefix 解析席數／週期／家族，改 manifest 會連帶改判準。
    只在輸出那一刻轉成畫面用字：「+N 存取席位 月繳」、「MCP 伺服器」。
    既有 DB 由 migrations/18.0.1.0.214 以同一條規則補改。
    """
    name = _VAR.sub(lambda m: f'+{m.group(1)} 存取席位 {m.group(2)}繳', name)
    return name.replace('MCP Server', 'MCP 伺服器')


def parse_manifest(path):
    items = []
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        m = _ROW.match(line)
        if m:
            items.append(Item(no=int(m.group(1)), name=m.group(2),
                              price=int(m.group(3).replace(',', '')),
                              kind=m.group(4)))
    return items


def classify(item):
    for prefix, family, tiers in _FAMILY_RULES:
        if item.name.startswith(prefix):
            rest = item.name[len(prefix):]
            tier_key = next((t for marker, t in tiers.items()
                             if marker and rest.startswith(marker)), None)
            if tier_key is None and '' in tiers:
                tier_key = tiers['']
            if tier_key is None:
                raise ValueError(f'無法判 tier：{item.name}')
            mv = _VAR.search(item.name)
            if mv:
                return Variant(family, tier_key, int(mv.group(1)),
                               'month' if mv.group(2) == '月' else 'year',
                               item.price, item.name)
            mb = _BASE.search(item.name)
            if mb:
                return Variant(family, tier_key, 0,
                               'month' if mb.group(1) == '月' else 'year',
                               item.price, item.name)
            raise ValueError(f'無法判週期：{item.name}')
    raise ValueError(f'未知家族：{item.name}')


# ─────────────────────────────────────────────────────────────────────────
# 權威對映表（generator 內建；plan「權威對映表」章的機器化落地）
# 價格數字全部來自 manifest（Variant.price）；本區只放平台屬性與繼承常數。
# ─────────────────────────────────────────────────────────────────────────

# (family, tier_key) → base plan xmlid 字首（去 _monthly/_yearly）
TIER_BASE_XMLID = {
    ('odoo', 't1'): 'plan_odoo',
    ('odoo', 't2'): 'plan_odoo_t2',
    ('odoo', 't3'): 'plan_odoo_t3',
    ('n8n', 't1'): 'plan_n8n',
    ('n8n', 't2'): 'plan_n8n_t2',
    ('n8n', 't3'): 'plan_n8n_t3',
    ('n8n', 't4'): 'plan_n8n_t4',
    ('sa', 'r1'): 'plan_security_access_r1',
    ('sa', 'r3'): 'plan_security_access_r3',
    ('sa', 'r6'): 'plan_security_access_entry',   # r6 = entry（6 組）
    ('sa', 'r10'): 'plan_security_access_pro',    # r10 = pro（10 組）
    ('sh', 'p1'): 'plan_smarthome',
    ('sh', 'p2'): 'plan_smarthome_mcp',
    ('mcp', 'odoo'): 'plan_mcp_odoo',
    ('mcp', 'manage'): 'plan_mcp_odoo_manage',
    ('mcp', 'n8n'): 'plan_mcp_n8n',
    ('mcp', 'k8s'): 'plan_mcp_k8s',
    ('mcp', 'vk'): 'plan_mcp_vk',
    ('mcp', 'opendesign'): 'plan_mcp_opendesign',
    # #380 Open Design 完整應用（≠ mcp opendesign；base xmlid plan_opendesign）
    ('opendesign', 'app'): 'plan_opendesign',
    # #765：Hermes Agent 完整應用 ≠ Hermes MCP Server（plan_hermes_agent_* vs
    # plan_mcp_hermes_*），兩組 xmlid 前綴不互為前綴，變體不會跨掛。
    ('hermes_agent', 'app'): 'plan_hermes_agent',
    ('ha_full', 'app'): 'plan_ha_full',
    ('mcp_hermes', 't1'): 'plan_mcp_hermes',
    ('mcp_hermes', 't2'): 'plan_mcp_hermes_t2',
    ('mcp_hermes', 't3'): 'plan_mcp_hermes_t3',
}

# (family, tier_key) → CS 規格（odoo/n8n/mcp_hermes）；＝同 tier base plan 的 cs_* 值
# （與 PR-B2 後 seed 同值——n8n t4 為 t3 外推一級）
#
# ⚠ 沒列進本表的 family 產出的變體就**不帶 cs_***，這是刻意的：hermes_agent／
# ha_full／opendesign 的 base plan 也刻意不設 cs_*（那三欄非空會讓
# `_allocated_vals_from` 改吃 plan 值、蓋掉 template 的 min_*＝quota 峰值模型，
# 症狀是 pod 卡 exceeded quota 而 UI 照顯 Running，見 #456）。變體必須與同 tier
# base 一致，故「base 沒設 → 變體也不能設」。反之 mcp_hermes 的 base 有 cs_*
# （#431 三階規格），變體漏設會讓三階全部 fallback 到同一組 template.min_*
# ——那正是 #431 要解的病灶原地復發。
CS_SPECS = {
    ('odoo', 't1'): dict(cs_vcpu=2, cs_ram_gb=2.0, cs_storage_gb=20, cs_odoo_workers=2),
    ('odoo', 't2'): dict(cs_vcpu=2, cs_ram_gb=4.0, cs_storage_gb=30, cs_odoo_workers=3),
    ('odoo', 't3'): dict(cs_vcpu=3, cs_ram_gb=4.0, cs_storage_gb=40, cs_odoo_workers=4),
    ('n8n', 't1'): dict(cs_vcpu=1, cs_ram_gb=1.0, cs_storage_gb=10, cs_odoo_workers=0),
    ('n8n', 't2'): dict(cs_vcpu=1, cs_ram_gb=2.0, cs_storage_gb=20, cs_odoo_workers=0),
    ('n8n', 't3'): dict(cs_vcpu=2, cs_ram_gb=2.0, cs_storage_gb=30, cs_odoo_workers=0),
    ('n8n', 't4'): dict(cs_vcpu=2, cs_ram_gb=4.0, cs_storage_gb=40, cs_odoo_workers=0),
    # #765：mcp_hermes 三階＝ #431 拍板的 base cs_*（同價位對齊 n8n；
    # cs_odoo_workers 一律 0，非 Odoo 服務）
    ('mcp_hermes', 't1'): dict(cs_vcpu=1, cs_ram_gb=2.0, cs_storage_gb=20, cs_odoo_workers=0),
    ('mcp_hermes', 't2'): dict(cs_vcpu=2, cs_ram_gb=2.0, cs_storage_gb=30, cs_odoo_workers=0),
    ('mcp_hermes', 't3'): dict(cs_vcpu=2, cs_ram_gb=4.0, cs_storage_gb=40, cs_odoo_workers=0),
}

# (family, tier_key) → SA quota；＝同級 SA base 的 sa_* 值
SA_QUOTA = {
    ('sa', 'r1'): dict(sa_max_routes=1, sa_max_access_policies=1,
                       sa_max_custom_domains=1, sa_max_email_rules=0,
                       sa_max_emails_per_policy=20),
    ('sa', 'r3'): dict(sa_max_routes=3, sa_max_access_policies=3,
                       sa_max_custom_domains=1, sa_max_email_rules=0,
                       sa_max_emails_per_policy=20),
    ('sa', 'r6'): dict(sa_max_routes=6, sa_max_access_policies=6,
                       sa_max_custom_domains=1, sa_max_email_rules=0,
                       sa_max_emails_per_policy=20),
    ('sa', 'r10'): dict(sa_max_routes=10, sa_max_access_policies=10,
                        sa_max_custom_domains=3, sa_max_email_rules=10,
                        sa_max_emails_per_policy=20),
}

# family → service_type
FAMILY_SERVICE_TYPE = {
    'odoo': 'cloud_service', 'n8n': 'cloud_service', 'mcp': 'cloud_service',
    'opendesign': 'cloud_service',
    'hermes_agent': 'cloud_service', 'ha_full': 'cloud_service',
    'mcp_hermes': 'cloud_service',
    'sa': 'security_access', 'sh': 'smart_home',
}

# M2M template 落地順序（links 檔內 record 順序；template 內變體照 manifest 序）
# ⚠ 新增家族時**必須**同步在此登記：emit_links_xml 只走本清單，漏登記的 template
# 會讓該家族的變體 plan 建出來卻沒有任何 (4,) append——seed 載入不報錯、
# repo-lint 的 variant_plans 計數照樣長，只有前台的席數區塊靜默不出現。
# `_assert_templates_registered` 把這個靜默丟棄轉成 ValueError（產出前就炸）。
_TEMPLATE_ORDER = [
    'cloud_app_odoo', 'cloud_app_n8n', 'cloud_app_mcp_odoo',
    'cloud_app_mcp_odoo_manage', 'cloud_app_mcp_n8n',
    'cloud_app_mcp_k8s', 'cloud_app_mcp_vk', 'cloud_app_mcp_opendesign',
    'cloud_app_opendesign',
    # #765
    'cloud_app_hermes_agent', 'cloud_app_ha_full', 'cloud_app_ha_full_apporo',
    'cloud_app_mcp_hermes',
    'smart_home_home_assistant', 'security_access_default',
]

_CYCLE_SUFFIX = {'month': 'monthly', 'year': 'yearly'}

_GEN_HEADER_XML = ('<!-- GENERATED by scripts/seed/generate_plan_variants.py'
                   ' — DO NOT EDIT BY HAND -->')
_GEN_HEADER_PY = ('# GENERATED by scripts/seed/generate_plan_variants.py'
                  ' — DO NOT EDIT BY HAND')


def _plan_xmlid(v):
    return f'{TIER_BASE_XMLID[(v.family, v.tier_key)]}_ac{v.n}_{_CYCLE_SUFFIX[v.cycle]}'


def _base_plan_xmlid(v):
    return f'{TIER_BASE_XMLID[(v.family, v.tier_key)]}_{_CYCLE_SUFFIX[v.cycle]}'


def _product_xmlid(v):
    return 'product_' + _plan_xmlid(v)[len('plan_'):]


def _template_xmlid(v):
    if v.family == 'mcp':
        return {'odoo': 'cloud_app_mcp_odoo', 'manage': 'cloud_app_mcp_odoo_manage',
                'n8n': 'cloud_app_mcp_n8n', 'k8s': 'cloud_app_mcp_k8s',
                'vk': 'cloud_app_mcp_vk',
                'opendesign': 'cloud_app_mcp_opendesign'}[v.tier_key]
    return {'odoo': 'cloud_app_odoo', 'n8n': 'cloud_app_n8n',
            'sa': 'security_access_default',
            'sh': 'smart_home_home_assistant',
            'opendesign': 'cloud_app_opendesign',
            # #765：mcp_hermes 三階同掛一筆 template（tier 差異在 plan 層）
            'hermes_agent': 'cloud_app_hermes_agent',
            'ha_full': 'cloud_app_ha_full',
            'mcp_hermes': 'cloud_app_mcp_hermes'}[v.family]


# 同一組變體要再掛一份到「同產品、不同品牌」的孿生卡片上。
# ha-full 的 APPORO 線與 woowtech 線共用同一組 plan（同價、同規格，見
# subscription_template_links.xml 的註解），base plan 那邊已經是共用的；變體漏了
# 就會出現「base 一樣、加購變體只有其中一張卡有」的半套狀態——
# test_ha_full_apporo_seed 的 test_sealed_or_else_lockstep_with_ha_full 就是守
# 這件事（2026-09-25 封存後改成「解封才生效」，見下）。
# 2026-09-25：cloud_app_ha_full_apporo 已封存（is_active=False，見
# data/cloud_app_templates.xml）。鏡射**刻意保留**——移除它只會讓產生的 XML 少掉
# 那幾列，既有 DB 的 m2m 連結不會因此被刪（seed 移除列不等於刪資料），反而讓
# seed 與 DB 說法不一致；卡片既已封存，這些連結對外沒有任何效力。要真的解封時，
# 鏡射還在＝變體不會漏掉半套。
_MIRROR_TEMPLATES = {
    'cloud_app_ha_full': ['cloud_app_ha_full_apporo'],
}


def _template_model(v):
    if v.family == 'sa':
        return 'woow_paas_platform.security_access_template'
    if v.family == 'sh':
        return 'woow_paas_platform.smart_home_template'
    return 'woow_paas_platform.cloud_app_template'


# ─────────────────────────────────────────────────────────────────────────
# 產出器：variants XML（plans + products）／links XML／生成測試 py
# ─────────────────────────────────────────────────────────────────────────

def _plan_record(v):
    lines = [
        f'        <record id="{_plan_xmlid(v)}" model="sh.subscription.plan">',
        f'            <field name="name">{escape(display_name(v.name))}</field>',
        '            <field name="sh_duration">1</field>',
        f'            <field name="sh_unit">{v.cycle}</field>',
        f'            <field name="sh_plan_price">{v.price}</field>',
        '            <field name="sh_override_product" eval="True"/>',
        '            <field name="sh_no_of_billing_cycle">-1</field>',
        '            <field name="sh_never_expire" eval="True"/>',
        '            <field name="sh_trial" eval="True"/>',
        '            <field name="sh_trial_duration">7</field>',
        '            <field name="sh_trial_unit">day</field>',
        '            <field name="sh_start_immediately" eval="False"/>',
        '            <field name="sh_is_close_by_customer" eval="True"/>',
        '            <field name="sh_reminder"',
        "                   eval=\"[(4, ref('reminder_3days_before')), "
        "(4, ref('reminder_1day_before'))]\"/>",
        f'            <field name="service_type">{FAMILY_SERVICE_TYPE[v.family]}</field>',
        f'            <field name="ac_max_access_seats">{v.n}</field>',
    ]
    cs = CS_SPECS.get((v.family, v.tier_key))
    if cs:
        lines += [
            f'            <field name="cs_vcpu">{cs["cs_vcpu"]}</field>',
            f'            <field name="cs_ram_gb">{float(cs["cs_ram_gb"])}</field>',
            f'            <field name="cs_storage_gb">{cs["cs_storage_gb"]}</field>',
            f'            <field name="cs_odoo_workers">{cs["cs_odoo_workers"]}</field>',
        ]
    sa = SA_QUOTA.get((v.family, v.tier_key))
    if sa:
        lines += [
            f'            <field name="sa_max_routes">{sa["sa_max_routes"]}</field>',
            f'            <field name="sa_max_access_policies">{sa["sa_max_access_policies"]}</field>',
            f'            <field name="sa_max_custom_domains">{sa["sa_max_custom_domains"]}</field>',
            f'            <field name="sa_max_email_rules">{sa["sa_max_email_rules"]}</field>',
            f'            <field name="sa_max_emails_per_policy">{sa["sa_max_emails_per_policy"]}</field>',
        ]
    if v.family == 'sh' and v.tier_key == 'p2':
        lines.append('            <field name="mcp_integration">ha_mcp_tools</field>')
    lines.append('            <field name="storefront_hidden" eval="True"/>')
    lines.append('        </record>')
    return '\n'.join(lines)


def _product_record(v):
    return '\n'.join([
        f'        <record id="{_product_xmlid(v)}" model="product.product">',
        f'            <field name="name">{escape(display_name(v.name))} Subscription</field>',
        '            <field name="type">service</field>',
        f'            <field name="list_price">{v.price}</field>',
        '            <field name="sale_ok" eval="True"/>',
        '            <field name="purchase_ok" eval="False"/>',
        '            <field name="sh_product_subscribe" eval="True"/>',
        f'            <field name="sh_subscription_plan_id" ref="{_plan_xmlid(v)}"/>',
        # #366：pts 交易免稅——明寫空 taxes_id，不繼承 company 預設銷項稅
        '            <field name="taxes_id" eval="[(6, 0, [])]"/>',
        '        </record>',
    ])


def emit_variants_xml(variants):
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        _GEN_HEADER_XML,
        '<odoo>',
        # 計數綁 len(variants)（原為寫死的「216 個」——那正是 #433 那類會腐化的
        # 字面量，且它住在 generator 裡，撞到時重跑 generator 是 no-op）。
        '    <!-- %d 個 AC 席數變體 plan/product。noupdate=1：record 欄位不被'
        % len(variants),
        '         升級覆寫（改值＝改 manifest／對映表後重跑 generator）。全數',
        '         storefront_hidden=True——Wave C picker 落地前不上前台。 -->',
        '    <data noupdate="1">',
        '',
    ]
    for v in variants:
        parts.append(_plan_record(v))
        parts.append('')
    for v in variants:
        parts.append(_product_record(v))
        parts.append('')
    parts.append('    </data>')
    parts.append('</odoo>')
    return '\n'.join(parts) + '\n'


def _assert_templates_registered(groups):
    """有變體卻沒登記在 _TEMPLATE_ORDER 的 template → 硬失敗（禁靜默丟棄）。

    emit_links_xml 以 _TEMPLATE_ORDER 決定輸出順序並跳過不在清單內的 key，
    所以漏登記的結果不是「順序怪」而是「整個家族的 (4,) append 消失」，而
    plan/product 側照樣產出＝seed 載入全綠、拓撲計數照長、只有前台不顯示。
    """
    missing = [tpl for tpl in groups if tpl not in _TEMPLATE_ORDER]
    if missing:
        raise ValueError(
            '這些 template 有變體但沒登記在 _TEMPLATE_ORDER，links 會靜默漏掉：%s'
            % sorted(missing))


def emit_links_xml(variants):
    groups = {}
    for v in variants:
        tpl = _template_xmlid(v)
        for target in (tpl, *_MIRROR_TEMPLATES.get(tpl, ())):
            groups.setdefault(target, (_template_model(v), []))[1].append(
                _plan_xmlid(v))
    _assert_templates_registered(groups)
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        _GEN_HEADER_XML,
        '<odoo>',
        '    <!-- 變體 M2M links。noupdate=0 鐵律：升級時 base links 檔會用整批',
        '         reset 重寫 template plan 集，本檔的 (4,) append 必須每次 -u 跟著',
        '         重跑疊回變體——故必 noupdate=0，且本檔必排在',
        '         subscription_template_links.xml 之後載入（manifest data 順序）。 -->',
        '    <data noupdate="0">',
    ]
    for tpl in _TEMPLATE_ORDER:
        if tpl not in groups:
            continue
        model, plan_ids = groups[tpl]
        indent = ' ' * 26
        eval_body = (',\n' + indent).join(
            f"(4, ref('{p}'))" for p in plan_ids)
        parts += [
            '',
            f'        <record id="{tpl}" model="{model}">',
            '            <field name="subscription_plan_ids"',
            f'                   eval="[{eval_body}]"/>',
            '        </record>',
        ]
    parts.append('')
    parts.append('    </data>')
    parts.append('</odoo>')
    return '\n'.join(parts) + '\n'


_TESTS_TEMPLATE = '''# -*- coding: utf-8 -*-
__GEN_HEADER__
"""AC 席數變體 seed 全量斷言（Wave B PR-B3；#375 三型 MCP；#380 Open Design 完整應用）。

改本檔＝改 manifest／對映表後重跑 scripts/seed/generate_plan_variants.py。
每筆斷言：價格／席數／前台隱藏／service_type／cs_* 或 sa quota == 同 tier base／
M2M 掛載於對應 template。另：DB 的席數變體集合 == VARIANTS、前台不滲漏冒煙。

#433：本檔曾硬編 216 與 267 兩個計數，而它們寫死在 generator 的
_TESTS_TEMPLATE 字面量裡（267 裡的 base 51 更在 generator 的輸入域外）——
撞紅的人照上面那行指示重跑 generator 是 no-op，真正要改的是 generator 樣板，
而改產出檔又會被下次 regenerate 蓋掉。現已全部改綁 len(VARIANTS)／集合等值，
樣板內零計數字面量。base 側的守線在 src/tests/test_plan_group_keys_368.py
（對 subscription_template_links.xml 集合等值）與 scripts/audit/repo-lint.py
的 check_seed_topology。
"""
from odoo.tests.common import TransactionCase, tagged

MOD = 'woow_paas_platform'

VARIANTS = [
# __ENTRIES__
]


@tagged('post_install', '-at_install')
class TestPlanVariantsGenerated(TransactionCase):

    def test_all_variants(self):
        for e in VARIANTS:
            xmlid = e['xmlid']
            plan = self.env.ref('%s.%s' % (MOD, xmlid))
            self.assertEqual(plan.sh_plan_price, e['price'], xmlid)
            self.assertEqual(plan.ac_max_access_seats, e['seats'], xmlid)
            self.assertTrue(plan.storefront_hidden, xmlid)
            self.assertEqual(plan.service_type, e['service_type'], xmlid)
            base = self.env.ref('%s.%s' % (MOD, e['base']))
            if e['has_cs']:
                self.assertEqual(
                    (plan.cs_vcpu, plan.cs_ram_gb, plan.cs_storage_gb,
                     plan.cs_odoo_workers),
                    (base.cs_vcpu, base.cs_ram_gb, base.cs_storage_gb,
                     base.cs_odoo_workers), xmlid)
            if e['has_sa']:
                self.assertEqual(
                    (plan.sa_max_routes, plan.sa_max_access_policies,
                     plan.sa_max_custom_domains, plan.sa_max_email_rules,
                     plan.sa_max_emails_per_policy),
                    (base.sa_max_routes, base.sa_max_access_policies,
                     base.sa_max_custom_domains, base.sa_max_email_rules,
                     base.sa_max_emails_per_policy), xmlid)
            product = self.env.ref(
                '%s.%s' % (MOD, xmlid.replace('plan_', 'product_', 1)))
            self.assertEqual(product.list_price, e['price'], xmlid)
            template = self.env.ref('%s.%s' % (MOD, e['template']))
            self.assertIn(plan, template.sudo().subscription_plan_ids, xmlid)

    def test_plan_corpus_matches_variants(self):
        """DB 的席數變體集合 == VARIANTS（零漏載、零多餘）。

        取代原本的 assertEqual(count, 267)：集合等值不留任何字面量，且比計數
        嚴格——抓得到「手寫一個不在 manifest 的變體」「generator 產出與 seed XML
        不同步」這類「數量剛好相等但成員不同」的漂移。
        """
        self.assertTrue(VARIANTS, '前置：VARIANTS 不得為空')
        rows = self.env['ir.model.data'].sudo().search([
            ('module', '=', MOD), ('model', '=', 'sh.subscription.plan')])
        xmlid_by_res = {row.res_id: row.name for row in rows}

        db_variants = self.env['sh.subscription.plan'].search(
            [('ac_max_access_seats', '>', 0)])
        actual = {xmlid_by_res.get(plan.id) for plan in db_variants}
        self.assertEqual(
            actual, {e['xmlid'] for e in VARIANTS},
            'DB 的席數變體集合應等於 generator 的 VARIANTS'
            '（None 代表該 plan 缺 seed xmlid）')

    def test_variants_never_reach_storefront(self):
        """變體全 hidden：三個 template model 的可見 plan 皆無變體滲漏。"""
        variants = self.env['sh.subscription.plan'].search(
            [('ac_max_access_seats', '>', 0), ('storefront_hidden', '=', True)])
        self.assertEqual(
            len(variants), len(VARIANTS),
            'hidden 席數變體數應等於 VARIANTS 筆數（少了＝有變體被誤設可見）')
        for tpl_model in ('woow_paas_platform.cloud_app_template',
                          'woow_paas_platform.smart_home_template',
                          'woow_paas_platform.security_access_template'):
            for tpl in self.env[tpl_model].sudo().search([]):
                visible = tpl.subscription_plan_ids.filtered(
                    lambda p: not p.storefront_hidden)
                self.assertFalse(
                    visible.filtered(lambda p: p.ac_max_access_seats > 0),
                    '%s 有變體滲漏前台' % tpl.display_name)
'''


def emit_tests_py(variants):
    entries = []
    for v in variants:
        entries.append(
            "    {'xmlid': %r, 'price': %d, 'seats': %d, 'service_type': %r, "
            "'base': %r, 'template': %r, 'has_cs': %s, 'has_sa': %s},"
            % (_plan_xmlid(v), v.price, v.n, FAMILY_SERVICE_TYPE[v.family],
               _base_plan_xmlid(v), _template_xmlid(v),
               (v.family, v.tier_key) in CS_SPECS,
               (v.family, v.tier_key) in SA_QUOTA))
    return (_TESTS_TEMPLATE
            .replace('__GEN_HEADER__', _GEN_HEADER_PY)
            .replace('# __ENTRIES__', '\n'.join(entries)))


def main():
    root = Path(__file__).resolve().parents[2]
    manifest = root / 'docs/plans/2026-Q3/2026-07-19-cloud-services-220-manifest.md'
    variants = [classify(i) for i in parse_manifest(manifest)
                if i.kind == '+access變體']
    outputs = {
        root / 'src/data/subscription_products_variants.xml':
            emit_variants_xml(variants),
        root / 'src/data/subscription_template_links_variants.xml':
            emit_links_xml(variants),
        root / 'src/tests/test_plan_variants_generated.py':
            emit_tests_py(variants),
    }
    for path, content in outputs.items():
        path.write_text(content, encoding='utf-8')
    print(f'{len(variants)} variants → {len(outputs)} files')


if __name__ == '__main__':
    main()

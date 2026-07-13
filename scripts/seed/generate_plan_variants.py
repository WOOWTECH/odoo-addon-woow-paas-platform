# -*- coding: utf-8 -*-
"""214 manifest → 平台 seed 生成器（Wave B）。

單一事實源＝docs/plans/2026-Q3/2026-07-12-cloud-services-214-manifest.md。
改價＝改 manifest 後重跑本腳本；輸出冪等（同輸入 byte-identical）。
"""
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Item:
    no: int
    name: str
    price: int
    kind: str


@dataclass(frozen=True)
class Variant:
    family: str      # odoo | n8n | sa | sh | mcp
    tier_key: str    # odoo: t1/t2/t3；n8n: t1..t4；sa: r1/r3/r6/r10；sh: p1/p2；mcp: odoo/manage/n8n
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
]

_VAR = re.compile(r"\+(\d+)access(月|年)繳$")
_BASE = re.compile(r"(月|年)繳$")


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

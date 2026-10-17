"""X-LC-001-1／-2／-5：應用市集的搜尋比對與「有範本的分類」。

搜尋原本把字串直接放進 ``('name', 'ilike', search) | ('description', 'ilike', search)``：

- ``%``／``_`` 沒有跳脫，是 SQL LIKE 萬用字元 ⇒ 輸入 ``%`` 列出全部 18 張卡（X-LC-001-1）；
- 不比對 ``tags`` ⇒ 卡片上看得到的 ``vscode``、``workflow`` 搜不到自己（X-LC-001-2）。

``tags`` 是 JSON 字串欄位（``["code-server", "vscode", "ide"]``），在 DB 層 ilike 它會連引號、
逗號一起比（搜 ``"`` 就全中），所以比對改在 Python 做：市集範本只有十幾筆，
``name``／``description`` 取目前語言的值、``tags`` 逐個標籤比，一律不分大小寫的子字串比對，
任何字元都只代表它自己。

本模組不 import ORM／controller。
"""

import json
import logging

_logger = logging.getLogger(__name__)

# 與 cloud_app_template.category 的 selection 同序（分類頁籤的顯示順序）。
CATEGORY_ORDER = ('ai_llm', 'automation', 'database', 'analytics', 'devops', 'web',
                  'container', 'mcp')


def parse_tags(raw):
    """``tags`` 欄位（JSON 陣列字串）→ 字串 list；格式不對回空 list。"""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [str(tag) for tag in value if isinstance(tag, (str, int, float))]


def template_matches_search(name, description, tags, needle):
    """``needle`` 是否出現在名稱、說明或任一標籤（不分大小寫、字面比對）。空字串＝全部符合。"""
    needle = (needle or '').strip().casefold()
    if not needle:
        return True
    haystacks = [name or '', description or '', *(tags or [])]
    return any(needle in str(text).casefold() for text in haystacks)


def ordered_categories(categories):
    """去重、丟掉空值，依 ``CATEGORY_ORDER`` 排序；未知代碼排在最後（依字母序）。"""
    present = {c for c in categories if c}
    known = [c for c in CATEGORY_ORDER if c in present]
    unknown = sorted(present - set(CATEGORY_ORDER))
    return known + unknown

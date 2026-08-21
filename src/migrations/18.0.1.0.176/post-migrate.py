"""[#691 驗收 2(c)] 暫時性 no-op migration，只為在真 CI 上驗一格 gate 行為。

驗的是 #691 規則 B：manifest 版號 bump 到 `18.0.1.0.176`，且**本 PR 新增**的
`src/migrations/18.0.1.0.176/` 目錄名等於新版號 ⇒ gate 由 `unexpected-bump`（紅）
翻成 `migration-bump`（綠）。

⚠ 本目錄會在驗收 (d) 還原時整個刪除。若它出現在 merge 後的 develop 上，代表還原漏做
（判準：`git ls-tree origin/develop src/migrations/18.0.1.0.176` 必須是空的）。
"""


def migrate(cr, version):
    """刻意什麼都不做——本檔的用途是「存在」，不是「執行」。"""

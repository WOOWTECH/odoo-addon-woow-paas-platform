from . import controllers
from . import models
from . import wizard
from .hooks import pre_init_hook, post_init_hook, uninstall_hook

# [#691 驗收 2 (a)] 暫時性標記：驗「只動 src/、不 bump 版號 ⇒ gate 綠」。
# 本行會在驗收 (d) 還原時刪除；若它出現在 merge 後的 develop 上，代表還原漏做。

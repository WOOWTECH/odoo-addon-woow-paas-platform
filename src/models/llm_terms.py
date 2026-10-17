"""《AI 模型服務條款》的版本（DESIGN §2.7、§6.4；D10、D15；票 LLM-04）。

開通只需要同意條款（D10）。條款有版本：

* **一份 published**：``/api/llm/terms`` 回它；開通（``/api/llm/account/activate``）與重新同意
  （``/api/llm/account/accept_terms``）只收它的 ``version``。DB 層以 partial unique index 保證同時
  只有一份 published（:meth:`LlmTerms.init`）。
* **發佈**（:meth:`LlmTerms.action_publish`，後台表單的按鈕，只有 system）：草稿 → published，原本的
  published → retired；寫 ``llm_event``（``tos_publish``，等這個交易 commit 之後才寫）。
* **retired 不能刪除、published／retired 不能改**：使用者要能依版本號查到自己同意過的那一份
  （消費者保護法第 13 條；LLM-05 審查第 2 輪）。改條款＝複製成新的草稿再發佈。
* **重新同意的 gate**（:meth:`LlmTerms._llm_reaccept_required`）：帳戶同意過的版本之後，只要發佈過
  ``requires_reaccept=True`` 的版本，建金鑰、轉換、啟用金鑰就回 ``llm_tos_outdated``，直到同意
  目前的版本；既有金鑰不受影響（gate 只擋 PaaS 端的寫入，不碰 new-api 上的 token）。帳戶同意的版本
  不在本表裡（LLM-01b 的常數 ``draft-0``，沒有任何條文）＝沒有同意過任何條文：只要有 published 就擋。
  「之後」以發佈順序 ``publish_seq`` 判斷，不用 ``published_at``——Datetime 只到秒，同一秒發佈兩版
  時分不出先後。
* **草稿文字**（D15）：內容或標題含「草稿，尚未生效」或【待…】標記＝草稿文字（``is_draft_text``）。
  stg 可以發佈草稿文字（畫面會標「草稿，尚未生效」）；**正式站不能**——判準是 new-api 使用者名稱前綴：
  ``newapi_username_prefix`` 是正式站的 ``wp``（沒設也是 ``wp``）＝正式站（DESIGN N14）。

條款內容是資料值，不進 ``.po``（``docs/decisions/patterns/i18n-scope-data-values-vs-code-strings.md``）：
``title``／``content_html`` 都不是 ``translate=True``。``content_html`` 是 ``fields.Html``（寫入時伺服器端
sanitize，``<script>``、事件屬性、``style`` 都會被清掉，``sanitize_overridable=False``——system 也繞不過）；
前端顯示前再經 ``html_sanitize.js`` 的 ``safeHtml()``（DOMPurify）清一次。
"""
import collections
import re
from datetime import timezone

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from ..services import newapi_client as nac
from .billing_incident import _is_odoo_module_uninstall
from .llm_common import (
    ERR_LLM_TOS_UNAVAILABLE,
    LlmError,
    _is_llm_internal,
    _llm_internal,
    _without_llm_internal,
    llm_config,
)

TERMS_MODEL = 'woow_paas_platform.llm_terms'
TERMS_TABLE = 'woow_paas_platform_llm_terms'
EVENT_MODEL = 'woow_paas_platform.llm_event'
#: 版本號：英數開頭，之後英數、``.``、``_``、``-``，最長 64（例 ``2026-10-draft-1``）。前端會原樣送回來。
VERSION_RE = re.compile(r'[0-9A-Za-z][0-9A-Za-z._-]{0,63}', re.ASCII)
#: 草稿文字的標記（LLM-05 草稿開頭的「草稿，尚未生效」與【待法務確認 Qn】等）。
DRAFT_MARKERS = ('草稿，尚未生效', '【待')
#: 只有 :meth:`LlmTerms.action_publish` 能寫的欄位（要內部寫入把手）。
_STATE_FIELDS = frozenset({'state', 'published_at', 'retired_at', 'publish_seq'})
#: published／retired 之後就不能改的欄位（使用者同意的是這些內容）。
_CONTENT_FIELDS = frozenset({'version', 'title', 'content_html', 'requires_reaccept'})
#: 使用者查得到的狀態（草稿不對外）。
_PUBLIC_STATES = ('published', 'retired')

STATE_SELECTION = [
    ('draft', 'Draft'),
    ('published', 'Published'),
    ('retired', 'Retired'),
]

#: 呼叫端（帳戶、controller）用的唯讀快照：不帶 ORM 記錄，也不帶條文內容。
TermsRef = collections.namedtuple(
    'TermsRef', 'id version title published_at publish_seq requires_reaccept is_draft')

_CURRENT = object()


def clean_version(value):
    """前端送來的版本號 → 合法的字串；不是字串、空白或格式不合 ⇒ ``None``。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if VERSION_RE.fullmatch(text) else None


def _has_draft_markers(*texts):
    return any(marker in (text or '') for text in texts for marker in DRAFT_MARKERS)


def _is_production(env):
    """正式站＝new-api 使用者名稱前綴是 ``wp``（沒設也是；DESIGN N14、§2.11）。"""
    return llm_config(env)['prefix'] == nac.PROD_USERNAME_PREFIX


class LlmTerms(models.Model):
    _name = 'woow_paas_platform.llm_terms'
    _description = 'AI Models Terms of Service Version'
    _order = 'publish_seq desc, id desc'
    _rec_name = 'version'

    version = fields.Char(
        required=True, copy=False, index=True,
        help='Unique version label sent back by the client when accepting, e.g. 2026-10-draft-1.')
    title = fields.Char(required=True, help='Shown as the heading of the terms dialog (data value).')
    content_html = fields.Html(
        string='Content', required=True, sanitize=True, sanitize_overridable=False,
        strip_style=True,
        help='The terms text (data value, not translated). Sanitized on write; '
             'sanitized again in the browser before it is shown.')
    state = fields.Selection(
        STATE_SELECTION, required=True, default='draft', readonly=True, index=True, copy=False)
    requires_reaccept = fields.Boolean(
        string='Requires re-acceptance', default=True,
        help='When published, accounts that accepted an earlier version cannot create keys, '
             'convert points or re-enable keys until they accept this version. '
             'Existing keys keep working.')
    published_at = fields.Datetime(readonly=True, copy=False)
    retired_at = fields.Datetime(readonly=True, copy=False)
    publish_seq = fields.Integer(
        string='Publish order', readonly=True, copy=False,
        help='1 for the first version ever published, then +1 for each publication.')
    is_draft_text = fields.Boolean(
        string='Draft text', compute='_compute_is_draft_text', store=True,
        help='The title or content still carries a draft marker (草稿，尚未生效 or 【待…】). '
             'Draft text can be published on staging only.')

    _sql_constraints = [
        ('version_uniq', 'unique(version)', 'Each terms version must be unique.'),
    ]

    def init(self):
        # DESIGN §2.7：同時只有一份 published（並發發佈時輸的那一筆在 DB 層就失敗）。
        self.env.cr.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS woow_llm_terms_one_published '
            "ON %s (state) WHERE state = 'published'" % TERMS_TABLE)
        self.env.cr.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS woow_llm_terms_publish_seq_uniq '
            'ON %s (publish_seq) WHERE publish_seq IS NOT NULL' % TERMS_TABLE)

    @api.depends('title', 'content_html')
    def _compute_is_draft_text(self):
        for record in self:
            record.is_draft_text = _has_draft_markers(record.title, record.content_html)

    @api.constrains('version')
    def _check_version(self):
        for record in self:
            if not VERSION_RE.fullmatch(record.version or ''):
                raise ValidationError(_(
                    'The terms version must start with a letter or digit and contain only '
                    'letters, digits, ".", "_" or "-" (at most 64 characters).'))

    # ------------------------------------------------------------------ #
    #  守衛                                                                #
    # ------------------------------------------------------------------ #
    @api.model_create_multi
    def create(self, vals_list):
        if not _is_llm_internal(self.env):
            for vals in vals_list:
                if set(vals) & (_STATE_FIELDS - {'state'}) or vals.get('state', 'draft') != 'draft':
                    raise AccessError(_('A terms version is always created as a draft.'))
        return _without_llm_internal(super().create(vals_list))

    def write(self, vals):
        if not _is_llm_internal(self.env):
            if set(vals) & _STATE_FIELDS:
                raise AccessError(_('Use Publish to change the state of a terms version.'))
            if set(vals) & _CONTENT_FIELDS and any(r.state != 'draft' for r in self):
                raise UserError(_(
                    'A published or retired terms version cannot be changed. '
                    'Duplicate it as a new draft instead.'))
        return super().write(vals)

    def unlink(self):
        if _is_odoo_module_uninstall(self.env):
            return super().unlink()
        if any(r.state != 'draft' for r in self):
            raise UserError(_(
                'Published or retired terms versions cannot be deleted: users must be able '
                'to look up the version they accepted.'))
        return super().unlink()

    def copy_data(self, default=None):
        default = dict(default or {})
        vals_list = super().copy_data(default=default)
        if 'version' not in default:
            for record, vals in zip(self, vals_list):
                vals['version'] = ('%s-copy' % record.version)[:64]
        return vals_list

    # ------------------------------------------------------------------ #
    #  發佈（後台按鈕）                                                      #
    # ------------------------------------------------------------------ #
    def action_publish(self):
        """草稿 → published；原本的 published → retired；寫 ``tos_publish`` 稽核（commit 之後）。

        只有 system（``check_access('write')``：其他群組對本 model 沒有寫入權）。並發兩個發佈：
        輸的那一筆撞 partial unique index（同時只有一份 published）或 ``publish_seq`` 的 unique
        index，整個交易失敗、什麼都沒改。
        """
        self.check_access('write')
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(_('Only a draft terms version can be published.'))
        if not (self.content_html and re.sub(r'<[^>]*>|\s|&nbsp;', '', str(self.content_html))):
            raise UserError(_('The terms content is empty.'))
        if self.is_draft_text and _is_production(self.env):
            raise UserError(_(
                'Draft terms (marked 草稿，尚未生效 or 【待…】) can only be published on staging. '
                'Production can only publish the finalized text (D15).'))
        Terms = self.sudo()
        current = Terms.search([('state', '=', 'published')])
        last = Terms.search([('publish_seq', '!=', False)], order='publish_seq desc', limit=1)
        now = fields.Datetime.now()
        if current:
            _llm_internal(current).write({'state': 'retired', 'retired_at': now})
            # 先寫出 retired，再寫 published：兩筆 UPDATE 的順序要固定，否則 partial unique index
            # 會在中間那一刻看到兩份 published。
            current.flush_recordset()
        _llm_internal(Terms.browse(self.id)).write({
            'state': 'published',
            'published_at': now,
            'publish_seq': (last.publish_seq or 0) + 1,
        })
        self.flush_recordset()
        self.env[EVENT_MODEL]._llm_record_after_commit(
            'tos_publish', uid=self.env.uid, target_model=TERMS_MODEL, target_id=self.id,
            details={'requires_reaccept': bool(self.requires_reaccept)})
        return True

    # ------------------------------------------------------------------ #
    #  service methods（帳戶、controller 用；私有方法，RPC 叫不到）             #
    # ------------------------------------------------------------------ #
    def _llm_ref(self):
        self.ensure_one()
        return TermsRef(self.id, self.version, self.title, self.published_at, self.publish_seq,
                        bool(self.requires_reaccept), bool(self.is_draft_text))

    @api.model
    def _llm_current(self):
        """目前 published 的版本（:data:`TermsRef`）；沒有就 ``None``。"""
        record = self.sudo().search([('state', '=', 'published')], limit=1)
        return record._llm_ref() if record else None

    @api.model
    def _llm_reaccept_required(self, accepted_version, current=_CURRENT):
        """帳戶同意 ``accepted_version`` 之後，是否還要重新同意才能建金鑰、轉換、啟用金鑰。

        * 沒有 published 版本 ⇒ 不擋（沒有東西可以同意）。
        * 同意的就是目前的版本 ⇒ 不擋。
        * 同意的版本不在本表（``draft-0``、空值）⇒ 擋：沒有同意過任何條文。
        * 其他 ⇒ 該版本之後發佈過任何 ``requires_reaccept=True`` 的版本就擋（中間那一版要求重新同意、
          最新這一版不要求，也還是要擋：使用者沒有同意過中間那一版的實質變更）。
        """
        if current is _CURRENT:
            current = self._llm_current()
        if current is None:
            return False
        if accepted_version == current.version:
            return False
        Terms = self.sudo()
        accepted = Terms.search([('version', '=', accepted_version), ('state', 'in', _PUBLIC_STATES)],
                                limit=1) if accepted_version else Terms.browse()
        if not accepted or not accepted.publish_seq:
            return True
        return bool(Terms.search_count([
            ('state', 'in', _PUBLIC_STATES),
            ('publish_seq', '>', accepted.publish_seq),
            ('requires_reaccept', '=', True),
        ], limit=1))

    @api.model
    def _llm_terms_payload(self, version=None):
        """``/api/llm/terms``：published 版（或依版本號查 published／retired 版；草稿不對外）。"""
        domain = [('state', '=', 'published')]
        if version is not None:
            version = clean_version(version)
            if version is None:
                raise LlmError(ERR_LLM_TOS_UNAVAILABLE)
            domain = [('version', '=', version), ('state', 'in', _PUBLIC_STATES)]
        record = self.sudo().search(domain, limit=1)
        if not record:
            raise LlmError(ERR_LLM_TOS_UNAVAILABLE)
        return {
            'version': record.version,
            'title': record.title,
            'content_html': str(record.content_html or ''),
            'state': record.state,
            'is_draft': bool(record.is_draft_text),
            'requires_reaccept': bool(record.requires_reaccept),
            'published_at': (record.published_at.replace(tzinfo=timezone.utc).isoformat()
                             if record.published_at else None),
        }

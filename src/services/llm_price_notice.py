"""Dormant price-notice draft: pure rendering and evidence checks, never delivery/apply.

Not imported by services/__init__, registered as an Odoo model, or wired to any
route/cron/admin CLI. Evidence and approval references MUST come from a trusted,
transactional adapter, not a request body or operator-edited JSON. That adapter
and the cross-repository pricing write interlock are intentionally not shipped.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

SEVEN_DAYS = 7 * 24 * 60 * 60
_TITLE = 'AI 模型價格調整公告'
_MODEL = re.compile(r'[A-Za-z0-9_.:/+-]{1,200}\Z')
_REF = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,119}\Z')
_RECIPIENT = re.compile(r'(?:paas|legacy):[A-Za-z0-9_-]{1,64}\Z')


class NoticeBlocked(ValueError):
    """Fixed labels only; never reflect addresses, source options or response bodies."""


def _need(value, label):
    if not value:
        raise NoticeBlocked(label)


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'))


def _hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _time(value):
    _need(type(value) is int and 0 < value <= 4102444800, 'notice_time')
    return value


def _ref(value):
    return type(value) is str and _REF.fullmatch(value) is not None


def _audience(values):
    _need(type(values) is tuple and 0 < len(values) <= 100000, 'notice_audience')
    _need(all(type(v) is str and _RECIPIENT.fullmatch(v) for v in values), 'notice_audience')
    _need(len(values) == len(set(values)), 'notice_audience')
    return tuple(sorted(values))


def _amount(value):
    _need(type(value) is str and 0 < len(value) <= 64, 'notice_price')
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise NoticeBlocked('notice_price') from None
    _need(number.is_finite() and number >= 0 and len(number.as_tuple().digits) <= 24
          and -64 <= number.as_tuple().exponent <= 64
          and (not number or -18 <= number.adjusted() <= 12), 'notice_price')
    if not number:
        return '0'
    text = format(number, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _catalog(value):
    """Effective points rates, AFTER all multipliers; never compare raw ratios alone.

    Only comparable default-group ratio/fixed models are covered by this draft.
    Tiering, unknown cache prices, group changes and model-set changes fail closed.
    """
    _need(type(value) is dict and set(value) == {'default'}, 'notice_catalog')
    models = value['default']
    _need(type(models) is dict and 0 < len(models) <= 10000, 'notice_catalog')
    result = {}
    for name, item in models.items():
        _need(type(name) is str and _MODEL.fullmatch(name), 'notice_catalog')
        _need(type(item) is dict and set(item) == {'mode', 'rates'}, 'notice_catalog')
        _need(type(item['mode']) is str and item['mode'] in ('ratio', 'fixed'), 'notice_catalog')
        fields = {'input', 'output', 'cache_read', 'cache_write'} if item['mode'] == 'ratio' else {'per_call'}
        _need(type(item['rates']) is dict and set(item['rates']) == fields, 'notice_catalog')
        result[name] = dict(mode=item['mode'], rates={k: _amount(v) for k, v in item['rates'].items()})
    return {'default': result}


def _body(payload):
    date = datetime.fromtimestamp(payload['not_before'], timezone.utc).astimezone(
        ZoneInfo('Asia/Taipei')).isoformat(sep=' ', timespec='seconds')
    labels = {'input': '輸入', 'output': '輸出', 'cache_read': '快取讀取',
              'cache_write': '快取寫入', 'per_call': '每次呼叫'}
    lines = ['AI 模型價格將調整，最早生效時間：%s（台北時間）。' % date,
             '不會早於分頁公告與全體通知寄出後七天生效；如通知延遲，生效時間順延。',
             '已轉換額度不退、不保價。']
    for change in payload['changes']:
        unit = '點／次' if change['mode'] == 'fixed' else '點／百萬 tokens'
        lines.append('模型：' + change['model'])
        for field in sorted(change['after']):
            lines.append('%s：%s → %s %s' % (labels[field], change['before'][field],
                                             change['after'][field], unit))
    lines.append('請至 AI 模型分頁的價目表查看公告。')
    return '\n'.join(lines)


@dataclass(frozen=True)
class Policy:
    enabled: bool = False
    environment: str = ''
    owner_decision_ref: str = ''
    legal_approval_ref: str = ''
    recipient_scope_ref: str = ''

    def require_enabled(self):
        _need(self.enabled is True, 'notice_dormant')
        _need(self.environment in ('stg', 'prod'), 'notice_environment')
        _need(all(_ref(v) for v in (self.owner_decision_ref, self.legal_approval_ref,
                                    self.recipient_scope_ref)), 'notice_decision_pending')


@dataclass(frozen=True)
class Notice:
    change_id: str
    policy: Policy
    before_json: str
    after_json: str
    recipients: tuple
    prepared_at: int
    not_before: int
    payload_json: str

    @property
    def fingerprint(self):
        # Bind audience/environment/approvals and both effective catalogs, not just title.
        return _hash(_json(dict(change_id=self.change_id, policy=self.policy.__dict__,
            before=self.before_json, after=self.after_json, recipients=self.recipients,
            prepared_at=self.prepared_at, not_before=self.not_before, payload=self.payload_json)))


@dataclass(frozen=True)
class MailEvidence:
    recipient: str
    state: str
    sent_at: int
    fingerprint: str


@dataclass(frozen=True)
class PublicationEvidence:
    fingerprint: str
    published_at: int
    visible: bool
    mails: tuple


@dataclass(frozen=True)
class RecipientSnapshot:
    """Exact draft obligation; an absent address is retained, never filtered."""

    key: str
    destination: str | None
    revision: int


@dataclass(frozen=True)
class CandidateSnapshot:
    """Content-addressed proposal only: no source, approval or commit authority."""

    version: str
    environment: str
    campaign_fingerprint: str
    catalog_sha256: str
    audience_sha256: str
    recipient_count: int
    unresolved_count: int
    payload_json: str
    fingerprint: str


def _effective_changes(before, after):
    before, after = _catalog(before), _catalog(after)
    old, new = before['default'], after['default']
    _need(set(old) == set(new), 'notice_scope_review')
    changed, increases = [], False
    for name in sorted(old):
        _need(old[name]['mode'] == new[name]['mode'], 'notice_scope_review')
        if old[name] == new[name]:
            continue
        increases |= any(Decimal(new[name]['rates'][k]) > Decimal(v)
                         for k, v in old[name]['rates'].items())
        changed.append(dict(model=name, mode=new[name]['mode'],
                            before=old[name]['rates'], after=new[name]['rates']))
    _need(increases, 'notice_no_increase')
    _need(len(changed) <= 100, 'notice_scope_review')  # Public projection limit.
    return before, after, changed


def _audience_snapshot(recipients):
    _need(type(recipients) is tuple and 0 < len(recipients) <= 100000, 'notice_audience')
    rows, keys, unresolved = [], set(), 0
    for recipient in recipients:
        _need(type(recipient) is RecipientSnapshot, 'notice_audience')
        key, destination, revision = recipient.key, recipient.destination, recipient.revision
        _need(type(key) is str and _RECIPIENT.fullmatch(key) and key not in keys,
              'notice_audience')
        _need(type(revision) is int and 0 < revision <= 2147483647, 'notice_audience')
        if destination is None:
            unresolved += 1
        else:
            _need(type(destination) is str and 3 <= len(destination) <= 320
                  and destination.count('@') == 1
                  and all(part for part in destination.split('@'))
                  and not any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127
                              or ch in '<>\x00' for ch in destination), 'notice_address')
        keys.add(key)
        rows.append(dict(key=key, destination=destination, revision=revision))
    return sorted(rows, key=lambda row: row['key']), unresolved


def prepare_candidate(*, version, environment, body_text, before, after,
                      recipients, now, not_before):
    """Bind proposed content and every supplied obligation without authorizing it.

    The caller's catalog and audience may be incomplete or forged. This result
    is never publication, legal approval, SMTP evidence or a price-write permit.
    A future adapter must verify sources and persist the exact fingerprint.
    """
    _need(_ref(version) and environment in ('stg', 'prod'), 'notice_identity')
    _need(type(body_text) is str and 0 < len(body_text) <= 65536
          and body_text.strip() and '\x00' not in body_text, 'notice_body')
    now, not_before = _time(now), _time(not_before)
    _need(not_before >= now + SEVEN_DAYS, 'notice_too_soon')
    before, after, changes = _effective_changes(before, after)
    rows, unresolved = _audience_snapshot(recipients)
    catalog_sha256 = _hash(_json(dict(before=before, after=after)))
    audience_sha256 = _hash(_json(rows))
    campaign_fingerprint = _hash(_json(dict(
        version=version, body_text=body_text,
        catalog_fingerprint=catalog_sha256, environment=environment)))
    content_sha256 = _hash(_json(dict(campaign_fingerprint=campaign_fingerprint,
        catalog_sha256=catalog_sha256, audience_sha256=audience_sha256,
        recipient_count=len(rows), unresolved_count=unresolved,
        not_before=not_before)))
    payload = dict(schema=1, id='pc_' + content_sha256[:32], version=version,
                   title=_TITLE, body_text=body_text, not_before=not_before,
                   timezone='Asia/Taipei', unit='points_per_million_tokens_or_per_call',
                   changes=changes)
    payload_json = _json(payload)
    fingerprint = _hash(_json(dict(content_sha256=content_sha256,
                                   payload_json=payload_json)))
    return CandidateSnapshot(version, environment, campaign_fingerprint,
                             catalog_sha256, audience_sha256, len(rows), unresolved,
                             payload_json, fingerprint)


def prepare_notice(policy, change_id, before, after, recipients, *, now, not_before):
    """Build an inert publication/outbox plan; no model, network, mail or file writes."""
    _need(type(policy) is Policy, 'notice_policy')
    policy.require_enabled()  # Dormant denies before touching supplied catalogs/audience.
    _need(type(change_id) is str and re.fullmatch(r'pc_[a-f0-9]{32}', change_id), 'notice_id')
    now, not_before = _time(now), _time(not_before)
    _need(not_before >= now + SEVEN_DAYS, 'notice_too_soon')
    recipients = _audience(recipients)
    before, after, changed = _effective_changes(before, after)
    payload = dict(schema=1, id=change_id, title=_TITLE, not_before=not_before,
                   timezone='Asia/Taipei', unit='points_per_million_tokens_or_per_call', changes=changed)
    payload['body_text'] = _body(payload)
    return Notice(change_id, policy, _json(before), _json(after), recipients,
                  now, not_before, _json(payload))


def announcement(notice):
    """Allowlisted AI-tab DTO, no approval references, audience, addresses or options."""
    _need(type(notice) is Notice, 'notice_record')
    return json.loads(notice.payload_json)


def email_drafts(notice):
    """Same notice version for every recipient; NO mail.mail/create/send_mail call."""
    payload = announcement(notice)
    body = payload['body_text']
    fingerprint = notice.fingerprint
    return tuple(dict(recipient=key, subject=_TITLE, body_text=body,
                      fingerprint=fingerprint,
                      dedupe_key='%s:%s' % (fingerprint, key)) for key in notice.recipients)


def check_effective_gate(policy, notice, evidence, current, target, current_recipients, *, now):
    """Pure readiness result, NOT an apply permit or a signed upstream receipt.

    A future adapter must serialize this fresh check with ALL pricing writers,
    validate approval authority, and bind the exact upstream mutation/readback.
    mail queue/outgoing/exception/absence is never treated as sent or delivered.
    """
    _need(type(policy) is Policy, 'notice_policy')
    policy.require_enabled()
    _need(type(notice) is Notice and policy == notice.policy, 'notice_policy_changed')
    now = _time(now)
    _need(_json(_catalog(current)) == notice.before_json, 'notice_baseline_changed')
    _need(_json(_catalog(target)) == notice.after_json, 'notice_target_changed')
    _need(_audience(current_recipients) == notice.recipients, 'notice_audience_changed')
    fingerprint = notice.fingerprint
    _need(type(evidence) is PublicationEvidence and evidence.visible is True
          and evidence.fingerprint == fingerprint, 'notice_unpublished')
    published = _time(evidence.published_at)
    _need(notice.prepared_at <= published <= now, 'notice_time')
    _need(type(evidence.mails) is tuple, 'notice_mail_pending')
    _need(all(type(mail) is MailEvidence for mail in evidence.mails), 'notice_mail_pending')
    _need(len(evidence.mails) == len(notice.recipients)
          and {mail.recipient for mail in evidence.mails} == set(notice.recipients), 'notice_mail_pending')
    sent = []
    for mail in evidence.mails:
        _need(mail.state == 'sent' and mail.fingerprint == fingerprint, 'notice_mail_pending')
        sent_at = _time(mail.sent_at)
        _need(published <= sent_at <= now, 'notice_time')
        sent.append(sent_at)
    eligible_at = max(notice.not_before, published + SEVEN_DAYS, max(sent) + SEVEN_DAYS)
    _need(now >= eligible_at, 'notice_not_mature')
    return dict(change_id=notice.change_id, fingerprint=fingerprint,
                target_sha256=_hash(notice.after_json), eligible_at=eligible_at,
                runtime_apply_implemented=False)

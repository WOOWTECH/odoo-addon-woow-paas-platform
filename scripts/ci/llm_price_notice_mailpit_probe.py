"""Explicit Odoo-shell-only proof of one synthetic SMTP acceptance in Mailpit.

The workflow feeds this file to ``odoo shell`` in a throwaway database. It is
never imported by the addon, and its proposed journal DDL is never migrated.
All addresses and transport endpoints are fixed to the private CI network.
"""

import hashlib
import email.policy
import json
import os
import time
import urllib.request
import uuid
from email.parser import BytesParser
from types import SimpleNamespace

import odoo
from odoo.modules import module

from odoo.addons.woow_paas_platform.services.llm_price_notice import (
    RecipientSnapshot,
    SEVEN_DAYS,
    prepare_candidate,
)
from odoo.addons.woow_paas_platform.services.llm_price_notice_odoo_adapter import (
    BINDING_SCHEMA,
    MailBinding,
    odoo_boundary,
)
from odoo.addons.woow_paas_platform.services.llm_price_notice_odoo_smtp import BoundNoticeSMTP
from odoo.addons.woow_paas_platform.services.llm_price_notice_transport import (
    Attempt,
    SCHEMA,
    _matches_mime_snapshot,
)


CAMPAIGN = 'woow_paas_platform.llm_price_notice_campaign'
RECIPIENT = 'woow_paas_platform.llm_price_notice_recipient'
PUBLICATION = 'woow_paas_platform.llm_price_notice_publication'
OUTBOX = 'woow_paas_platform.llm_price_notice_outbox'
RUNTIME_KEY = 'llm_price_notice_runtime_environment'
DESTINATION = 'one@example.test'
SENDER = 'notice@example.test'
MARKER = '【合成測試，不代表正式公告】'
BEFORE = {'default': {'vendor/model': {'mode': 'fixed', 'rates': {'per_call': '1'}}}}
AFTER = {'default': {'vendor/model': {'mode': 'fixed', 'rates': {'per_call': '1.3'}}}}
TABLES = ('llm_notice_transport_attempt', 'llm_notice_transport_acceptance',
          'llm_notice_odoo_mail_binding')


def _require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def _mailpit_json(path):
    url = 'http://mailpit:8025' + path
    with urllib.request.urlopen(url, timeout=5) as response:
        _require(response.status == 200, 'mailpit_api_status')
        return json.load(response)


def _mailpit_raw(message_id):
    # Mailpit IDs are opaque. Use the documented `latest` alias only after
    # checking that the isolated mailbox has exactly one message.
    _require(message_id == 'latest', 'mailpit_raw_id_not_allowed')
    with urllib.request.urlopen('http://mailpit:8025/api/v1/message/latest/raw',
                                timeout=5) as response:
        _require(response.status == 200, 'mailpit_raw_status')
        return response.read()


def _mailpit_messages():
    result = _mailpit_json('/api/v1/messages')
    messages = result.get('messages')
    _require(type(messages) is list, 'mailpit_messages_shape')
    _require(result.get('messages_count') == len(messages), 'mailpit_messages_count')
    return messages


def _make_message(server, mail):
    # Follow the Odoo 18 mail.mail -> ir.mail_server.build_email path, but keep
    # the queue's sender and Return-Path inside example.test.
    values = mail._prepare_outgoing_list()
    _require(len(values) == 1, 'one_outgoing_message_required')
    item = values[0]
    _require(item['email_to'] == [DESTINATION]
             and item['email_to_normalized'] == [DESTINATION]
             and not item['email_cc'] and not item['partner_id']
             and item['email_from'] == SENDER
             and item['headers']['Return-Path'] == SENDER,
             'synthetic_mail_values_required')
    message = server.build_email(
        email_from=mail.email_from,
        email_to=item['email_to'],
        subject=item['subject'],
        body=item['body'],
        body_alternative=item['body_alternative'],
        email_cc=item['email_cc'],
        reply_to=item['reply_to'],
        attachments=item['attachments'],
        message_id=item['message_id'],
        references=item['references'],
        object_id=item['object_id'],
        subtype='html',
        subtype_alternative='plain',
        headers=item['headers'],
    )
    # _prepare_email_message mutates From/Bcc/To. Freeze *after* this pass,
    # before opening an SMTP socket. The real session must not alter the bytes;
    # BoundNoticeSMTP checks the exact bytes smtplib passes to sendmail/DATA.
    smtp_from, smtp_to, message = server._prepare_email_message(
        message, SimpleNamespace(from_filter=False, smtp_from=False))
    _require(smtp_from == SENDER and smtp_to == [DESTINATION],
             'synthetic_final_envelope_required')
    payload = message.as_bytes(policy=message.policy.clone(linesep='\r\n'))
    _require(_matches_mime_snapshot(payload, hashlib.sha256(payload).hexdigest()),
             'noncanonical_final_mime')
    _require(message['Bcc'] is None and message['Cc'] is None
             and message['To'] == DESTINATION and message['From'] == SENDER,
             'synthetic_final_headers_required')
    return message, payload


def main(env):
    _require(os.environ.get('WOOW_LLM_PRICE_NOTICE_REHEARSAL') == 'llmdev-synthetic-v1',
             'synthetic_rehearsal_flag_required')
    _require(os.environ.get('MAILPIT_HOST') == 'mailpit'
             and os.environ.get('MAILPIT_SMTP_PORT') == '1025'
             and os.environ.get('MAILPIT_HTTP_PORT') == '8025',
             'private_mailpit_endpoint_required')
    _require(env.cr.dbname == 'test' and env.uid == odoo.SUPERUSER_ID,
             'scratch_database_and_service_identity_required')
    _require(not bool(module.current_test), 'odoo_test_mode_forbidden')
    _require(not _mailpit_messages(), 'mailpit_must_start_empty')

    # One isolated scratch database. Refuse to overwrite a deployed journal.
    for table in TABLES:
        env.cr.execute('SELECT to_regclass(%s)', (table,))
        _require(env.cr.fetchone()[0] is None, 'journal_schema_already_installed')
    for statement in (*SCHEMA, BINDING_SCHEMA):
        env.cr.execute(statement)
    env.cr.commit()

    config = env['ir.config_parameter'].sudo()
    _require(config.get_param(RUNTIME_KEY) in (False, 'stg'),
             'runtime_environment_not_staging')
    config.set_param(RUNTIME_KEY, 'stg')
    _require(config.get_param(RUNTIME_KEY) == 'stg', 'runtime_environment_not_staging')
    version = 'rehearsal-' + uuid.uuid4().hex
    body = MARKER + '\nCI Mailpit acceptance probe.'
    not_before = int(time.time()) + SEVEN_DAYS + 120
    recipient = RecipientSnapshot('paas:synthetic_one', DESTINATION, 1)
    proposal = prepare_candidate(
        version=version, environment='stg', body_text=body,
        before=BEFORE, after=AFTER, recipients=(recipient,),
        now=int(time.time()), not_before=not_before)
    campaign = env[CAMPAIGN].create({
        'version': version, 'body_text': body,
        'catalog_fingerprint': proposal.catalog_sha256, 'environment': 'stg',
    })
    env[RECIPIENT].create({
        'campaign_id': campaign.id, 'recipient_key': recipient.key,
        'destination_snapshot': DESTINATION, 'destination_revision': 1,
    })
    publication = env[PUBLICATION]._llm_publish_synthetic_rehearsal(
        campaign.id, BEFORE, AFTER, not_before)
    outbox = env[OUTBOX].search([('publication_id', '=', publication.id)])
    _require(len(outbox) == 1 and outbox.state == 'pending'
             and outbox.destination_snapshot == DESTINATION,
             'single_committed_synthetic_outbox_required')
    mail = env['mail.mail'].create({
        'subject': 'Synthetic staging notice Mailpit probe',
        'body_html': '<p>' + MARKER + '</p><p>CI Mailpit acceptance probe.</p>',
        'email_from': SENDER,
        'reply_to': SENDER,
        'email_to': DESTINATION,
        'headers': repr({'Return-Path': SENDER}),
        'auto_delete': False,
        'state': 'exception',  # No normal outgoing queue owns this scratch row.
    })
    campaign_id, outbox_id, mail_id = campaign.id, outbox.id, mail.id
    campaign_fingerprint = campaign.fingerprint
    env.cr.commit()  # Independent journal and ORM reads must see these rows.

    with env.registry.cursor() as committed:
        committed.execute('SELECT count(*) FROM woow_paas_platform_llm_price_notice_outbox '
                          'WHERE id = %s AND state = %s', (outbox_id, 'pending'))
        _require(committed.fetchone()[0] == 1, 'outbox_not_committed')
        committed.execute('SELECT count(*) FROM mail_mail WHERE id = %s', (mail_id,))
        _require(committed.fetchone()[0] == 1, 'mail_not_committed')

    server = env['ir.mail_server']
    mail = env['mail.mail'].browse(mail_id)
    message, payload = _make_message(server, mail)
    attempt = Attempt(
        str(uuid.uuid4()), campaign_fingerprint, 'stg', recipient.key,
        DESTINATION, 1, hashlib.sha256(payload).hexdigest())
    binding = MailBinding(attempt, env.cr.dbname, mail_id, outbox_id)
    boundary = odoo_boundary(env, service_uid=odoo.SUPERUSER_ID,
                             runtime_environment='stg')
    boundary._journal.prepare(attempt)
    boundary.bind(binding, payload=payload, envelope=(DESTINATION,))
    _require(boundary.preflight(binding, payload=payload,
                                envelope=(DESTINATION,)) == binding,
             'committed_binding_preflight_failed')

    # A non-journal write in the outer Odoo transaction must disappear after
    # DATA 250, while the independently committed acceptance receipt survives.
    rollback_key = 'test.llm_price_notice.mailpit.' + attempt.attempt_uuid
    config.set_param(rollback_key, 'must-rollback')
    env.flush_all()

    # Explicit one-use Odoo 18 connection to the private Mailpit container.
    # Odoo connect issues EHLO; MAIL/RCPT/DATA remain behind the bound claim.
    session = server.connect(host='mailpit', port=1025, encryption='none')
    _require(session is not None, 'odoo_smtp_connection_missing')
    try:
        actual_from, actual_to, message = server._prepare_email_message(message, session)
        _require(actual_from == SENDER and actual_to == [DESTINATION]
                 and message.as_bytes(policy=message.policy.clone(linesep='\r\n')) == payload,
                 'live_odoo_session_changed_final_mime')
        bound = BoundNoticeSMTP(session, boundary=boundary, binding=binding)
        sent_message_id = server.send_email(message, smtp_session=bound)
        _require(sent_message_id == message['Message-Id'], 'odoo_mail_send_result')
        try:
            bound.send_message(message, SENDER, [DESTINATION])
        except ValueError as error:
            _require(str(error) == 'notice_session_single_use',
                     'wrong_second_send_rejection')
        else:
            raise RuntimeError('bound_session_reused')
    finally:
        session.quit()

    env.cr.rollback()
    with env.registry.cursor() as verify:
        verify.execute('SELECT value FROM ir_config_parameter WHERE key = %s',
                       (rollback_key,))
        _require(verify.fetchone() is None, 'outer_odoo_rollback_failed')
        verify.execute('SELECT status FROM llm_notice_transport_attempt '
                       'WHERE attempt_uuid = %s', (attempt.attempt_uuid,))
        _require(verify.fetchone() == ('accepted',), 'journal_not_accepted')
        verify.execute('SELECT accepted_at_us, data_code '
                       'FROM llm_notice_transport_acceptance WHERE attempt_uuid = %s',
                       (attempt.attempt_uuid,))
        receipt = verify.fetchone()
        _require(receipt is not None and receipt[0] > 0 and receipt[1] == 250,
                 'accepted_receipt_missing')
        verify.execute('SELECT count(*) FROM llm_notice_odoo_mail_binding '
                       'WHERE database_name = %s AND obligation_id = %s AND mail_id = %s',
                       (env.cr.dbname, outbox_id, mail_id))
        _require(verify.fetchone()[0] == 1, 'binding_not_committed')
        verify.execute('SELECT count(*) FROM woow_paas_platform_llm_price_notice_campaign '
                       'WHERE id = %s', (campaign_id,))
        _require(verify.fetchone()[0] == 1, 'source_not_committed')

    messages = []
    for _ in range(30):
        messages = _mailpit_messages()
        if messages:
            break
        time.sleep(0.2)
    _require(len(messages) == 1, 'mailpit_single_message_required')
    raw = _mailpit_raw('latest')
    _require(raw == payload, 'mailpit_raw_mime_sha_mismatch'
             + f': expected={attempt.payload_sha256}, actual={hashlib.sha256(raw).hexdigest()}')
    parsed = BytesParser(policy=email.policy.default).parsebytes(raw)
    _require(parsed['To'] == DESTINATION and parsed['From'] == SENDER
             and parsed['Cc'] is None and parsed['Bcc'] is None,
             'mailpit_message_recipients_mismatch')
    decoded_parts = '\n'.join(part.get_content() for part in parsed.walk()
                              if part.get_content_type().startswith('text/'))
    _require(MARKER in decoded_parts, 'mailpit_synthetic_body_missing')
    # 250 proves Mailpit accepted DATA, not delivery to a recipient mailbox.
    print('LLM_PRICE_NOTICE_MAILPIT_PROBE_OK')


main(globals()['env'])  # Odoo shell injects `env`; ordinary Python does not.

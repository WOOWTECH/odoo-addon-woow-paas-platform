"""Read challenges only from an isolated native test database's secured outbox.

No delivery or provider calls: the fixture authenticates the intended recipient
and uses the same HTTP acceptance endpoint as the product.
"""
import hashlib
import html
import json
import re


def accept_queued_invitation(case, issued, recipient, password, post):
    case.assertTrue(issued.get('success'), issued)
    case.assertEqual(issued['data']['state'], 'pending')
    case.assertEqual(issued['data']['user_id'], recipient.id)
    ledger = case.env['woow_paas_platform.workspace_invitation'].sudo().browse(
        issued['data']['id'])
    case.assertEqual(ledger.state, 'pending')
    case.assertEqual(ledger.recipient_email, recipient.login.strip().lower())
    membership = case.env['woow_paas_platform.workspace_access'].sudo()
    domain = [('workspace_id', '=', ledger.workspace_id.id),
              ('user_id', '=', recipient.id)]
    case.assertFalse(membership.search(domain), 'Issuance must not grant access')
    mail = case.env['mail.mail'].sudo().search([
        ('model', '=', ledger._name), ('res_id', '=', ledger.id),
    ], order='id desc', limit=1)
    case.assertTrue(mail, 'Expected the isolated native outbox challenge')
    case.assertEqual(mail.state, 'outgoing')
    case.assertEqual(mail.email_to, ledger.recipient_email)
    case.assertFalse(mail.email_cc)
    case.assertFalse(mail.recipient_ids)
    match = re.search(r'workspace-invitation/accept/([A-Za-z0-9_-]{43})',
                      html.unescape(mail.body_html or ''))
    case.assertTrue(match, 'Expected a mailbox challenge in the native outbox')
    token = match.group(1)
    case.assertEqual(ledger.token_digest,
                     hashlib.sha256(token.encode('ascii')).hexdigest())
    case.assertNotIn(token, json.dumps(issued))
    case.authenticate(recipient.login, password)
    result = post('/api/workspace-invitations/accept', {'token': token})
    case.env.invalidate_all()
    case.assertNotIn(token, json.dumps(result))
    if result.get('success'):
        case.assertEqual(ledger.state, 'accepted')
        case.assertEqual(result['data']['user_id'], recipient.id)
        case.assertEqual(result['data']['workspace_id'], ledger.workspace_id.id)
        case.assertEqual(membership.search(domain).role, ledger.role)
    else:
        case.assertEqual(ledger.state, 'pending')
        case.assertFalse(membership.search(domain),
                         'Failed acceptance must roll back the inserted grant')
    return result

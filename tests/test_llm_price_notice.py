"""Pure dormant notice draft tests; no Odoo, DB, mail, HTTP or upstream client."""
from copy import deepcopy
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('price_notice_draft', ROOT / 'src/services/llm_price_notice.py')
n = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = n
spec.loader.exec_module(n)
NOW = 1800000000
RECIPIENTS = ('paas:1', 'paas:2', 'legacy:owner-case')  # Synthetic, not live account identifiers.
ID = 'pc_' + '1' * 32


def catalog(input='1', output='2', cache_read='0.1', cache_write='1.25'):
    return {'default': {'vendor/model': dict(mode='ratio', rates=dict(
        input=input, output=output, cache_read=cache_read, cache_write=cache_write))}}


def policy():
    # TEST references only. Runtime adapter must verify real owner/legal authority.
    return n.Policy(True, 'stg', 'test-owner-1', 'test-legal-1', 'test-scope-1')


def notice(**kwargs):
    return n.prepare_notice(policy(), ID, catalog(), catalog(input='2'), RECIPIENTS,
                            now=NOW, not_before=NOW+n.SEVEN_DAYS, **kwargs)


def evidence(record, delay=0):
    return n.PublicationEvidence(record.fingerprint, NOW, True, tuple(
        n.MailEvidence(key, 'sent', NOW+delay, record.fingerprint) for key in record.recipients))


def gate(record=None, proof=None, **changes):
    record = record or notice()
    values = dict(policy=policy(), notice=record, evidence=proof or evidence(record),
                  current=catalog(), target=catalog(input='2'), current_recipients=RECIPIENTS,
                  now=NOW+n.SEVEN_DAYS)
    values.update(changes)
    return n.check_effective_gate(**values)


class TestPriceNotice(unittest.TestCase):
    def test_default_dormant_denies_before_inspecting_or_enqueuing_anything(self):
        class Poison:
            def __iter__(self): raise AssertionError('dormant must not inspect')
        with self.assertRaisesRegex(n.NoticeBlocked, '^notice_dormant$'):
            n.prepare_notice(n.Policy(), ID, Poison(), Poison(), Poison(), now=NOW, not_before=NOW)
        with self.assertRaisesRegex(n.NoticeBlocked, '^notice_dormant$'):
            gate(policy=n.Policy())

    def test_all_owner_legal_and_scope_decisions_required(self):
        for field in ('owner_decision_ref', 'legal_approval_ref', 'recipient_scope_ref'):
            with self.subTest(field=field), self.assertRaisesRegex(n.NoticeBlocked, 'notice_decision_pending'):
                n.prepare_notice(replace(policy(), **{field:''}), ID, catalog(), catalog(input='2'),
                                 RECIPIENTS, now=NOW, not_before=NOW+n.SEVEN_DAYS)
        for enabled in (1, 'true', None):
            with self.assertRaises(n.NoticeBlocked):
                gate(policy=replace(policy(), enabled=enabled))

    def test_banner_and_all_mail_drafts_are_same_version_and_no_delivery(self):
        record = notice()
        banner, mails = n.announcement(record), n.email_drafts(record)
        self.assertEqual({m['recipient'] for m in mails}, set(RECIPIENTS))
        self.assertTrue(all(m['body_text'] == banner['body_text'] for m in mails))
        self.assertTrue(all(m['fingerprint'] == record.fingerprint for m in mails))
        self.assertEqual(len({m['dedupe_key'] for m in mails}), len(RECIPIENTS))
        self.assertEqual(mails, n.email_drafts(record))
        self.assertIn('輸入：1 → 2 點／百萬 tokens', banner['body_text'])
        self.assertNotIn('Unix', banner['body_text'])
        self.assertNotIn('recipient', str(banner))
        self.assertNotIn('test-owner', str(banner))

    def test_seven_days_exact_boundary_not_calendar_dates(self):
        with self.assertRaisesRegex(n.NoticeBlocked, 'notice_not_mature'):
            gate(now=NOW+n.SEVEN_DAYS-1)
        self.assertEqual(gate()['eligible_at'], NOW+n.SEVEN_DAYS)
        self.assertFalse(gate()['runtime_apply_implemented'])

    def test_last_sent_and_actual_publication_restart_seven_day_clock(self):
        record = notice()
        late = evidence(record, delay=3600)
        with self.assertRaisesRegex(n.NoticeBlocked, 'notice_not_mature'):
            gate(record, late)
        self.assertEqual(gate(record, late, now=NOW+3600+n.SEVEN_DAYS)['eligible_at'],
                         NOW+3600+n.SEVEN_DAYS)
        published = replace(late, published_at=NOW+1800)
        self.assertEqual(gate(record, published, now=NOW+3600+n.SEVEN_DAYS)['eligible_at'],
                         NOW+3600+n.SEVEN_DAYS)

    def test_queued_missing_bounced_duplicate_and_wrong_version_mail_deny(self):
        record=notice(); proof=evidence(record)
        for state in ('outgoing','exception','cancel','received',None):
            invalid=replace(proof, mails=(replace(proof.mails[0], state=state),)+proof.mails[1:])
            with self.assertRaises(n.NoticeBlocked):gate(record, invalid)
        for mails in (proof.mails[:-1], proof.mails+(proof.mails[0],),
                      (proof.mails[0], proof.mails[0], proof.mails[2]),
                      (replace(proof.mails[0], fingerprint='0'*64),)+proof.mails[1:]):
            with self.assertRaises(n.NoticeBlocked):gate(record, replace(proof,mails=mails))

    def test_visibility_withdrawal_and_wrong_campaign_fail_closed(self):
        proof=evidence(notice())
        for changes in ({'visible':False},{'visible':1},{'fingerprint':'0'*64}):
            with self.assertRaisesRegex(n.NoticeBlocked,'notice_unpublished'):
                gate(proof=replace(proof,**changes))

    def test_current_target_environment_approvals_and_audience_are_bound(self):
        for changes in ({'current':catalog(input='1.1')}, {'target':catalog(input='3')},
                        {'policy':replace(policy(),environment='prod')},
                        {'policy':replace(policy(),legal_approval_ref='test-revoked')},
                        {'current_recipients':RECIPIENTS+('paas:3',)},
                        {'current_recipients':RECIPIENTS[:-1]}):
            with self.subTest(changes=list(changes)),self.assertRaises(n.NoticeBlocked):gate(**changes)

    def test_notice_replacement_and_content_changes_do_not_reuse_old_evidence(self):
        record=notice(); proof=evidence(record)
        for altered in (replace(record,change_id='pc_'+'2'*32),
                        replace(record,not_before=record.not_before+1),
                        replace(record,payload_json=json.dumps({**n.announcement(record),'body_text':'changed'}))):
            with self.assertRaises(n.NoticeBlocked):gate(altered,proof)

    def test_all_rate_components_and_mixed_changes_detect_increase(self):
        for field in ('input','output','cache_read','cache_write'):
            after=catalog(**{field:'3'})
            record=n.prepare_notice(policy(),ID,catalog(),after,RECIPIENTS,
                                    now=NOW,not_before=NOW+n.SEVEN_DAYS)
            self.assertEqual(len(n.announcement(record)['changes']),1)
        after=catalog(input='0.5',output='3')
        record=n.prepare_notice(policy(),ID,catalog(),after,RECIPIENTS,
                                now=NOW,not_before=NOW+n.SEVEN_DAYS)
        self.assertTrue(n.announcement(record)['changes'])

    def test_fixed_per_call_increase_supported(self):
        old={'default':{'vendor/model':dict(mode='fixed',rates={'per_call':'0.1'})}}
        new={'default':{'vendor/model':dict(mode='fixed',rates={'per_call':'0.2'})}}
        record=n.prepare_notice(policy(),ID,old,new,RECIPIENTS,now=NOW,not_before=NOW+n.SEVEN_DAYS)
        self.assertIn('0.1 → 0.2 點／次',n.announcement(record)['body_text'])

    def test_no_decrease_or_equal_price_bypass_of_draft(self):
        for after in (catalog(),catalog(input='0.5')):
            with self.assertRaisesRegex(n.NoticeBlocked,'notice_no_increase'):
                n.prepare_notice(policy(),ID,catalog(),after,RECIPIENTS,
                                 now=NOW,not_before=NOW+n.SEVEN_DAYS)

    def test_unknown_price_tiering_model_or_group_scope_requires_review(self):
        invalids=[catalog(cache_read=None),catalog(input='NaN'),catalog(input='Infinity'),
                  catalog(input=True),catalog(input=1.1),catalog(input='-1'),catalog(input='1e100'),catalog(input='0e-99999999')]
        x=catalog();x['default']['vendor/model']['mode']='tiered';invalids.append(x)
        x=catalog();x['default']['other/model']=deepcopy(x['default']['vendor/model']);invalids.append(x)
        x=catalog();x['free']={};invalids.append(x)
        x=catalog();x['default']['vendor/model']['rates']['unknown']='1';invalids.append(x)
        for after in invalids:
            with self.assertRaises(n.NoticeBlocked):
                n.prepare_notice(policy(),ID,catalog(),after,RECIPIENTS,
                                 now=NOW,not_before=NOW+n.SEVEN_DAYS)

    def test_bad_identity_audience_and_injection_deny_without_mutating_inputs(self):
        before,after=catalog(),catalog(input='2'); snapshot=deepcopy((before,after))
        for audience in ((),('paas:1','paas:1'),('somebody@example.invalid',),('paas:<script>',),None):
            with self.assertRaises(n.NoticeBlocked):
                n.prepare_notice(policy(),ID,before,after,audience,now=NOW,not_before=NOW+n.SEVEN_DAYS)
        self.assertEqual((before,after),snapshot)
        bad={'default':{'<script>':next(iter(after['default'].values()))}}
        with self.assertRaises(n.NoticeBlocked):
            n.prepare_notice(policy(),ID,before,bad,RECIPIENTS,now=NOW,not_before=NOW+n.SEVEN_DAYS)

    def test_timestamps_cannot_be_future_backdated_boolean_or_invalid(self):
        record=notice(); proof=evidence(record)
        for stamp in (True,0,-1,'1800000000',NOW+n.SEVEN_DAYS+1):
            with self.assertRaises(n.NoticeBlocked):
                gate(record,replace(proof,published_at=stamp))
        for stamp in (NOW-1,NOW+n.SEVEN_DAYS+1):
            with self.assertRaises(n.NoticeBlocked):
                gate(record,replace(proof,mails=(replace(proof.mails[0],sent_at=stamp),)+proof.mails[1:]))
        with self.assertRaisesRegex(n.NoticeBlocked,'notice_too_soon'):
            n.prepare_notice(policy(),ID,catalog(),catalog(input='2'),RECIPIENTS,
                             now=NOW,not_before=NOW+n.SEVEN_DAYS-1)

    def test_decimal_equivalence_order_and_retry_are_stable(self):
        record=notice()
        same=n.prepare_notice(policy(),ID,catalog(input='1.000'),catalog(input='2.00'),
                              tuple(reversed(RECIPIENTS)),now=NOW,not_before=NOW+n.SEVEN_DAYS)
        self.assertEqual(record.fingerprint,same.fingerprint)
        self.assertEqual(gate()['fingerprint'],record.fingerprint)

    def test_draft_has_no_runtime_registration_or_effect_capabilities(self):
        import ast
        tree=ast.parse((ROOT/'src/services/llm_price_notice.py').read_text())
        names={node.id for node in ast.walk(tree) if isinstance(node,ast.Name)}
        self.assertFalse(names & {'odoo','requests','socket','smtplib','subprocess','open','eval','exec'})
        self.assertNotIn('llm_price_notice',(ROOT/'src/services/__init__.py').read_text())


if __name__=='__main__':
    unittest.main()

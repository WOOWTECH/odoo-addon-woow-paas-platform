"""Append-only history of tunnel-binding changes (HA issue #38, point 2).

Who bound a Smart Home / Security Access tunnel, who released it, who was refused.
The binding itself only keeps the CURRENT holder; when a service has to be taken
over (the holder is gone, a machine was replaced) the question "what happened to
this binding" had no answer except the server log. Every state change and every
refusal now leaves one line here, written by the binding mixin.

Deliberately small and tenant-invisible: system administrators can read it
(Woow PaaS > Tunnel Binding History, or the Binding history button on a Security
Access), nobody can write or delete through the ORM's access rights, and the lines
are keyed by (anchor_model, anchor_id) without a foreign key so they outlive the
service they describe.
"""
from datetime import timedelta
import logging

from psycopg2 import OperationalError
from psycopg2.errors import ReadOnlySqlTransaction

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

EVENTS = [
    ('claimed', 'Claimed by an installation'),
    ('legacy_claimed', 'Bound by a component that does not identify itself'),
    ('upgraded', 'Older binding identified (the same machine updated its component)'),
    ('taken_over', 'Older binding taken over after an explicit confirmation'),
    ('released', 'Released'),
    ('refused_claim', 'Claim refused'),
    ('refused_release', 'Release refused'),
]


class TunnelBindingEvent(models.Model):
    _name = 'woow_paas_platform.tunnel_binding_event'
    _description = 'Tunnel binding history'
    _order = 'id desc'

    anchor_model = fields.Char(required=True, readonly=True, index=True)
    anchor_id = fields.Integer(required=True, readonly=True, index=True)
    event = fields.Selection(EVENTS, required=True, readonly=True, index=True)
    previous_state = fields.Char(readonly=True)
    new_state = fields.Char(readonly=True)
    instance_uid = fields.Char(string='Installation id', readonly=True)
    instance_label = fields.Char(string='Installation name', readonly=True)
    held_by = fields.Char(string='Held by', readonly=True,
                          help='Who held the binding when a request was refused.')
    actor = fields.Char(readonly=True,
                        help="'ha-instance' for a request from the integration, otherwise "
                             'the login of the person who released it.')
    detail = fields.Char(readonly=True)

    @api.model
    def log_event(self, anchor, event, *, previous_state='', new_state='', instance_uid='',
                  instance_label='', held_by='', actor='', detail='', dedupe_minutes=0):
        """Append one line; any other failure is logged and swallowed (an audit line must not fail the
        operation it records). Database errors that make Odoo retry the request are re-raised.

        ``dedupe_minutes`` skips a line identical (anchor, event, installation) to the latest one
        written within that many minutes: a refused installation retries on every restart.
        """
        try:
            with self.env.cr.savepoint():
                Event = self.sudo()
                domain = [('anchor_model', '=', anchor._name), ('anchor_id', '=', anchor.id)]
                if dedupe_minutes:
                    last = Event.search(domain, order='id desc', limit=1)
                    if (last and last.event == event and (last.instance_uid or '') == (instance_uid or '')
                            and last.create_date
                            and last.create_date > fields.Datetime.now() - timedelta(minutes=dedupe_minutes)):
                        return last
                return Event.create({
                    'anchor_model': anchor._name, 'anchor_id': anchor.id, 'event': event,
                    'previous_state': previous_state, 'new_state': new_state,
                    'instance_uid': (instance_uid or '')[:64], 'instance_label': (instance_label or '')[:128],
                    'held_by': (held_by or '')[:128], 'actor': (actor or '')[:128],
                    'detail': (detail or '')[:255],
                })
        except (OperationalError, ReadOnlySqlTransaction):
            # Not ours to swallow: the HA token endpoints start in a read-only transaction
            # and Odoo retries the request read/write when a write is attempted, and a
            # serialization failure is retried the same way.
            raise
        except Exception:  # noqa: BLE001 - see the docstring
            _logger.exception("[tunnel-binding] could not record %s for %s id=%s",
                              event, anchor._name, anchor.id)
            return self.browse()

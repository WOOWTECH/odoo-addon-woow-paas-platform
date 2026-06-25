import secrets
from datetime import timedelta

from odoo import api, fields, models

# RFC 8628 device flow parameters
DEVICE_CODE_LIFETIME = timedelta(minutes=15)
DEVICE_POLL_INTERVAL = 5  # seconds
MAX_VERIFICATION_ATTEMPTS = 5
# user_code alphabet: no vowels (avoids accidental words) + no easily-confused
# chars (0/O/1/I/L). 20 symbols ^ 8 chars ~= 34.6 bits, well above RFC 8628
# §6.1's 20-bit floor for the polling+entropy brute-force tradeoff.
_USER_CODE_ALPHABET = 'BCDFGHJKLMNPQRSTVWXZ'
_USER_CODE_LEN = 8


class OAuthDeviceCode(models.Model):
    _name = 'woow_paas_platform.oauth_device_code'
    _description = 'OAuth 2.0 Device Authorization Grant (RFC 8628)'
    _order = 'create_date desc'

    device_code = fields.Char(
        string='Device Code', required=True, index=True, readonly=True,
        help='High-entropy code the device polls the token endpoint with.',
    )
    user_code = fields.Char(
        string='User Code', required=True, index=True, readonly=True,
        help='Human-typed code the user enters at the verification page.',
    )
    scope = fields.Char(string='Scope', help='Space-separated requested scopes.')
    client_id = fields.Many2one(
        'woow_paas_platform.oauth_client', string='OAuth Client',
        required=True, ondelete='cascade', index=True,
    )
    user_id = fields.Many2one(
        'res.users', string='Approving User', ondelete='cascade', index=True,
        help='Set when the user approves at the verification page.',
    )
    status = fields.Selection(
        selection=[('pending', 'Pending'), ('approved', 'Approved'),
                   ('denied', 'Denied'), ('consumed', 'Consumed')],
        default='pending', required=True, index=True,
    )
    expires_at = fields.Datetime(string='Expires At', required=True)
    last_polled_at = fields.Datetime(string='Last Polled At')
    interval = fields.Integer(string='Poll Interval (s)', default=DEVICE_POLL_INTERVAL)

    _sql_constraints = [
        ('unique_device_code', 'UNIQUE(device_code)', 'device_code must be unique.'),
        ('unique_user_code', 'UNIQUE(user_code)', 'user_code must be unique.'),
    ]

    @api.model
    def _generate_user_code(self) -> str:
        raw = ''.join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(_USER_CODE_LEN))
        return f'{raw[:4]}-{raw[4:]}'

    @api.model
    def create_for_client(self, client, scope: str):
        """Create a pending device_code with collision-free unique codes."""
        user_code = self._generate_user_code()
        for _ in range(5):
            # Search the WHOLE table: UNIQUE(user_code) is unconditional, so an
            # expired/consumed row not yet GC'd still owns the value. Filtering
            # to status='pending' here would let a stale collision slip past the
            # Python check and hit the DB constraint -> IntegrityError on a
            # long-lived DB (fresh-install green, prod flaky).
            if not self.sudo().search_count([('user_code', '=', user_code)]):
                break
            user_code = self._generate_user_code()
        return self.sudo().create({
            'device_code': secrets.token_urlsafe(32),
            'user_code': user_code,
            'scope': scope or '',
            'client_id': client.id,
            'expires_at': fields.Datetime.now() + DEVICE_CODE_LIFETIME,
            'interval': DEVICE_POLL_INTERVAL,
        })

    def is_expired(self) -> bool:
        self.ensure_one()
        return fields.Datetime.now() >= self.expires_at

    def register_poll(self) -> bool:
        """Record a poll. Return True if it arrived faster than `interval`
        (caller must answer slow_down). Bumps interval per RFC 8628 §3.5."""
        self.ensure_one()
        now = fields.Datetime.now()
        too_fast = bool(
            self.last_polled_at
            and (now - self.last_polled_at).total_seconds() < self.interval
        )
        self.last_polled_at = now
        if too_fast:
            # RFC 8628 §3.5 mandates +5 per slow_down; cap so a flapping but
            # benign client can't ratchet the legit device's interval unbounded.
            self.interval = min(self.interval + 5, 30)
        return too_fast

    def mark_approved(self, user) -> None:
        self.ensure_one()
        self.write({'status': 'approved', 'user_id': user.id})

    def mark_denied(self) -> None:
        self.ensure_one()
        self.status = 'denied'

    def consume(self) -> bool:
        """Atomically transition approved -> consumed. Return True iff THIS call
        won the race (only the winner may issue tokens). Compare-and-swap via
        conditional UPDATE rowcount is race-safe under READ COMMITTED — two
        concurrent polls cannot double-issue tokens for one authorization.
        (Mirrors oauth_code's single-use flag discipline rather than a delete,
        so a replayed device_code gets a clean error, not 'unknown'.)"""
        self.ensure_one()
        # Flush pending ORM writes (e.g. a mark_approved() in the same
        # transaction) so the raw-SQL compare-and-swap sees the current status
        # rather than a stale pre-write value (ORM buffer vs raw SQL).
        self.flush_recordset(['status'])
        self.env.cr.execute(
            "UPDATE woow_paas_platform_oauth_device_code "
            "SET status='consumed' WHERE id=%s AND status='approved'",
            (self.id,),
        )
        won = self.env.cr.rowcount == 1
        if won:
            self.invalidate_recordset(['status'])
        return won

    @api.model
    def _cron_gc_expired(self) -> None:
        """Best-effort GC: delete device codes past expiry + 1h grace."""
        cutoff = fields.Datetime.now() - timedelta(hours=1)
        self.sudo().search([('expires_at', '<', cutoff)]).unlink()

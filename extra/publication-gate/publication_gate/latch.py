"""Durable high-water close latch; generations come only from Odoo's journal."""
from contextlib import closing
import os
from pathlib import Path
import re
import sqlite3
import uuid


class Conflict(Exception):
    pass


def validate_envelope(value, scope):
    expected = {'resource_identity', 'effect_id', 'desired_generation', 'desired_digest'}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError('exact envelope required')
    if value['resource_identity'] != scope:
        raise ValueError('scope mismatch')
    if not isinstance(value['effect_id'], str) or str(uuid.UUID(value['effect_id'])) != value['effect_id']:
        raise ValueError('canonical effect UUID required')
    generation = value['desired_generation']
    if type(generation) is not int or not 0 < generation < 2**63:
        raise ValueError('positive bigint generation required')
    if not isinstance(value['desired_digest'], str) or not re.fullmatch('[0-9a-f]{64}', value['desired_digest']):
        raise ValueError('SHA256 digest required')


class Latch:
    def __init__(self, path, scope, installation_id):
        if not re.fullmatch(r'cf-tunnel:v1:[0-9a-f]{32}:[0-9a-f-]{36}', scope):
            raise ValueError('canonical scope required')
        if str(uuid.UUID(scope.rsplit(':', 1)[1])) != scope.rsplit(':', 1)[1]:
            raise ValueError('canonical tunnel UUID required')
        self.path = str(Path(path).resolve())
        self.scope = scope
        self.installation_id = str(uuid.UUID(installation_id))

    def install(self):
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE latch (singleton INTEGER PRIMARY KEY CHECK(singleton=1), scope TEXT NOT NULL, installation TEXT NOT NULL, generation INTEGER NOT NULL, effect TEXT, digest TEXT)')
            db.execute('CREATE TABLE denial_receipt (generation INTEGER NOT NULL, effect TEXT NOT NULL, digest TEXT NOT NULL, session_sha256 TEXT NOT NULL, observed_at TEXT NOT NULL, PRIMARY KEY (generation, effect, digest, session_sha256))')
            db.execute('INSERT INTO latch VALUES (1, ?, ?, 0, NULL, NULL)',
                       (self.scope, self.installation_id))
            db.commit()
        self._fsync()

    def _fsync(self):
        for path in (self.path, str(Path(self.path).parent)):
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def close(self, envelope):
        validate_envelope(envelope, self.scope)
        uri = Path(self.path).as_uri() + '?mode=rw'
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            # Additive migration for an already-owned latch created by the
            # pre-receipt image.  Never recreate or reset the high-water row.
            db.execute('CREATE TABLE IF NOT EXISTS denial_receipt (generation INTEGER NOT NULL, effect TEXT NOT NULL, digest TEXT NOT NULL, session_sha256 TEXT NOT NULL, observed_at TEXT NOT NULL, PRIMARY KEY (generation, effect, digest, session_sha256))')
            row = db.execute('SELECT scope,installation,generation,effect,digest FROM latch WHERE singleton=1').fetchone()
            if not row or row[:2] != (self.scope, self.installation_id):
                raise Conflict('persisted scope unavailable or mismatched')
            incoming = (envelope['desired_generation'], envelope['effect_id'], envelope['desired_digest'])
            if incoming[0] < row[2] or (incoming[0] == row[2] and incoming != row[2:]):
                raise Conflict('stale generation or conflicting effect/digest')
            db.execute('UPDATE latch SET generation=?,effect=?,digest=? WHERE singleton=1', incoming)
            db.commit()
        self._fsync()

    def matches(self, envelope):
        validate_envelope(envelope, self.scope)
        uri = Path(self.path).as_uri() + '?mode=rw'
        with closing(sqlite3.connect(uri, uri=True)) as db:
            row = db.execute('SELECT scope,installation,generation,effect,digest FROM latch WHERE singleton=1').fetchone()
        return row == (self.scope, self.installation_id, envelope['desired_generation'],
                       envelope['effect_id'], envelope['desired_digest'])

    def record_denial(self, envelope, session_sha256):
        """Persist first observation of a valid, policy-denied app session.

        The caller supplies only a one-way digest derived from signed claims;
        raw JWTs, cookies, email addresses and subject identifiers never enter
        the state volume.  A receipt can be written only for the exact current
        high-water close envelope.
        """
        validate_envelope(envelope, self.scope)
        if not isinstance(session_sha256, str) or not re.fullmatch('[0-9a-f]{64}', session_sha256):
            raise ValueError('SHA256 session digest required')
        uri = Path(self.path).as_uri() + '?mode=rw'
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            row = db.execute(
                'SELECT scope,installation,generation,effect,digest FROM latch WHERE singleton=1'
            ).fetchone()
            expected = (self.scope, self.installation_id,
                        envelope['desired_generation'], envelope['effect_id'],
                        envelope['desired_digest'])
            if row != expected:
                raise Conflict('denial does not match current close envelope')
            db.execute(
                "INSERT OR IGNORE INTO denial_receipt VALUES (?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                (envelope['desired_generation'], envelope['effect_id'],
                 envelope['desired_digest'], session_sha256))
            db.commit()
            receipt = db.execute(
                'SELECT generation,effect,digest,session_sha256,observed_at '
                'FROM denial_receipt WHERE generation=? AND effect=? AND digest=? '
                'AND session_sha256=?',
                (envelope['desired_generation'], envelope['effect_id'],
                 envelope['desired_digest'], session_sha256)).fetchone()
        self._fsync()
        return dict(zip(
            ('desired_generation', 'effect_id', 'desired_digest',
             'session_sha256', 'observed_at'), receipt))

    def denial_receipts(self, envelope):
        validate_envelope(envelope, self.scope)
        uri = Path(self.path).as_uri() + '?mode=rw'
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS denial_receipt (generation INTEGER NOT NULL, effect TEXT NOT NULL, digest TEXT NOT NULL, session_sha256 TEXT NOT NULL, observed_at TEXT NOT NULL, PRIMARY KEY (generation, effect, digest, session_sha256))')
            rows = db.execute(
                'SELECT generation,effect,digest,session_sha256,observed_at '
                'FROM denial_receipt WHERE generation=? AND effect=? AND digest=? '
                'ORDER BY observed_at,session_sha256',
                (envelope['desired_generation'], envelope['effect_id'],
                 envelope['desired_digest'])).fetchall()
            db.commit()
        keys = ('desired_generation', 'effect_id', 'desired_digest',
                'session_sha256', 'observed_at')
        return [dict(zip(keys, row)) for row in rows]

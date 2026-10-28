#!/usr/bin/env python3
"""Batch54-owned remote effect end for the RACE-01 native oracle."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


PARKED = 'http_status:503'
LOCK = threading.Lock()
RELEASE = threading.Event()
STATE = {'armed': False, 'held': False, 'targets': {}, 'history': []}


def event(phase, **values):
    with LOCK:
        STATE['history'].append(
            {'seq': len(STATE['history']), 'phase': phase, **values})


class Handler(BaseHTTPRequestHandler):
    server_version = 'race01-effect/1'

    def log_message(self, fmt, *args):
        return

    def body(self):
        size = int(self.headers.get('Content-Length', '0'))
        return json.loads(self.rfile.read(size) or b'{}')

    def send_json(self, status, payload):
        raw = json.dumps(payload, sort_keys=True).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path != '/control/state':
            self.send_json(404, {'detail': 'not found'})
            return
        with LOCK:
            payload = json.loads(json.dumps(STATE))
        self.send_json(200, payload)

    def do_POST(self):
        if self.path == '/control/reset':
            with LOCK:
                STATE.update(armed=False, held=False, targets={}, history=[])
            RELEASE.set()
            self.send_json(200, {'ok': True})
            return
        if self.path == '/control/arm-live':
            RELEASE.clear()
            with LOCK:
                STATE['armed'] = True
                STATE['held'] = False
            self.send_json(200, {'ok': True})
            return
        if self.path == '/control/release':
            RELEASE.set()
            self.send_json(200, {'ok': True})
            return
        self.send_json(404, {'detail': 'not found'})

    def do_PUT(self):
        parsed = urlparse(self.path)
        bits = parsed.path.strip('/').split('/')
        if (len(bits) != 4 or bits[:2] != ['api', 'tunnels']
                or bits[3] != 'config'):
            self.send_json(404, {'detail': 'not found'})
            return
        tunnel_id = bits[2]
        data = self.body()
        target = data.get('service_url')
        actor = self.headers.get('X-RACE01-Actor', '?')
        event('offered', actor=actor, tunnel_id=tunnel_id, target=target)
        should_hold = False
        with LOCK:
            if STATE['armed'] and target != PARKED:
                STATE['armed'] = False
                STATE['held'] = True
                should_hold = True
        if should_hold:
            if not RELEASE.wait(30):
                event('timeout', actor=actor, tunnel_id=tunnel_id, target=target)
                self.send_json(504, {'detail': 'batch54 hold timeout'})
                return
            with LOCK:
                STATE['held'] = False
        with LOCK:
            STATE['targets'][tunnel_id] = target
        event('applied', actor=actor, tunnel_id=tunnel_id, target=target)
        self.send_json(200, {
            'message': 'Tunnel %s configuration updated' % tunnel_id,
            'hostname': data.get('hostname'),
            'service_url': target,
        })


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()

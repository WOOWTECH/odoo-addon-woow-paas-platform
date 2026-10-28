#!/usr/bin/env python3
"""Render an exact-owned sidecar patch and supporting private resources.

The output is JSON (valid Kubernetes YAML).  It deliberately contains Secret
names, never Secret data, and cannot create or patch anything itself.
"""
import argparse
import ipaddress
import json
import re
import sys


DNS = re.compile(r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$')
DIGEST_IMAGE = re.compile(r'^[^\s]+@sha256:[0-9a-f]{64}$')
SHA256 = re.compile(r'^[0-9a-f]{64}$')


def exact(config):
    required = {
        'namespace', 'workload', 'owner_uid', 'image', 'secret_name',
        'pvc_name', 'resource_identity', 'installation_id', 'canonical_name',
        'authority_host', 'authority_port', 'authority_server_sha256',
        'control_client_sha256', 'issuer', 'audience',
    }
    if not isinstance(config, dict) or set(config) != required:
        raise ValueError('exact deployment config required')
    for key in ('namespace', 'workload', 'secret_name', 'pvc_name'):
        if not isinstance(config[key], str) or not DNS.fullmatch(config[key]):
            raise ValueError(f'invalid {key}')
    if not DIGEST_IMAGE.fullmatch(config['image']):
        raise ValueError('digest-pinned image required')
    for key in ('authority_server_sha256', 'control_client_sha256'):
        if not SHA256.fullmatch(config[key]):
            raise ValueError(f'invalid {key}')
    if not isinstance(config['authority_port'], int) or not 1 <= config['authority_port'] <= 65535:
        raise ValueError('invalid authority_port')
    # Host may be an exact DNS label/FQDN or literal IP; no URL/path accepted.
    try:
        ipaddress.ip_address(config['authority_host'])
    except ValueError:
        labels = config['authority_host'].split('.')
        if not labels or any(not DNS.fullmatch(label) for label in labels):
            raise ValueError('invalid authority_host') from None
    for key in ('owner_uid', 'resource_identity', 'installation_id', 'canonical_name',
                'issuer', 'audience'):
        if not isinstance(config[key], str) or not config[key] or len(config[key]) > 512:
            raise ValueError(f'invalid {key}')
    return config


def render(c):
    c = exact(c)
    labels = {'app.kubernetes.io/part-of': 'b38-rev01',
              'woow.io/owner-uid': c['owner_uid']}
    gate = {
        'resource_identity': c['resource_identity'],
        'installation_id': c['installation_id'],
        'state': '/var/lib/woow-publication/latch.sqlite3',
        'issuer': c['issuer'], 'audience': c['audience'],
        'jwt_public_key': '/etc/woow-publication/secret/access-jwt.pem',
        'client_sha256': c['control_client_sha256'],
        'server_cert': '/etc/woow-publication/secret/gate.crt',
        'server_key': '/etc/woow-publication/secret/gate.key',
        'ca': '/etc/woow-publication/secret/ca.crt',
        'control_bind': '0.0.0.0', 'control_port': 18443,
        'data_bind': '0.0.0.0', 'data_port': 18303,
        'upstream': 'http://127.0.0.1:8123/manifest.json',
    }
    authority = {
        'host': c['authority_host'], 'port': c['authority_port'],
        'server_sha256': c['authority_server_sha256'],
        'ca': '/etc/woow-publication/secret/ca.crt',
        'client_cert': '/etc/woow-publication/secret/gate-client.crt',
        'client_key': '/etc/woow-publication/secret/gate-client.key',
    }
    config_name = c['workload'] + '-publication-gate'
    resources = [
        {'apiVersion': 'v1', 'kind': 'ConfigMap',
         'metadata': {'name': config_name, 'namespace': c['namespace'], 'labels': labels},
         'data': {'gate.json': json.dumps(gate, separators=(',', ':')),
                  'authority.json': json.dumps(authority, separators=(',', ':'))}},
        {'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim',
         'metadata': {'name': c['pvc_name'], 'namespace': c['namespace'], 'labels': labels},
         'spec': {'accessModes': ['ReadWriteOnce'], 'resources': {'requests': {'storage': '64Mi'}}}},
        {'apiVersion': 'v1', 'kind': 'Service',
         'metadata': {'name': config_name, 'namespace': c['namespace'], 'labels': labels},
         'spec': {'type': 'ClusterIP', 'selector': {'app': c['workload']},
                  'ports': [{'name': 'manifest-gate', 'port': 18303, 'targetPort': 18303}]}},
        {'apiVersion': 'batch/v1', 'kind': 'Job',
         'metadata': {'name': config_name + '-initialize', 'namespace': c['namespace'], 'labels': labels},
         'spec': {'backoffLimit': 0, 'template': {'metadata': {'labels': labels}, 'spec': {
             'restartPolicy': 'Never',
             'containers': [{'name': 'initialize', 'image': c['image'], 'args': ['initialize'],
                 'securityContext': {'runAsNonRoot': True, 'runAsUser': 65532,
                 'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                 'capabilities': {'drop': ['ALL']}},
                 'volumeMounts': [{'name': 'publication-gate-state', 'mountPath': '/var/lib/woow-publication'},
                                  {'name': 'publication-gate-config', 'mountPath': '/etc/woow-publication', 'readOnly': True},
                                  {'name': 'publication-gate-secret', 'mountPath': '/etc/woow-publication/secret', 'readOnly': True}]}],
             'volumes': [{'name': 'publication-gate-state', 'persistentVolumeClaim': {'claimName': c['pvc_name']}},
                         {'name': 'publication-gate-config', 'configMap': {'name': config_name}},
                         {'name': 'publication-gate-secret', 'secret': {'secretName': c['secret_name'], 'defaultMode': 256}}],
         }}}},
        {'apiVersion': 'apps/v1', 'kind': 'Deployment',
         'metadata': {'name': c['workload'], 'namespace': c['namespace']},
         'spec': {'template': {'metadata': {'labels': labels}, 'spec': {
             'containers': [{'name': 'publication-gate', 'image': c['image'],
                 'ports': [{'name': 'manifest-gate', 'containerPort': 18303},
                           {'name': 'gate-control', 'containerPort': 18443}],
                 'securityContext': {'runAsNonRoot': True, 'runAsUser': 65532,
                 'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                 'capabilities': {'drop': ['ALL']}},
                 'volumeMounts': [{'name': 'publication-gate-state', 'mountPath': '/var/lib/woow-publication'},
                                  {'name': 'publication-gate-config', 'mountPath': '/etc/woow-publication/config', 'readOnly': True},
                                  {'name': 'publication-gate-secret', 'mountPath': '/etc/woow-publication/secret', 'readOnly': True}],
                 'env': [{'name': 'PUBLICATION_GATE_BINDING', 'value': '/etc/woow-publication/config/gate.json'},
                         {'name': 'PUBLICATION_AUTHORITY_BINDING', 'value': '/etc/woow-publication/config/authority.json'}],
                 'readinessProbe': {'tcpSocket': {'port': 18303}, 'periodSeconds': 3},
                 'resources': {'requests': {'cpu': '10m', 'memory': '32Mi'},
                               'limits': {'cpu': '250m', 'memory': '128Mi'}}}],
             'volumes': [{'name': 'publication-gate-state', 'persistentVolumeClaim': {'claimName': c['pvc_name']}},
                         {'name': 'publication-gate-config', 'configMap': {'name': config_name}},
                         {'name': 'publication-gate-secret', 'secret': {'secretName': c['secret_name'], 'defaultMode': 256}}],
         }}}},
    ]
    return {'apiVersion': 'v1', 'kind': 'List', 'items': resources,
            'x-rev01': {'canonical_name': c['canonical_name'],
                        'apply_mode': 'server-side-owned-only-after-ops-release',
                        'pvc_cleanup': 'verify owner UID, delete PVC, then verify Longhorn volume absent'}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('config')
    args = parser.parse_args()
    with open(args.config, encoding='utf-8') as stream:
        json.dump(render(json.load(stream)), sys.stdout, indent=2)
    sys.stdout.write('\n')


if __name__ == '__main__':
    main()

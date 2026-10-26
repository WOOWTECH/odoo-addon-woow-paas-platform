#!/usr/bin/env python3
"""Render, but never apply, one isolated owned PaaS Operator deployment."""
import argparse
import json
import re
import sys

DNS = re.compile(r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$')
IMAGE = re.compile(r'^[^\s]+@sha256:[0-9a-f]{64}$')


def exact(value):
    keys = {'namespace', 'name', 'owner_uid', 'image', 'credential_secret',
            'api_key_name', 'account_id_name', 'tunnel_token_name'}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError('exact owned operator config required')
    for key in ('namespace', 'name', 'credential_secret'):
        if not isinstance(value[key], str) or not DNS.fullmatch(value[key]):
            raise ValueError('invalid ' + key)
    if not IMAGE.fullmatch(value['image']):
        raise ValueError('digest-pinned operator image required')
    for key in ('owner_uid', 'api_key_name', 'account_id_name', 'tunnel_token_name'):
        if not isinstance(value[key], str) or not value[key] or len(value[key]) > 253:
            raise ValueError('invalid ' + key)
    return value


def render(c):
    c = exact(c)
    labels = {'app': c['name'], 'app.kubernetes.io/part-of': 'b38-fault02',
              'woow.io/owner-uid': c['owner_uid']}
    refs = [('API_KEY', c['api_key_name']),
            ('CLOUDFLARE_ACCOUNT_ID', c['account_id_name']),
            ('CLOUDFLARE_TUNNEL_TOKEN', c['tunnel_token_name'])]
    env = [{'name': name, 'valueFrom': {'secretKeyRef': {
        'name': c['credential_secret'], 'key': key}}} for name, key in refs]
    items = [
        {'apiVersion': 'apps/v1', 'kind': 'Deployment',
         'metadata': {'name': c['name'], 'namespace': c['namespace'], 'labels': labels},
         'spec': {'replicas': 1, 'selector': {'matchLabels': {'app': c['name']}},
                  'template': {'metadata': {'labels': labels}, 'spec': {
                      'automountServiceAccountToken': False,
                      'containers': [{'name': 'operator', 'image': c['image'], 'env': env,
                          'ports': [{'name': 'http', 'containerPort': 8000}],
                          'readinessProbe': {'httpGet': {'path': '/health', 'port': 'http'},
                                             'periodSeconds': 3},
                          'securityContext': {'runAsNonRoot': True,
                              'allowPrivilegeEscalation': False,
                              'readOnlyRootFilesystem': True,
                              'capabilities': {'drop': ['ALL']}},
                          'resources': {'requests': {'cpu': '25m', 'memory': '64Mi'},
                                        'limits': {'cpu': '500m', 'memory': '256Mi'}}}]}}}},
        {'apiVersion': 'v1', 'kind': 'Service',
         'metadata': {'name': c['name'], 'namespace': c['namespace'], 'labels': labels},
         'spec': {'selector': {'app': c['name']},
                  'ports': [{'name': 'http', 'port': 8000, 'targetPort': 'http'}]}},
        {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
         'metadata': {'name': c['name'], 'namespace': c['namespace'], 'labels': labels},
         'spec': {'podSelector': {'matchLabels': {'app': c['name']}},
                  'policyTypes': ['Ingress'],
                  'ingress': [{'from': [{'podSelector': {'matchLabels': {
                      'woow.io/owner-uid': c['owner_uid']}}}],
                               'ports': [{'protocol': 'TCP', 'port': 8000}]}]}}
    ]
    return {'apiVersion': 'v1', 'kind': 'List', 'items': items,
            'x-fault02': {'apply': 'owned-namespace-only-after-ops-release',
                          'provider_resources_created': False}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('config')
    args = parser.parse_args()
    with open(args.config, encoding='utf-8') as stream:
        json.dump(render(json.load(stream)), sys.stdout, indent=2)
    sys.stdout.write('\n')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Render the exclusive llmdev test command OFFLINE. Never contacts a cluster.

After Elmo explicitly grants exclusivity, verify the pod source is the reviewed
commit, stop other acceptance jobs, then run the rendered command. Odoo module
upgrade is included. Capture the full log and collection/skip/error evidence.
No rendered output is evidence that tests executed. No secret discovery.
"""
import argparse
import shlex


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deployment', required=True, help='Previously verified llmdev Odoo deployment')
    parser.add_argument('--database', required=True, help='Previously verified exclusive sandbox database')
    args = parser.parse_args()
    selectors = ','.join('/woow_paas_platform:' + name for name in (
        'TestLlmCreditAttemptCommitted', 'TestLlmCreditWorkerCommitted',
        'TestLlmCreditAccountingCommitted', 'TestLlmCreditOrderSchema', 'TestLlmCreditRollback',
        'TestLlmCreditVoid'))
    command = ['kubectl', '--context', 'woow-k3s', '-n', 'dev-sandbox-llmdev',
               'exec', 'deploy/' + args.deployment, '--', 'odoo',
               '-c', '/etc/odoo/odoo.conf', '-d', args.database,
               '-u', 'woow_paas_platform', '--test-enable', '--test-tags', selectors,
               '--stop-after-init', '--http-port=18069', '--gevent-port=18072',
               '--max-cron-threads=0', '--log-level=test']
    print('PREPARED ONLY — wait for Elmo exclusive grant; verify exact pod source SHA first.')
    print(shlex.join(command))


if __name__ == '__main__':
    main()

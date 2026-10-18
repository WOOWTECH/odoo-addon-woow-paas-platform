#!/usr/bin/env python3
"""Compare sanitized stg pricing/BFF/options exports without credentials or network calls.

Usage: python scripts/audit/check-llm-pricing.py --pricing pricing.json --models models.json
           --options pricing-options.json --expected-paid 27
The options file only needs ModelRatio and CompletionRatio (the get-options projection).
Do not export credentials. Output contains counts and public model IDs, never option values.
"""
import argparse
from decimal import Decimal
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('llm_pricing', ROOT / 'src/services/llm_pricing.py')
pricing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pricing)


def read(path):
    return json.loads(Path(path).read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('pricing', 'models', 'options'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--expected-paid', type=int, default=27)
    args = parser.parse_args()
    upstream, public, options = read(args.pricing), read(args.models), read(args.options)
    public = public.get('result', public)
    public = public.get('data', public) if isinstance(public, dict) else public
    if not isinstance(options, dict):
        parser.error('options must be a JSON object containing ModelRatio and CompletionRatio')
    ratios = options.get('ModelRatio')
    ratios = json.loads(ratios) if isinstance(ratios, str) else ratios
    completions = options.get('CompletionRatio')
    completions = json.loads(completions) if isinstance(completions, str) else completions
    if not isinstance(ratios, dict) or not isinstance(completions, dict):
        parser.error('options must contain ModelRatio and CompletionRatio objects')
    expected = pricing.normalize_pricing(upstream)
    failures = []
    if public.get('state') != 'ok' or public.get('items') != expected['items']:
        failures.append('BFF catalogue differs from normalized upstream pricing')
    paid = [item for item in expected['items'] if item['group'] == 'default']
    free = [item for item in expected['items'] if item['group'] == 'free']
    if len(paid) != args.expected_paid or {item['model'] for item in free} != pricing.FREE_MODELS:
        failures.append('catalogue model count/set differs from the approved plan')
    for item in paid:
        name = item['model']
        if item['pricing_mode'] != 'ratio' or name not in ratios or name not in completions:
            failures.append('missing ratio tariff/options: ' + name)
            continue
        expected_input = Decimal('83.2') * Decimal(str(ratios[name]))
        if (Decimal(item['price']['input']) != expected_input
                or Decimal(item['price']['output']) != expected_input * Decimal(str(completions[name]))):
            failures.append('option tariff mismatch: ' + name)
    for failure in failures:
        print('FAIL:', failure)
    print('Pricing comparison: %d paid, %d free, %d failures' % (len(paid), len(free), len(failures)))
    return bool(failures)


if __name__ == '__main__':
    raise SystemExit(main())

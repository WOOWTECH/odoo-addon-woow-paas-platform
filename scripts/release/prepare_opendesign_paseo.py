#!/usr/bin/env python3
"""Emit an offline product-default patch after the coordinator supplies an image.

Does not edit source, contact Odoo/registries, or deploy anything. Apply the
reviewed output to this checkout, run seed/Odoo tests, then follow normal release
coordination. A digest's syntax is not proof of runtime compatibility.
"""
import argparse
import difflib
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]
SEED = 'src/data/cloud_app_templates.xml'
TEST = 'src/tests/test_opendesign_seed_380.py'
XMLID = 'cloud_app_opendesign'
CHART_VERSION = '0.5.10'
IMAGE_REPOSITORY = 'jcr-prod.woowtech.io/woow-paas-docker-local/open-design'
IMAGE_TAG = re.compile(r'[0-9]{8}\.[0-9]{4}@sha256:[0-9a-f]{64}\Z')


def promoted_seed(source, image_tag):
    """Change only the existing product's chart/image/features, preserving XML."""
    if not isinstance(image_tag, str) or not IMAGE_TAG.fullmatch(image_tag):
        raise ValueError('AWAITING_COORDINATOR_DIGEST: supply the reviewed PaaS YYYYMMDD.HHMM@sha256:<64 hex> tag.')
    root = ET.fromstring(source)
    records = root.findall(f".//record[@id='{XMLID}']")
    if len(records) != 1 or records[0].get('model') != 'woow_paas_platform.cloud_app_template':
        raise ValueError('Expected exactly one Open Design product seed.')
    fields = {f.get('name'): f.text for f in records[0].findall('field')}
    if fields['helm_chart_version'] not in ('0.5.7', CHART_VERSION):
        raise ValueError('Product chart changed; re-review the promotion base.')
    values = json.loads(fields['helm_default_values'])
    if values['image']['repository'] != IMAGE_REPOSITORY:
        raise ValueError('Product image repository changed; re-review the promotion base.')
    if image_tag == values['image']['tag'] and fields['helm_chart_version'] == '0.5.7':
        raise ValueError('The old product image cannot enable native Paseo.')
    values['image']['tag'] = image_tag
    # A standalone digest would shadow the tag@digest reference in some charts.
    values['image'].pop('digest', None)
    values['paseo'] = {'enabled': True, 'origin': ''}
    switches = json.loads(fields['rollback_feature_switches'])
    switches['paseo.enabled'] = ['image']
    replacements = {
        'helm_chart_version': CHART_VERSION,
        'helm_default_values': json.dumps(values, ensure_ascii=False),
        'rollback_feature_switches': json.dumps(switches, ensure_ascii=False),
    }
    pattern = re.compile(r'(<record id="cloud_app_opendesign"[^>]*>)(.*?)(</record>)', re.S)
    match = pattern.search(source)
    if not match:
        raise ValueError('Open Design seed layout changed; re-review before patching.')
    record = match.group(0)
    for name, value in replacements.items():
        field = re.compile(r'(<field name="' + name + r'">).*?(</field>)', re.S)
        record, count = field.subn(lambda m: m[1] + value + m[2], record)
        if count != 1:
            raise ValueError(f'Expected one {name} field.')
    return source[:match.start()] + record + source[match.end():]


def promotion_patch(root, image_tag):
    source = (root / SEED).read_text()
    seed = promoted_seed(source, image_tag)
    tests = (root / TEST).read_text()
    old = "self.assertEqual(tpl.helm_chart_version, '0.5.7')"
    new = (f"self.assertEqual(tpl.helm_chart_version, '{CHART_VERSION}')\n"
           "        self.assertEqual(self._values()['paseo'], {'enabled': True, 'origin': ''})\n"
           "        self.assertEqual(json.loads(tpl.rollback_feature_switches)['paseo.enabled'], ['image'])")
    if tests.count(old) == 1:
        updated_tests = tests.replace(old, new)
    elif tests.count(new) == 1:
        updated_tests = tests
    else:
        raise ValueError('Seed chart test changed; re-review before patching.')
    return ''.join(''.join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile='a/' + name, tofile='b/' + name))
        for name, before, after in [(SEED, source, seed), (TEST, tests, updated_tests)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image-tag', help='Reviewed compatible PaaS timestamp@sha256 digest (required to emit a patch).')
    args = parser.parse_args()
    try:
        result = promotion_patch(ROOT, args.image_tag)
    except (ValueError, KeyError) as exc:
        parser.exit(2, str(exc) + '\n')
    sys.stdout.write(result)


if __name__ == '__main__':
    main()

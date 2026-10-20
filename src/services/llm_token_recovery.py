"""Pure proof and scheduling rules for bounded New API token recovery."""
import hashlib
import json


def valid_page(items, total, page, page_size, max_id):
    return (isinstance(items, list) and type(total) is int and total >= 0
            and type(page) is int and page >= 1
            and type(page_size) is int and page_size > 0
            and (page == 1 or (page - 1) * page_size < total)
            and len(items) == min(page_size, max(0, total - (page - 1) * page_size))
            and all(isinstance(item, dict) and isinstance(item.get('name'), str)
                    and type(item.get('id')) is int and 0 < item['id'] <= max_id
                    for item in items)
            and len({item['id'] for item in items}) == len(items))


def page_fingerprint(items):
    return hashlib.sha256(json.dumps(
        [(item['id'], item['name']) for item in items],
        separators=(',', ':'), ensure_ascii=True).encode('ascii')).hexdigest()


def next_action(*, scan_required, scan_complete, pending, prefer_scan, now):
    """Alternate discovery with per-token retries; a retrying ID cannot block scans."""
    due = any(entry.get('retry_at', 0) <= now for entry in pending.values())
    if scan_required and not scan_complete and (prefer_scan or not due):
        return 'scan'
    if due:
        return 'disable'
    if scan_required and not scan_complete:
        return 'scan'
    return 'wait'


def head_matches(*, total, baseline_total, head, verify_items, verify_total,
                 page_size, max_id):
    return (total == baseline_total and verify_total == total
            and valid_page(verify_items, verify_total, 1, page_size, max_id)
            and page_fingerprint(verify_items) == head)


def stable_scan_complete(*, pending, disabled, total, baseline_total, head,
                         verify_items, verify_total, page_size, max_id):
    """Accept a scan only after every ID is settled and its page-one proof agrees."""
    return (not pending and bool(disabled) and head_matches(
        total=total, baseline_total=baseline_total, head=head,
        verify_items=verify_items, verify_total=verify_total,
        page_size=page_size, max_id=max_id))

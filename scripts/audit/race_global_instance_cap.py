#!/usr/bin/env python3
"""全域實例上限（#501）的**真並發**驗證——單元測試量不到的那一半。

## 為什麼需要這支腳本

`test_global_instance_cap_501.py` 與 `test_concurrency_error_propagation_518.py`
都是單連線的，**構造不出並發**。而上限的並發正確性不是靠「取鎖後重新計數」成立的
——Odoo 的 cursor 是 REPEATABLE READ（`odoo/sql_db.py` 的
`set_isolation_level(ISOLATION_LEVEL_REPEATABLE_READ)`），取鎖不會刷新 snapshot，
鎖住了也數不到並發那一筆。

實際成立的鏈是三環：

    1. `_claim_global_cap_slots` 對 template row 做 UPDATE（寫衝突列）
    2. 並發的第二者撞到寫寫衝突 → PostgreSQL SQLSTATE 40001（SerializationFailure）
    3. controller 重拋 OperationalError（#518）→ Odoo `retrying()` 以**新交易、新
       snapshot** 重跑 → 這時才數得到贏家

三環缺一，上限在並發下就等於沒有。**這支腳本是那條鏈的唯一端到端證據**，合併後
若有人動到其中任一環，重跑它才知道有沒有斷。

## 怎麼跑

需要一個**跑著的** Odoo server（不是 `--stop-after-init`），因為要並發打 HTTP。

    # 1. 起 server（範例為本機 docker 容器；port 依環境調整）
    odoo -d <db> --db_host <host> --db_user odoo --db_password odoo \
         --addons-path=/opt/bitnami/odoo/lib/odoo/addons,/opt/bitnami/odoo/custom-addons \
         --data-dir=/tmp/odoo-data --http-port=8069

    # 2. 跑本腳本
    python3 scripts/audit/race_global_instance_cap.py \
        --url http://localhost:8069 --db <db> --user admin --password admin

## 預期輸出

    [setup]   template=<id> cap=1 workspace=<id> plan=<id>
    [race]    5 concurrent create requests …
    [result]  services created: 1  (expected 1)
    [result]  HTTP 200 with success=true : 1
    [result]  JSON-RPC error (retried & rejected): 4
    PASS: 上限守住，落庫 1 台

**鑑別項（必看）**：把 `_claim_global_cap_slots` 的 UPDATE 註解掉再跑一次，必須看到

    [result]  services created: 5  (expected 1)
    FAIL: 超賣 4 台

只跑一次看到 PASS 不算數——**沒有這個對照，PASS 同樣相容於「並發根本沒發生」**
（例如 server 單工處理、或請求其實是串列送出的）。腳本本身無法分辨這兩者，所以
把對照列為必要步驟而不是選配。

## ⚠ 本檔的驗證狀態（2026-08-02）

原始的一次性腳本（PR #517 描述中「cap=1、5 個同時下單：停用衝突列落 5 列＝超賣 4 台，
啟用後落 1 列」那次實測）**沒有進版控就遺失了**，這支是依同一設計重建的。

重建版**已驗**：Python 語法、CLI 參數解析。
重建版**尚未驗**：端到端實跑（未在真 server 上跑過，PASS/FAIL 判定路徑未經執行）。

⇒ **第一次使用時請連同上面的「鑑別項」一起跑**（先跑一次看 PASS，再註解掉
`_claim_global_cap_slots` 的 UPDATE 看 FAIL）。若腳本本身有 bug，鑑別項那一步會
暴露它——兩次都 PASS 或兩次都 FAIL 都代表腳本壞了，不是被測物的結論。

## 刻意不做的事

不併進自動化測試套件：它需要真 server、真並發，且成功條件依賴時序，放進 CI 會變成
flaky 來源。寧可維持「手動、但有明確 recipe 與鑑別項」也不要一條會偶發紅的守線。
"""
import argparse
import json
import sys
import threading
import urllib.request


def _rpc(url, db, session, path, params):
    """打一個 JSON-RPC 呼叫，回 (http_status, parsed_json)。"""
    req = urllib.request.Request(
        url.rstrip('/') + path,
        data=json.dumps({'jsonrpc': '2.0', 'method': 'call',
                         'id': 1, 'params': params}).encode(),
        headers={'Content-Type': 'application/json',
                 'Cookie': 'session_id=%s' % session} if session else
                {'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, {'_body': exc.read()[:200].decode('utf-8', 'replace')}


def _login(url, db, user, password):
    req = urllib.request.Request(
        url.rstrip('/') + '/web/session/authenticate',
        data=json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': {
            'db': db, 'login': user, 'password': password}}).encode(),
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req) as resp:
        cookie = resp.headers.get('Set-Cookie') or ''
        body = json.loads(resp.read())
    if body.get('result', {}).get('uid') is None:
        sys.exit('登入失敗：%s' % body)
    for part in cookie.split(';'):
        if part.strip().startswith('session_id='):
            return part.strip().split('=', 1)[1]
    sys.exit('登入成功但拿不到 session_id cookie')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--url', required=True)
    ap.add_argument('--db', required=True)
    ap.add_argument('--user', default='admin')
    ap.add_argument('--password', default='admin')
    ap.add_argument('--workspace', type=int, required=True,
                    help='既有 workspace id（腳本不建 workspace，避免副作用）')
    ap.add_argument('--template', type=int, required=True,
                    help='限量 template id；其 cap 需先設為 1')
    ap.add_argument('--plan', type=int, required=True)
    ap.add_argument('--concurrency', type=int, default=5)
    args = ap.parse_args()

    session = _login(args.url, args.db, args.user, args.password)
    print('[setup]   template=%s workspace=%s plan=%s concurrency=%d'
          % (args.template, args.workspace, args.plan, args.concurrency))

    results = []
    barrier = threading.Barrier(args.concurrency)

    def fire(i):
        # barrier 讓所有 thread 在同一瞬間送出——沒有它，請求會被自然錯開，
        # 「沒有超賣」就無法區分「守衛有效」與「並發根本沒發生」。
        barrier.wait()
        status, body = _rpc(
            args.url, args.db, session,
            '/api/workspaces/%s/services' % args.workspace,
            {'action': 'create', 'template_id': args.template,
             'name': 'race-%d' % i, 'plan_id': args.plan})
        results.append((status, body))

    print('[race]    %d concurrent create requests …' % args.concurrency)
    threads = [threading.Thread(target=fire, args=(i,))
               for i in range(args.concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok = sum(1 for _, b in results if isinstance(b, dict)
             and (b.get('result') or {}).get('success') is True)
    rpc_err = sum(1 for _, b in results if isinstance(b, dict) and 'error' in b)
    rejected = len(results) - ok - rpc_err

    print('[result]  HTTP 200 with success=true : %d' % ok)
    print('[result]  JSON-RPC error             : %d' % rpc_err)
    print('[result]  rejected (success=false)   : %d' % rejected)
    print('[result]  services created           : %d  (expected 1)' % ok)

    if ok == 1:
        print('PASS: 上限守住，落庫 1 台')
        print('⚠ 只跑這一次不算數——請依 module docstring 的「鑑別項」把 '
              '_claim_global_cap_slots 的 UPDATE 註解掉重跑，必須看到超賣。')
        return 0
    print('FAIL: 超賣 %d 台' % (ok - 1) if ok > 1 else 'FAIL: 一台都沒建成（檢查 fixture）')
    return 1


if __name__ == '__main__':
    sys.exit(main())

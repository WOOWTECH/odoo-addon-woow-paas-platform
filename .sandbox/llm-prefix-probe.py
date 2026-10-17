"""經 newapi-proxy 以 AP 查「<前綴>0000000001」在 new-api-stg 是否已存在（LLM-01b finding 6）。

在**沙盒 Odoo pod 裡**跑（只有那裡的 NetworkPolicy 放行 new-api-stg 的代理）；post-create 這樣叫，
本檔與簽章用的 ``extra/newapi-proxy/scripts/signed_request.py`` 都不落地::

    kubectl exec -i -n <ns> deploy/<odoo> -- python3 -c "$(cat .sandbox/llm-prefix-probe.py)" \\
        http://newapi-proxy.new-api-stg.svc.cluster.local:8080 wsab \\
        < extra/newapi-proxy/scripts/signed_request.py

* 簽章金鑰讀 pod 自己的 env ``WOOW_NEWAPI_PROXY_AUTH_CRON``（signed_request.py 的預設），不經 argv。
* stdin 是 signed_request.py 的原始碼：這裡 compile 成一個模組命名空間再呼叫它的 ``main()``
  ——簽章只有那一份實作（它和代理的 ``signing.py`` 由代理的測試鎖在一起）。
* 輸出一行：``free``／``taken``／``error <原因碼>``；不印任何 body、標頭或金鑰。
* 成本：AP（198.18.0.1）的 GA 1 格；不吃租戶的 CT。
"""
import contextlib
import io
import json
import re
import sys


def _verdict(argv, source):
    if len(argv) != 2 or not re.fullmatch(r'ws[a-z]{2}', argv[1]) \
            or not re.fullmatch(r'http://[a-z0-9.-]+(:[0-9]{1,5})?', argv[0]):
        return 'error usage'
    proxy, prefix = argv
    username = prefix + '0000000001'
    namespace = {'__name__': 'woow_signed_request'}
    exec(compile(source, 'signed_request.py', 'exec'), namespace)  # noqa: S102 - our own file
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            namespace['main'](['--identity', 'ap', '--timeout', '15', 'GET',
                               '%s/api/user/search?keyword=%s&p=1&page_size=10'
                               % (proxy, username)])
    except BaseException as exc:  # noqa: BLE001 - SystemExit from argparse / missing env
        return 'error %s' % type(exc).__name__
    try:
        line = json.loads(out.getvalue().strip().splitlines()[-1])
    except (ValueError, IndexError):
        return 'error output'
    if line.get('status') != 200:
        return 'error status_%s_%s' % (line.get('status'), line.get('proxy_error') or '-')
    try:
        body = json.loads(line.get('body') or '')
    except ValueError:
        return 'error body'
    if not isinstance(body, dict) or body.get('success') is not True:
        return 'error not_success'
    data = body.get('data')
    items = data.get('items') if isinstance(data, dict) else data
    if not isinstance(items, list):
        return 'error shape'
    taken = any(isinstance(item, dict) and item.get('username') == username for item in items)
    return 'taken' if taken else 'free'


print(_verdict(sys.argv[1:], sys.stdin.read()))

"""omni-msg CLI；ask 立即回 request ID，reply 必須顯式配對。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import time
import uuid
from pathlib import Path

from .bridge import Bridge, Store, validate_actors
from .registration import Registrar, poll_seconds, validate_roots
from .server import BridgeServer
from .transport import (
    MAX_BYTES,
    MessageError,
    OmnigentTransport,
    decode_json,
    load_private_json,
    private_file,
    request_json,
    validate_url,
)


def dump(value: dict, *, stream=sys.stdout):
    print(json.dumps(value, ensure_ascii=False), file=stream, flush=True)


def port_number(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port 必須介於 1..65535")
    return port


def save_private(path: Path, value: dict):
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def provision(args):
    """Operator 一次建立固定 registry 與各 actor 的獨立 credential。"""
    url = validate_url(args.server_url)
    token_path = Path(args.token_file).absolute()
    private_file(token_path)
    if args.roots:
        roots = validate_roots(decode_json(Path(args.roots).read_text(encoding="utf-8")))
        directory = Path(args.directory).absolute()
        try:
            directory.mkdir(mode=0o700)
        except OSError as exc:
            raise MessageError("new_private_directory_required") from exc
        save_private(directory / "server.json", {
            "server_url": url, "token_file": str(token_path),
            "registration": {"roots": roots, "poll_seconds": args.poll_seconds},
        })
        dump({"server_config": str(directory / "server.json"),
              "notice": "serve 自動核對 descendants；各 client config 留在 state-dir/clients，狀態用 registrations 查詢。"})
        return
    try:
        registry = decode_json(Path(args.registry).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MessageError("invalid_registry") from exc
    if not isinstance(registry, dict) or not registry:
        raise MessageError("invalid_registry")
    actors, tokens = {}, {}
    for name, entry in registry.items():
        if not isinstance(entry, dict) or set(entry) != {"session_id", "allow"}:
            raise MessageError("invalid_registry_entry")
        token = secrets.token_urlsafe(32)
        tokens[name] = token
        actors[name] = {**entry, "token_sha256": hashlib.sha256(token.encode()).hexdigest()}
    validate_actors(actors)
    directory = Path(args.directory).absolute()
    try:
        directory.mkdir(mode=0o700)
    except OSError as exc:
        raise MessageError("new_private_directory_required") from exc
    save_private(directory / "server.json", {
        "server_url": url, "token_file": str(token_path), "actors": actors,
    })
    clients = directory / "clients"
    clients.mkdir(mode=0o700)
    for name, token in tokens.items():
        save_private(clients / f"{name}.json", {
            "bridge_url": f"http://127.0.0.1:{args.port}", "token": token,
        })
    dump({"server_config": str(directory / "server.json"), "client_configs": str(clients),
          "notice": "僅交付對應 actor 的 client config；整個目錄與上游 token 留在 operator 端。"})


def serve(args):
    config = load_private_json(args.server_config)
    automatic = set(config) == {"server_url", "token_file", "registration"}
    if automatic:
        options = config["registration"]
        if not isinstance(options, dict) or set(options) != {"roots", "poll_seconds"}:
            raise MessageError("invalid_registration_config")
        validate_roots(options["roots"])
        poll_seconds(options["poll_seconds"])
        actors = {}
    elif set(config) == {"server_url", "token_file", "actors"}:
        actors = validate_actors(config["actors"])
    else:
        raise MessageError("invalid_server_config")
    transport = OmnigentTransport(config["server_url"], config["token_file"])
    store = Store(Path(args.state_dir) / "messages.sqlite3", actors)
    registrar = None
    try:
        bridge = Bridge(actors, store, transport, automatic=automatic)
        if automatic:
            registrar = Registrar(options["roots"], options["poll_seconds"], bridge, args.state_dir, args.port)
        with BridgeServer(("127.0.0.1", args.port), bridge) as server:
            dump({"listening": f"http://127.0.0.1:{args.port}"}, stream=sys.stderr)
            if registrar:
                registrar.start()
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                if registrar:
                    registrar.close()
    finally:
        store.close()


def registrations(args):
    status = decode_json(private_file(Path(args.state_dir) / "registrations.json", max_bytes=2 * 1024 * 1024))
    if not isinstance(status, dict):
        raise MessageError("invalid_registration_status")
    if status.get("ready") and (status.get("expires_at") or 0) <= time.time():
        status.update(ready=False, error="registration_expired", members=[])
    dump(status)


def client(args) -> int:
    if not args.config:
        raise MessageError("set_OMNI_MSG_CONFIG_or_config")
    config = load_private_json(args.config)
    if set(config) != {"bridge_url", "token"}:
        raise MessageError("invalid_client_config")
    url = validate_url(config["bridge_url"], loopback=True)
    token = config["token"]
    if not isinstance(token, str) or len(token) < 32 or any(c.isspace() for c in token):
        raise MessageError("invalid_client_token")
    if args.command == "list":
        result = request_json(f"{url}/v1/contacts", token)
    elif args.command == "check":
        from .bridge import message_id
        identifier = message_id(args.request_id)
        result = request_json(f"{url}/v1/messages/{identifier}", token)
    else:
        identifier = args.id or str(uuid.uuid4())
        text = args.text
        if text == "-":
            text = sys.stdin.read(MAX_BYTES + 1)
        # 先把 ID 留給 caller：HTTP timeout 後能 check，不能換 ID 盲目重送。
        dump({"request_id": identifier}, stream=sys.stderr)
        result = request_json(f"{url}/v1/messages", token, timeout=25, payload={
            "id": identifier, "kind": args.command, "target": args.target, "text": text,
        })
    dump(result)
    return 3 if result.get("delivery") in {"unknown", "rejected"} else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=os.environ.get("OMNI_MSG_CONFIG"),
                        help="目前 actor 的 private client config（或 OMNI_MSG_CONFIG）")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="列出允許聯絡的 session aliases")
    for command in ("send", "ask", "reply"):
        sub = commands.add_parser(command, help="非同步發訊／提問／回覆")
        sub.add_argument("target", help="session alias；reply 則為原 ask 的 request ID")
        sub.add_argument("text", help="訊息文字；- 表示從 stdin 讀取")
        sub.add_argument("--id", help="重送同一 HTTP request 時沿用的 UUID（不會再次投遞）")
    check = commands.add_parser("check", help="讀取 receipt／明確 reply；不觸發新 turn")
    check.add_argument("request_id")
    setup = commands.add_parser("provision", help="Operator：建立 registry 與各 actor credential")
    setup.add_argument("--directory", required=True, help="必須是尚不存在的 private 目錄")
    source = setup.add_mutually_exclusive_group(required=True)
    source.add_argument("--registry", help="人工模式：alias → session_id／allow 的 JSON 檔")
    source.add_argument("--roots", help="自動模式：root alias → session_id／workspace 的 operator allowlist")
    setup.add_argument("--poll-seconds", type=poll_seconds, default=5, help="自動 discovery 間隔（0.1..60，預設 5 秒）")
    setup.add_argument("--server-url", required=True, help="Omnigent server origin，不含 /v1")
    setup.add_argument("--token-file", required=True, help="上游專用帳號 bearer token（0600）")
    setup.add_argument("--port", type=port_number, default=6778)
    server = commands.add_parser("serve", help="Operator：啟動 loopback bridge")
    server.add_argument("--server-config", required=True)
    server.add_argument("--state-dir", required=True, help="private 0700 ledger 目錄")
    server.add_argument("--port", type=port_number, default=6778)
    status = commands.add_parser("registrations", help="Operator：讀取 registrar 狀態；不發送訊息")
    status.add_argument("--state-dir", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "provision":
            provision(args)
        elif args.command == "serve":
            serve(args)
        elif args.command == "registrations":
            registrations(args)
        else:
            return client(args)
        return 0
    except MessageError as exc:
        dump({"error": exc.code}, stream=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError):
        dump({"error": "local_config_or_io_error"}, stream=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

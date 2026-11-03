"""Operator CLI for developer pairing, listing and revocation."""

import argparse
import json
import os
from pathlib import Path

from fullish.management.identity import DeveloperIdentity, fingerprint
from fullish.management.store import ControlStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["pair", "developers", "revoke"])
    parser.add_argument("--user-ref")
    args = parser.parse_args()
    directory = os.environ.get("GROUP_CONTROL_DIR")
    if not directory:
        raise SystemExit("GROUP_CONTROL_DIR is not configured")
    identity = DeveloperIdentity(ControlStore(Path(directory)))
    if args.command == "pair":
        print("私聊机器人发送（10 分钟有效，仅使用一次）：\n绑定开发者 " + identity.pair())
    elif args.command == "revoke":
        if not args.user_ref:
            raise SystemExit("--user-ref is required")
        identity.revoke(args.user_ref)
    else:
        print(json.dumps([fingerprint(user) for user in identity.developers()]))


if __name__ == "__main__":
    main()

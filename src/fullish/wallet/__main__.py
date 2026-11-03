"""Create or inspect the local Fullish wallet without starting the bot."""

import argparse
import json
import subprocess
from pathlib import Path

from eth_utils.address import to_checksum_address

from fullish.wallet.service import CHAIN_ID, create_wallet, read_private


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or inspect Fullish's private BSC wallet")
    parser.add_argument("action", choices=["create", "address"])
    parser.add_argument("--directory", type=Path, default=Path(".state/wallet"))
    args = parser.parse_args()
    try:
        if args.action == "create":
            result = create_wallet(args.directory)
        else:
            key = json.loads(read_private(args.directory / "keystore.json"))
            result = {
                "name": "Fullish",
                "chain_id": CHAIN_ID,
                "address": to_checksum_address(key["address"]),
            }
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        parser.exit(
            1,
            "Wallet operation failed; check private files/permissions. "
            "Existing keys were not replaced.\n",
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

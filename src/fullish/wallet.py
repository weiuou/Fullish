"""Fullish's BSC hot wallet: local signing and durable, event-bound payments."""

import argparse
import asyncio
import json
import os
import re
import secrets
import sqlite3
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path

import httpx
from eth_account import Account
from eth_utils.address import is_address, to_checksum_address
from eth_utils.crypto import keccak

from fullish.settings import WalletSettings
from fullish.tools.web import ToolError

CHAIN_ID = 56
ZERO_ADDRESS = "0x" + "0" * 40


def private_directory(directory: Path) -> None:
    if directory.is_symlink():
        raise ValueError("Wallet directory must not be a symlink")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "nt":
        # chmod alone does not protect files on Windows. Restrict the NTFS ACL
        # before writing any secret; new files inherit this directory's ACL.
        subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ErrorActionPreference = 'Stop'; "
                "$walletSid = "
                "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
                "$walletAcl = New-Object System.Security.AccessControl.DirectorySecurity; "
                "$walletAcl.SetSecurityDescriptorSddlForm("
                '"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;$walletSid)", '
                "[System.Security.AccessControl.AccessControlSections]::Access); "
                "[System.IO.Directory]::SetAccessControl("
                "$env:FULLISH_PRIVATE_WALLET_DIR, $walletAcl)",
            ],
            check=True,
            capture_output=True,
            env={**os.environ, "FULLISH_PRIVATE_WALLET_DIR": str(directory.resolve())},
        )
    else:
        directory.chmod(0o700)


def write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())


def read_private(path: Path) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 65536:
        raise ValueError("Invalid wallet file")
    if os.name != "nt" and info.st_mode & 0o077:
        raise ValueError("Wallet files must have mode 0600")
    return path.read_text(encoding="utf-8")


def create_wallet(directory: Path) -> dict:
    """Create once, never silently replace a missing or damaged existing key."""
    private_directory(directory)
    if any(directory.iterdir()):
        raise ValueError("Wallet directory is not empty; existing wallets are never overwritten")
    password = secrets.token_urlsafe(48)
    account = Account.create()
    encrypted = account.encrypt(password)
    write_private(directory / "unlock.secret", password)
    write_private(directory / "keystore.json", json.dumps(encrypted))
    public = {
        "name": "Fullish",
        "chain": "BSC",
        "chain_id": CHAIN_ID,
        "address": account.address,
        "explorer": "https://bscscan.com/address/" + account.address,
    }
    write_private(directory / "wallet.json", json.dumps(public, indent=2) + "\n")
    return public


def recipient(value: str) -> str:
    if not is_address(value) or value.lower() == ZERO_ADDRESS:
        raise ToolError("wallet_invalid_address")
    # Lowercase addresses are accepted; mixed-case ones must have a valid checksum.
    address = to_checksum_address(value)
    body = value.removeprefix("0x")
    if not (body.islower() or body.isupper() or body.isdigit()) and value != address:
        raise ToolError("wallet_invalid_address_checksum")
    return address


def units(amount: str, decimals: int) -> int:
    if not re.fullmatch(r"(?:0|[1-9][0-9]{0,77})(?:\.[0-9]{1,36})?", amount):
        raise ToolError("wallet_invalid_amount")
    # String arithmetic avoids Decimal's default 28-digit rounding at 18 decimals.
    whole, _, fraction = amount.partition(".")
    if len(fraction.rstrip("0")) > decimals:
        raise ToolError("wallet_amount_precision")
    value = int(whole) * 10**decimals + int((fraction[:decimals]).ljust(decimals, "0") or "0")
    if not 0 < value < 2**256:
        raise ToolError("wallet_invalid_amount")
    return value


def display_units(value: int, decimals: int) -> str:
    whole, fraction = divmod(value, 10**decimals)
    return str(whole) + ("." + str(fraction).zfill(decimals).rstrip("0") if fraction else "")


class BSCWallet:
    def __init__(self, settings: WalletSettings, *, transport=None):
        self.settings = settings
        directory = settings.directory
        if directory.is_symlink() or (os.name != "nt" and directory.stat().st_mode & 0o077):
            raise ValueError("Wallet requires a private directory")
        try:
            self._account = Account.from_key(
                Account.decrypt(
                    read_private(directory / "keystore.json"),
                    read_private(directory / "unlock.secret"),
                )
            )
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError(
                "Cannot unlock Fullish wallet; restore the original wallet files"
            ) from None
        self.address = self._account.address
        self.path = directory / "transfers.sqlite3"
        if not self.path.exists():
            write_private(self.path, "")
        self._check_journal()
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS identity (address TEXT PRIMARY KEY)")
            identity = db.execute("SELECT address FROM identity").fetchone()
            if identity and identity["address"] != self.address:
                raise ValueError("Wallet key does not match its transfer journal")
            db.execute("INSERT OR IGNORE INTO identity(address) VALUES(?)", (self.address,))
            db.execute("""CREATE TABLE IF NOT EXISTS transfers (
                event_key TEXT PRIMARY KEY, request TEXT NOT NULL,
                tx_hash TEXT UNIQUE NOT NULL, raw TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
        self.lock = asyncio.Lock()
        self.client = httpx.AsyncClient(
            transport=transport,
            timeout=10,
            trust_env=False,
            follow_redirects=False,
        )

    def _check_journal(self) -> None:
        info = self.path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or (os.name != "nt" and info.st_mode & 0o077)
        ):
            raise ValueError("Invalid wallet journal")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=0)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    async def close(self) -> None:
        await self.client.aclose()

    async def rpc(self, method: str, params: list):
        # The model can never select an RPC endpoint or an arbitrary RPC method.
        try:
            async with self.client.stream(
                "POST",
                self.settings.rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            ) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 131072:
                        raise ToolError("wallet_invalid_rpc_response")
            result = json.loads(body)
            if not isinstance(result, dict) or result.get("error") or "result" not in result:
                raise ToolError("wallet_rpc_rejected")
            return result["result"]
        except (httpx.HTTPError, ValueError, TypeError):
            raise ToolError("wallet_rpc_unavailable") from None

    async def chain(self) -> None:
        if await self.rpc("eth_chainId", []) != "0x38":
            raise ToolError("wallet_wrong_chain")

    async def integer(self, method: str, params: list) -> int:
        value = await self.rpc(method, params)
        if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{1,64}", value):
            raise ToolError("wallet_invalid_rpc_response")
        return int(value, 16)

    async def token_decimals(self, token: str) -> int:
        decimals = await self.integer("eth_call", [{"to": token, "data": "0x313ce567"}, "latest"])
        if decimals > 36:
            raise ToolError("wallet_unsupported_token_decimals")
        return decimals

    async def info(self, token_address: str = "") -> dict:
        await self.chain()
        balance = await self.integer("eth_getBalance", [self.address, "latest"])
        result = {
            "name": "Fullish",
            "chain": "BSC",
            "chain_id": CHAIN_ID,
            "address": self.address,
            "bnb_balance": display_units(balance, 18),
            "explorer": "https://bscscan.com/address/" + self.address,
        }
        if token_address:
            token = recipient(token_address)
            decimals = await self.token_decimals(token)
            balance = await self.integer(
                "eth_call",
                [
                    {"to": token, "data": "0x70a08231" + self.address[2:].lower().zfill(64)},
                    "latest",
                ],
            )
            result["token"] = {
                "address": token,
                "decimals": decimals,
                "balance": display_units(balance, decimals),
            }
        with self.connection() as db:
            result["recent_transfers"] = [
                self.public(row)
                for row in db.execute("SELECT * FROM transfers ORDER BY rowid DESC LIMIT 5")
            ]
        return result

    @staticmethod
    def public(row) -> dict:
        return {
            "tx_hash": row["tx_hash"],
            "status": row["status"],
            "request": json.loads(row["request"]),
            "explorer": "https://bscscan.com/tx/" + row["tx_hash"],
        }

    async def receipt(self, tx_hash: str) -> str:
        data = await self.rpc("eth_getTransactionReceipt", [tx_hash])
        if data is None:
            return "pending"
        if (
            not isinstance(data, dict)
            or data.get("transactionHash", "").lower() != tx_hash.lower()
            or data.get("status") not in {"0x0", "0x1"}
        ):
            raise ToolError("wallet_invalid_rpc_response")
        return "confirmed" if data["status"] == "0x1" else "reverted"

    async def transaction(self, tx_hash: str) -> dict:
        if not re.fullmatch(r"0x[0-9a-fA-F]{64}", tx_hash):
            raise ToolError("wallet_invalid_tx_hash")
        await self.chain()
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM transfers WHERE tx_hash=?", (tx_hash.lower(),)
            ).fetchone()
        if row is None:
            raise ToolError("wallet_unknown_transaction")
        status = await self.receipt(row["tx_hash"])
        if status != "pending":
            with self.connection() as db:
                db.execute(
                    "UPDATE transfers SET status=? WHERE tx_hash=?", (status, row["tx_hash"])
                )
            return {**self.public(row), "status": status}
        return {**self.public(row), "status": "pending", "confirmed": False}

    async def transfer(
        self, event_key: str, to_address: str, amount: str, token_address: str = ""
    ) -> dict:
        if not token_address:
            raise ToolError("wallet_bnb_reserved_for_gas")
        to = recipient(to_address)
        token = recipient(token_address)
        # Validate before even opening a network connection; normalize equivalent amounts.
        units(amount, 36)
        normalized_amount = amount.rstrip("0").rstrip(".") if "." in amount else amount
        request = json.dumps(
            {"to_address": to, "amount": normalized_amount, "token_address": token}, sort_keys=True
        )
        async with self.lock:
            await self.chain()
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT * FROM transfers WHERE event_key=?", (event_key,)
                ).fetchone()
                if row and row["request"] != request:
                    raise ToolError("wallet_one_transfer_per_message")
                if row and row["status"] in {"confirmed", "reverted"}:
                    return self.public(row)
                if row is None:
                    # A pending payment reserves the nonce across processes and restarts.
                    for pending in db.execute(
                        "SELECT * FROM transfers WHERE status IN ('prepared', 'submitted')"
                    ):
                        status = await self.receipt(pending["tx_hash"])
                        if status == "pending":
                            raise ToolError("wallet_pending_transaction_check_status")
                        db.execute(
                            "UPDATE transfers SET status=? WHERE tx_hash=?",
                            (status, pending["tx_hash"]),
                        )
                    decimals = await self.token_decimals(token) if token else 18
                    value = units(amount, decimals)
                    tx = {
                        "from": self.address,
                        "to": token or to,
                        "value": hex(0 if token else value),
                    }
                    if token:
                        tx["data"] = (
                            "0xa9059cbb" + to[2:].lower().zfill(64) + hex(value)[2:].zfill(64)
                        )
                        available = await self.integer(
                            "eth_call",
                            [
                                {
                                    "to": token,
                                    "data": "0x70a08231" + self.address[2:].lower().zfill(64),
                                },
                                "pending",
                            ],
                        )
                        if value > available:
                            raise ToolError("wallet_insufficient_token_balance")
                        # Standard BEP-20 transfers return true (or no bytes for older tokens).
                        simulated = await self.rpc("eth_call", [tx, "pending"])
                        if simulated not in {"0x", "0x" + "0" * 63 + "1"}:
                            raise ToolError("wallet_token_transfer_rejected")
                    gas = await self.integer("eth_estimateGas", [tx])
                    gas = (gas * 12 + 9) // 10
                    gas_price = await self.integer("eth_gasPrice", [])
                    balance = await self.integer("eth_getBalance", [self.address, "pending"])
                    if balance < gas * gas_price + (0 if token else value):
                        raise ToolError("wallet_insufficient_bnb_for_amount_and_fee")
                    nonce = await self.integer("eth_getTransactionCount", [self.address, "pending"])
                    transaction = {
                        "chainId": CHAIN_ID,
                        "nonce": nonce,
                        "to": tx["to"],
                        "value": 0 if token else value,
                        "gas": gas,
                        "gasPrice": gas_price,
                    }
                    if token:
                        transaction["data"] = tx["data"]
                    signed = self._account.sign_transaction(transaction)
                    tx_hash = "0x" + keccak(signed.raw_transaction).hex()
                    db.execute(
                        "INSERT INTO transfers(event_key, request, tx_hash, raw, status) "
                        "VALUES(?,?,?,?, 'prepared')",
                        (event_key, request, tx_hash, "0x" + signed.raw_transaction.hex()),
                    )
                    row = db.execute(
                        "SELECT * FROM transfers WHERE event_key=?", (event_key,)
                    ).fetchone()
            # Signed bytes are fsynced by SQLite before any broadcast. Retrying the
            # same platform event can only rebroadcast these exact bytes/hash.
            assert row is not None
            status = await self.receipt(row["tx_hash"])
            if status != "pending":
                with self.connection() as db:
                    db.execute(
                        "UPDATE transfers SET status=? WHERE event_key=?", (status, event_key)
                    )
                return {**self.public(row), "status": status}
            try:
                returned = await self.rpc("eth_sendRawTransaction", [row["raw"]])
                if not isinstance(returned, str) or returned.lower() != row["tx_hash"]:
                    raise ToolError("wallet_broadcast_hash_mismatch")
            except ToolError:
                # A timeout can mean the chain received the transaction. Preserve
                # the hash and pending state; never build a replacement payment.
                return {
                    **self.public(row),
                    "status": "broadcast_unknown",
                    "hint": "查询此交易哈希；相同消息重试只会重发同一交易。不要新建付款。",
                }
            with self.connection() as db:
                db.execute(
                    "UPDATE transfers SET status='submitted' WHERE event_key=?", (event_key,)
                )
            return {**self.public(row), "status": "submitted", "confirmed": False}


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

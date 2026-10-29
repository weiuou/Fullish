"""Real local signatures and MCP messages; all blockchain traffic is simulated."""

import asyncio
import json
import shutil
from dataclasses import replace
from unittest.mock import Mock

import httpx
import pytest
import rlp
from eth_account import Account
from eth_utils.crypto import keccak

from fullish.group_policy import Features, GroupPolicies
from fullish.mcp_client import WalletMCPClient
from fullish.settings import ToolSettings, WalletSettings
from fullish.tools import ToolContext, ToolRuntime
from fullish.tools.web import ToolError
from fullish.wallet import BSCWallet, create_wallet, display_units, units, write_private
from fullish.wallet_mcp import create_server

GROUP = "g" * 32
DESTINATION = "0x" + "12" * 20
TOKEN = "0x" + "34" * 20
CONTEXT = ToolContext(GROUP, "member", "message")


@pytest.fixture(scope="module")
def material(tmp_path_factory):
    path = tmp_path_factory.mktemp("wallet-material")
    path.chmod(0o700)
    # Public, deterministic TEST key; lower-cost KDF only for test fixtures.
    account = Account.from_key(b"\x01" * 32)
    write_private(path / "keystore.json", json.dumps(account.encrypt("test", iterations=16384)))
    write_private(path / "unlock.secret", "test")
    write_private(path / "wallet.json", json.dumps({"address": account.address}))
    return path


@pytest.fixture
def settings(tmp_path, material):
    directory = tmp_path / "wallet"
    shutil.copytree(material, directory)
    directory.chmod(0o700)
    return WalletSettings(directory, frozenset({GROUP}))


class RPC:
    def __init__(self):
        self.chain = "0x38"
        self.balance = 100 * 10**18
        self.decimals = 18
        self.sent = []
        self.receipts = {}
        self.timeout = False
        self.calls = []

    def __call__(self, request):
        data = json.loads(request.content)
        method, params = data["method"], data["params"]
        self.calls.append((method, params))
        match method:
            case "eth_chainId":
                result = self.chain
            case "eth_getBalance":
                result = hex(self.balance)
            case "eth_getTransactionCount":
                result = hex(len(set(self.sent)))
            case "eth_gasPrice":
                result = hex(10**9)
            case "eth_estimateGas":
                result = hex(60000 if "data" in params[0] else 21000)
            case "eth_call":
                selector = params[0]["data"][:10]
                result = (
                    hex(self.decimals)
                    if selector == "0x313ce567"
                    else (
                        hex(1000 * 10**self.decimals)
                        if selector == "0x70a08231"
                        else "0x" + "0" * 63 + "1"
                    )
                )
            case "eth_getTransactionReceipt":
                result = self.receipts.get(params[0])
            case "eth_sendRawTransaction":
                self.sent.append(params[0])
                if self.timeout:
                    raise httpx.ReadTimeout("simulated after node accepted transaction")
                result = "0x" + keccak(bytes.fromhex(params[0][2:])).hex()
            case _:
                raise AssertionError(method)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


def open_wallet(settings, rpc):
    return BSCWallet(settings, transport=httpx.MockTransport(rpc))


def test_creation_does_not_overwrite_or_expose_key(tmp_path, capsys):
    directory = tmp_path / "new-wallet"
    public = create_wallet(directory)
    original = (directory / "keystore.json").read_bytes()
    assert public["chain_id"] == 56 and public["address"].startswith("0x")
    assert "private" not in json.dumps(public)
    assert capsys.readouterr().out == ""
    with pytest.raises(ValueError, match="never overwritten"):
        create_wallet(directory)
    assert (directory / "keystore.json").read_bytes() == original


def test_token_signature_restart_and_event_idempotency(settings):
    rpc = RPC()

    async def run():
        wallet = open_wallet(settings, rpc)
        first = await wallet.transfer("event", DESTINATION, "0.123456789012345678", TOKEN)
        raw = rpc.sent[0]
        assert Account.recover_transaction(raw) == wallet.address
        fields = rlp.decode(bytes.fromhex(raw[2:]))
        assert fields[3].hex() == TOKEN[2:]
        assert int.from_bytes(fields[4]) == 0
        assert (int.from_bytes(fields[6]) - 35) // 2 == 56
        await wallet.close()
        wallet = open_wallet(settings, rpc)
        try:
            second = await wallet.transfer("event", DESTINATION, "0.1234567890123456780", TOKEN)
            assert first["tx_hash"] == second["tx_hash"]
            assert len(set(rpc.sent)) == 1
            with pytest.raises(ToolError, match="one_transfer_per_message"):
                await wallet.transfer("event", DESTINATION, "0.2", TOKEN)
            with pytest.raises(ToolError, match="pending_transaction"):
                await wallet.transfer("new-event", DESTINATION, "0.1", TOKEN)
            rpc.receipts[first["tx_hash"]] = {"transactionHash": first["tx_hash"], "status": "0x1"}
            assert (await wallet.transaction(first["tx_hash"]))["status"] == "confirmed"
            await wallet.transfer("new-event", DESTINATION, "0.1", TOKEN)
            assert len(set(rpc.sent)) == 2
            public = json.dumps(await wallet.info())
            assert raw not in public and "unlock" not in public and "raw" not in public
        finally:
            await wallet.close()

    asyncio.run(run())


def test_timeout_preserves_signed_payment_and_concurrent_retry(settings):
    rpc = RPC()
    rpc.timeout = True

    async def run():
        wallet = open_wallet(settings, rpc)
        first = await wallet.transfer("event", DESTINATION, "0.1", TOKEN)
        assert first["status"] == "broadcast_unknown"
        await wallet.close()
        rpc.timeout = False
        wallet = open_wallet(settings, rpc)
        try:
            results = await asyncio.gather(
                *[wallet.transfer("event", DESTINATION, "0.1", TOKEN) for _ in range(2)]
            )
            assert {x["tx_hash"] for x in results} == {first["tx_hash"]}
            assert len(set(rpc.sent)) == 1
        finally:
            await wallet.close()

    asyncio.run(run())


def test_bnb_transfer_is_rejected_before_broadcast(settings):
    rpc = RPC()

    async def run():
        wallet = open_wallet(settings, rpc)
        try:
            with pytest.raises(ToolError, match="bnb_reserved_for_gas"):
                await wallet.transfer("event", DESTINATION, "0.1")
            assert not rpc.sent
        finally:
            await wallet.close()

    asyncio.run(run())


def test_bep20_uses_contract_decimals_and_fixed_transfer_selector(settings):
    rpc = RPC()
    rpc.decimals = 6

    async def run():
        wallet = open_wallet(settings, rpc)
        try:
            await wallet.transfer("event", DESTINATION, "17.123456", TOKEN)
            fields = rlp.decode(bytes.fromhex(rpc.sent[0][2:]))
            assert fields[3].hex() == TOKEN[2:] and int.from_bytes(fields[4]) == 0
            assert fields[5].hex() == "a9059cbb" + DESTINATION[2:].zfill(64) + hex(17123456)[
                2:
            ].zfill(64)
        finally:
            await wallet.close()

    asyncio.run(run())


@pytest.mark.parametrize("amount", ["0", "-1", "NaN", "Infinity", "1e-3", "0.0000000000000000001"])
def test_invalid_amounts_cannot_sign(settings, amount):
    rpc = RPC()

    async def run():
        wallet = open_wallet(settings, rpc)
        try:
            with pytest.raises(ToolError):
                await wallet.transfer("event", DESTINATION, amount, TOKEN)
            assert not rpc.sent
        finally:
            await wallet.close()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["chain", "balance"])
def test_wrong_chain_or_insufficient_fee_cannot_sign(settings, failure):
    rpc = RPC()
    if failure == "chain":
        rpc.chain = "0x1"
    else:
        rpc.balance = 0

    async def run():
        wallet = open_wallet(settings, rpc)
        try:
            with pytest.raises(ToolError, match="wrong_chain|insufficient_bnb"):
                await wallet.transfer("event", DESTINATION, "0.1", TOKEN)
            assert not rpc.sent
        finally:
            await wallet.close()

    asyncio.run(run())


def test_missing_key_fails_without_generating_replacement(settings):
    (settings.directory / "keystore.json").unlink()
    with pytest.raises(ValueError, match="restore the original"):
        open_wallet(settings, RPC())
    assert not (settings.directory / "keystore.json").exists()


def test_wallet_mcp_discovery_permissions_and_host_metadata(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("TOOL_AUDIT_PATH", str(tmp_path / "audit.sqlite3"))
    rpc = RPC()
    server = create_server(settings, wallet_factory=lambda s: open_wallet(s, rpc))

    async def run():
        client = WalletMCPClient(settings, server=server)
        runtime = ToolRuntime(ToolSettings(frozenset()), wallet=client)
        try:
            await runtime.prepare(CONTEXT)
            schemas = runtime.schemas(CONTEXT)
            assert {x["function"]["name"] for x in schemas} == {
                "wallet_info",
                "wallet_transfer",
                "wallet_transaction",
            }
            transfer = next(
                x["function"] for x in schemas if x["function"]["name"] == "wallet_transfer"
            )
            assert "event_key" not in transfer["parameters"]["properties"]
            assert not runtime.schemas(ToolContext("other", "member", "message"))
            args = {"to_address": DESTINATION, "amount": "0.1", "token_address": TOKEN}
            with pytest.raises(ToolError, match="trusted_event_required"):
                await client.mcp.call("wallet_transfer", args)
            rejected = json.loads(
                await runtime.invoke(
                    "wallet_transfer", json.dumps({**args, "event_key": "forged"}), CONTEXT
                )
            )
            assert rejected["error"] == "invalid_tool_arguments"
            first = json.loads(await runtime.invoke("wallet_transfer", json.dumps(args), CONTEXT))
            second = json.loads(await runtime.invoke("wallet_transfer", json.dumps(args), CONTEXT))
            assert first["ok"] and second["ok"]
            assert first["data"]["tx_hash"] == second["data"]["tx_hash"]
            assert len(set(rpc.sent)) == 1
        finally:
            await runtime.close()

    asyncio.run(run())


def test_real_stdio_mcp_process_discovers_and_rejects_untrusted_transfer(settings):
    async def run():
        client = WalletMCPClient(settings)
        await client.prepare()
        assert len(client.mcp.schemas) == 3
        with pytest.raises(ToolError, match="trusted_event_required"):
            await client.mcp.call("wallet_transfer", {"to_address": DESTINATION, "amount": "1"})
        assert "AI_API_KEY" not in client.mcp.server.env

    asyncio.run(run())


def test_amounts_use_exact_integer_arithmetic():
    assert units("1234567890123.123456789012345678", 18) == 1234567890123123456789012345678
    assert display_units(1234567890123123456789012345678, 18) == "1234567890123.123456789012345678"


def test_wallet_settings_require_explicit_groups_and_https(monkeypatch):
    monkeypatch.setenv("WALLET_ENABLED", "true")
    monkeypatch.delenv("WALLET_ALLOWED_GROUPS", raising=False)
    with pytest.raises(ValueError, match="explicit"):
        WalletSettings.from_env()
    monkeypatch.setenv("WALLET_ALLOWED_GROUPS", GROUP)
    monkeypatch.setenv("WALLET_RPC_URL", "http://untrusted.example")
    with pytest.raises(ValueError, match="HTTPS"):
        WalletSettings.from_env()


def test_all_groups_wallet_still_obeys_chat_revocation(settings):
    settings = replace(settings, allowed_groups=frozenset({"*"}))
    client = WalletMCPClient(settings)
    policies = Mock(spec=GroupPolicies)
    runtime = ToolRuntime(ToolSettings(frozenset()), wallet=client, policies=policies)
    policies.features.return_value = Features(chat=True)
    assert runtime.wallet_allowed(CONTEXT)
    assert runtime.wallet_allowed(ToolContext("other-group-openid", "member", "message"))
    policies.features.return_value = Features(chat=False)
    assert not runtime.wallet_allowed(CONTEXT)
    asyncio.run(runtime.close())


def test_deployment_acceptance_uses_mcp_and_checks_chain(settings, monkeypatch):
    from fullish import wallet_check

    rpc = RPC()
    server = create_server(settings, wallet_factory=lambda s: open_wallet(s, rpc))
    monkeypatch.setattr(WalletSettings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.setattr(
        wallet_check, "WalletMCPClient", lambda s: WalletMCPClient(s, server=server)
    )
    result = asyncio.run(wallet_check.check())
    assert result["wallet"] == "ok" and result["mcp_tools"] == 3
    assert result["bnb_balance"] == "100"
    assert not rpc.sent
    rpc.chain = "0x1"
    with pytest.raises(ToolError, match="wrong_chain"):
        asyncio.run(wallet_check.check())

"""Stage E / F — compile, deploy, attest, and read from Base Sepolia."""
from __future__ import annotations

import json
from pathlib import Path

from eth_account import Account
from web3 import Web3

from .config import BASE_SEPOLIA_CHAIN_ID, BASESCAN_ADDR, BASESCAN_TX, DEPLOYMENTS_FILE, REPO_ROOT

CONTRACT_SRC = REPO_ROOT / "contracts" / "AttestationRegistry.sol"
SOLC_VERSION = "0.8.24"


def _compile() -> dict:
    from solcx import compile_standard, install_solc

    install_solc(SOLC_VERSION)
    src = CONTRACT_SRC.read_text()
    out = compile_standard(
        {
            "language": "Solidity",
            "sources": {"AttestationRegistry.sol": {"content": src}},
            "settings": {
                "optimizer": {"enabled": True, "runs": 200},
                "outputSelection": {"*": {"*": ["abi", "evm.bytecode.object"]}},
            },
        },
        solc_version=SOLC_VERSION,
    )
    c = out["contracts"]["AttestationRegistry.sol"]["AttestationRegistry"]
    return {"abi": c["abi"], "bytecode": c["evm"]["bytecode"]["object"]}


class Chain:
    def __init__(self, rpc_url: str, private_key: str | None = None):
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 60}))
        self.acct = Account.from_key(private_key) if private_key else None

    # ---- deploy ----
    def deploy(self) -> dict:
        assert self.acct, "deployer private key required"
        art = _compile()
        contract = self.w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"])
        tx = contract.constructor().build_transaction(
            {
                "from": self.acct.address,
                "nonce": self.w3.eth.get_transaction_count(self.acct.address),
                "chainId": BASE_SEPOLIA_CHAIN_ID,
                "maxFeePerGas": self.w3.eth.gas_price * 2,
                "maxPriorityFeePerGas": self.w3.to_wei(1, "gwei"),
            }
        )
        tx["gas"] = int(self.w3.eth.estimate_gas(tx) * 1.2)
        signed = self.acct.sign_transaction(tx)
        h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        rcpt = self.w3.eth.wait_for_transaction_receipt(h, timeout=180)

        info = {
            "address": rcpt.contractAddress,
            "abi": art["abi"],
            "deploy_tx": h.hex(),
            "chain_id": BASE_SEPOLIA_CHAIN_ID,
            "explorer": BASESCAN_ADDR + rcpt.contractAddress,
        }
        DEPLOYMENTS_FILE.write_text(json.dumps(info, indent=2))
        return info

    # ---- registry handle ----
    def registry(self, address: str, abi: list | None = None):
        if abi is None:
            abi = json.loads(DEPLOYMENTS_FILE.read_text())["abi"]
        return self.w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi)

    # ---- attest ----
    def attest(self, address: str, *, merkle_root: str, probe_hash: str, cid: str, match_url: str) -> dict:
        assert self.acct, "attester private key required"
        reg = self.registry(address)
        fn = reg.functions.attest(
            Web3.to_bytes(hexstr=merkle_root),
            Web3.to_bytes(hexstr=_b32(probe_hash)),
            cid,
            match_url or "",
        )
        tx = fn.build_transaction(
            {
                "from": self.acct.address,
                "nonce": self.w3.eth.get_transaction_count(self.acct.address),
                "chainId": BASE_SEPOLIA_CHAIN_ID,
                "maxFeePerGas": self.w3.eth.gas_price * 2,
                "maxPriorityFeePerGas": self.w3.to_wei(1, "gwei"),
            }
        )
        tx["gas"] = int(self.w3.eth.estimate_gas(tx) * 1.2)
        signed = self.acct.sign_transaction(tx)
        h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        rcpt = self.w3.eth.wait_for_transaction_receipt(h, timeout=180)
        ev = reg.events.Attested().process_receipt(rcpt)[0]
        return {
            "id": ev["args"]["id"],
            "tx": h.hex(),
            "explorer": BASESCAN_TX + h.hex(),
            "block": rcpt.blockNumber,
        }

    # ---- read ----
    def get(self, address: str, attestation_id: int) -> dict:
        reg = self.registry(address)
        a = reg.functions.get(attestation_id).call()
        return {
            "attester": a[0],
            "merkle_root": "0x" + a[1].hex(),
            "probe_hash": "0x" + a[2].hex(),
            "cid": a[3],
            "match_url": a[4],
            "timestamp": a[5],
        }


def _b32(hexstr: str) -> str:
    """sha256 digests are already 32 bytes; pass through, tolerate missing 0x."""
    h = hexstr[2:] if hexstr.startswith("0x") else hexstr
    return "0x" + h.rjust(64, "0")[:64]

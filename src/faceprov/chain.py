"""Stage E / F — compile, deploy, attest, and read from an EVM testnet.

Defaults to Ethereum Sepolia; the chain is fully configured by `Config`
(rpc_url / chain_id / explorer_url), so any EVM testnet works unchanged.
"""
from __future__ import annotations

import json
from pathlib import Path

from eth_account import Account
from web3 import Web3

from .config import DEPLOYMENTS_FILE, REPO_ROOT, Config

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
    def __init__(
        self,
        rpc_url: str,
        chain_id: int,
        explorer_url: str,
        private_key: str | None = None,
    ):
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 60}))
        self.chain_id = chain_id
        self.explorer_url = explorer_url.rstrip("/")
        self.acct = Account.from_key(private_key) if private_key else None

    @classmethod
    def from_config(cls, cfg: Config, *, signer: bool = False) -> "Chain":
        return cls(
            cfg.rpc_url, cfg.chain_id, cfg.explorer_url,
            cfg.deployer_key if signer else None,
        )

    def _tx_common(self) -> dict:
        return {
            "from": self.acct.address,
            "nonce": self.w3.eth.get_transaction_count(self.acct.address),
            "chainId": self.chain_id,
            "maxFeePerGas": self.w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": self.w3.to_wei(1, "gwei"),
        }

    def _send(self, tx: dict) -> tuple:
        tx["gas"] = int(self.w3.eth.estimate_gas(tx) * 1.2)
        signed = self.acct.sign_transaction(tx)
        h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return h, self.w3.eth.wait_for_transaction_receipt(h, timeout=240)

    # ---- deploy ----
    def deploy(self) -> dict:
        assert self.acct, "deployer private key required"
        art = _compile()
        contract = self.w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"])
        tx = contract.constructor().build_transaction(self._tx_common())
        h, rcpt = self._send(tx)

        info = {
            "address": rcpt.contractAddress,
            "abi": art["abi"],
            "deploy_tx": h.hex(),
            "chain_id": self.chain_id,
            "explorer": f"{self.explorer_url}/address/{rcpt.contractAddress}",
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
        h, rcpt = self._send(fn.build_transaction(self._tx_common()))
        ev = reg.events.Attested().process_receipt(rcpt)[0]
        return {
            "id": ev["args"]["id"],
            "tx": h.hex(),
            "explorer": f"{self.explorer_url}/tx/{h.hex()}",
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

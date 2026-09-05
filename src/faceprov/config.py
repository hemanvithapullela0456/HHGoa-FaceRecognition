"""Central configuration, loaded from environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOYMENTS_FILE = REPO_ROOT / "deployments.json"

# Default target chain: Ethereum Sepolia (chain id 11155111).
# Override RPC_URL / CHAIN_ID / EXPLORER_URL in .env to point at any EVM testnet.
DEFAULT_RPC = "https://ethereum-sepolia-rpc.publicnode.com"
DEFAULT_CHAIN_ID = 11155111
DEFAULT_EXPLORER = "https://sepolia.etherscan.io"


def _req(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise RuntimeError(f"Missing required env var: {name} (see .env.example)")
    return val


def _first_env(*names: str, default: str = "") -> str:
    for n in names:
        v = os.getenv(n, "").strip()
        if v:
            return v
    return default


@dataclass(frozen=True)
class Config:
    serpapi_key: str
    pinata_jwt: str
    pinata_gateway: str
    rpc_url: str
    chain_id: int
    explorer_url: str
    deployer_key: str
    registry_address: str
    match_threshold: float
    max_candidates: int
    wayback_enabled: bool
    wayback_max_urls: int
    wayback_timeout: float

    def explorer_addr(self, address: str) -> str:
        return f"{self.explorer_url.rstrip('/')}/address/{address}"

    def explorer_tx(self, tx_hash: str) -> str:
        return f"{self.explorer_url.rstrip('/')}/tx/{tx_hash}"

    @classmethod
    def load(cls, *, require_chain: bool = True, require_search: bool = True) -> "Config":
        return cls(
            serpapi_key=_req("SERPAPI_KEY") if require_search else os.getenv("SERPAPI_KEY", ""),
            pinata_jwt=_req("PINATA_JWT") if require_search else os.getenv("PINATA_JWT", ""),
            pinata_gateway=os.getenv("PINATA_GATEWAY", "https://gateway.pinata.cloud").rstrip("/"),
            rpc_url=_first_env("RPC_URL", "BASE_SEPOLIA_RPC", "SEPOLIA_RPC", default=DEFAULT_RPC),
            chain_id=int(_first_env("CHAIN_ID", default=str(DEFAULT_CHAIN_ID))),
            explorer_url=_first_env("EXPLORER_URL", default=DEFAULT_EXPLORER),
            deployer_key=_req("DEPLOYER_PRIVATE_KEY") if require_chain else os.getenv("DEPLOYER_PRIVATE_KEY", ""),
            registry_address=os.getenv("ATTESTATION_REGISTRY_ADDRESS", "").strip(),
            match_threshold=float(os.getenv("FACEPROV_MATCH_THRESHOLD", "0.36")),
            max_candidates=int(os.getenv("FACEPROV_MAX_CANDIDATES", "12")),
            # Wayback dating is a plain unauthenticated GET, so it is on by default;
            # set FACEPROV_WAYBACK=0 to run fully offline of the Archive.
            wayback_enabled=os.getenv("FACEPROV_WAYBACK", "1").strip().lower()
            not in {"0", "false", "no"},
            wayback_max_urls=int(os.getenv("FACEPROV_WAYBACK_MAX_URLS", "8")),
            wayback_timeout=float(os.getenv("FACEPROV_WAYBACK_TIMEOUT", "20")),
        )

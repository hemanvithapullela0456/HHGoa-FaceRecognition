"""Central configuration, loaded from environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOYMENTS_FILE = REPO_ROOT / "deployments.json"

BASE_SEPOLIA_CHAIN_ID = 84532
BASESCAN_ADDR = "https://sepolia.basescan.org/address/"
BASESCAN_TX = "https://sepolia.basescan.org/tx/"


def _req(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise RuntimeError(f"Missing required env var: {name} (see .env.example)")
    return val


@dataclass(frozen=True)
class Config:
    serpapi_key: str
    pinata_jwt: str
    pinata_gateway: str
    rpc_url: str
    deployer_key: str
    registry_address: str
    match_threshold: float
    max_candidates: int

    @classmethod
    def load(cls, *, require_chain: bool = True, require_search: bool = True) -> "Config":
        return cls(
            serpapi_key=_req("SERPAPI_KEY") if require_search else os.getenv("SERPAPI_KEY", ""),
            pinata_jwt=_req("PINATA_JWT") if require_search else os.getenv("PINATA_JWT", ""),
            pinata_gateway=os.getenv("PINATA_GATEWAY", "https://gateway.pinata.cloud").rstrip("/"),
            rpc_url=os.getenv("BASE_SEPOLIA_RPC", "https://sepolia.base.org"),
            deployer_key=_req("DEPLOYER_PRIVATE_KEY") if require_chain else os.getenv("DEPLOYER_PRIVATE_KEY", ""),
            registry_address=os.getenv("ATTESTATION_REGISTRY_ADDRESS", "").strip(),
            match_threshold=float(os.getenv("FACEPROV_MATCH_THRESHOLD", "0.38")),
            max_candidates=int(os.getenv("FACEPROV_MAX_CANDIDATES", "12")),
        )

"""Pinata IPFS pinning — used for the probe image and the final evidence bundle."""
from __future__ import annotations

import io
import json

import requests

PIN_FILE = "https://api.pinata.cloud/pinning/pinFileToIPFS"
PIN_JSON = "https://api.pinata.cloud/pinning/pinJSONToIPFS"

# Public gateways tried, in order, when the configured one does not answer. A CID
# addresses content, not a server, so every one of these returns identical bytes or
# nothing — falling through them cannot change what is verified, only whether the
# verification completes at all.
# Ordered by observed reliability, not popularity: on the machine this was last run
# from, the well-known gateways (pinata's public one, ipfs.io, dweb.link, w3s.link)
# all timed out at TCP-connect while filebase answered in under two seconds.
PUBLIC_GATEWAYS = (
    "https://ipfs.filebase.io",
    "https://ipfs.io",
    "https://dweb.link",
    "https://w3s.link",
    "https://nftstorage.link",
)


class Pinata:
    def __init__(self, jwt: str, gateway: str):
        self.jwt = jwt
        self.gateway = gateway.rstrip("/")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.jwt}"}

    def pin_bytes(self, data: bytes, name: str) -> str:
        r = requests.post(
            PIN_FILE,
            headers=self._headers(),
            files={"file": (name, io.BytesIO(data))},
            data={"pinataMetadata": json.dumps({"name": name})},
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["IpfsHash"]

    def pin_json(self, obj: dict, name: str) -> str:
        r = requests.post(
            PIN_JSON,
            headers=self._headers(),
            json={"pinataContent": obj, "pinataMetadata": {"name": name}},
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["IpfsHash"]

    def gateway_url(self, cid: str) -> str:
        return f"{self.gateway}/ipfs/{cid}"

    def fetch_bytes(self, cid: str, *, timeout: int = 20) -> bytes:
        """Fetch pinned content by CID, falling through gateways until one answers."""
        errors: list[str] = []
        for base in (self.gateway, *PUBLIC_GATEWAYS):
            url = f"{base.rstrip('/')}/ipfs/{cid}"
            try:
                r = requests.get(url, timeout=timeout)
                r.raise_for_status()
                return r.content
            except Exception as e:  # noqa: BLE001
                errors.append(f"{base}: {type(e).__name__}")
        raise RuntimeError(
            f"could not fetch {cid} from any gateway - tried {len(errors)}: "
            + "; ".join(errors)
        )

    def fetch_json(self, cid: str, *, timeout: int = 20) -> dict:
        """Fetch a pinned JSON document, falling through gateways until one answers.

        The configured gateway is tried first (it is the one recorded in the bundle
        and, on a paid plan, the fast one). Public gateways rate-limit aggressively,
        so a timeout on any single one says nothing about whether the evidence is
        still retrievable.
        """
        errors: list[str] = []
        for base in (self.gateway, *PUBLIC_GATEWAYS):
            url = f"{base.rstrip('/')}/ipfs/{cid}"
            try:
                r = requests.get(url, timeout=timeout)
                r.raise_for_status()
                return r.json()
            except Exception as e:  # noqa: BLE001
                errors.append(f"{base}: {type(e).__name__}")
        raise RuntimeError(
            f"could not fetch {cid} from any gateway - tried {len(errors)}: "
            + "; ".join(errors)
        )

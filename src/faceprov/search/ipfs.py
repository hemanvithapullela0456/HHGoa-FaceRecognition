"""Pinata IPFS pinning — used for the probe image and the final evidence bundle."""
from __future__ import annotations

import io
import json

import requests

PIN_FILE = "https://api.pinata.cloud/pinning/pinFileToIPFS"
PIN_JSON = "https://api.pinata.cloud/pinning/pinJSONToIPFS"


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

    def fetch_json(self, cid: str) -> dict:
        r = requests.get(self.gateway_url(cid), timeout=60)
        r.raise_for_status()
        return r.json()

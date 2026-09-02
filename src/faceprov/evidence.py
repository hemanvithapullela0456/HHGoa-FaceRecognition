"""Stage D — canonical evidence bundle + Merkle tree.

The bundle is a deterministic JSON document. Every top-level section is serialized
canonically (sorted keys, no whitespace) and hashed into a leaf; leaves are combined
into a keccak256 Merkle tree with sorted sibling pairs (the same convention OpenZeppelin
uses), so the root can be recomputed by anyone from the pinned bundle and checked
against the on-chain value.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from . import __version__
from ._keccak import keccak256


def sha256_bytes(b: bytes) -> str:
    return "0x" + hashlib.sha256(b).hexdigest()


def _canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def keccak(b: bytes) -> bytes:
    return keccak256(b)


def _leaf(section_name: str, payload: Any) -> bytes:
    # domain-separate the leaf by section name to prevent cross-section collisions
    return keccak(b"faceprov-leaf:" + section_name.encode() + b":" + _canon(payload))


def merkle_root(leaves: list[bytes]) -> bytes:
    if not leaves:
        raise ValueError("no leaves")
    level = list(leaves)
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])  # duplicate last
        nxt = []
        for i in range(0, len(level), 2):
            a, b = level[i], level[i + 1]
            lo, hi = (a, b) if a <= b else (b, a)  # sorted pair
            nxt.append(keccak(lo + hi))
        level = nxt
    return level[0]


def merkle_proof(leaves: list[bytes], index: int) -> list[str]:
    """Sibling path for the leaf at `index` (hex strings)."""
    proof: list[str] = []
    level = list(leaves)
    idx = index
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        sib = idx ^ 1
        proof.append("0x" + level[sib].hex())
        idx //= 2
        nxt = []
        for i in range(0, len(level), 2):
            a, b = level[i], level[i + 1]
            lo, hi = (a, b) if a <= b else (b, a)
            nxt.append(keccak(lo + hi))
        level = nxt
    return proof


def set_leaf_path(doc: dict, dotted: str, value: Any) -> None:
    """Set a nested value by a path like 'candidates.0.best_cosine'; keeps the old type."""
    cur: Any = doc
    parts = dotted.split(".")
    for p in parts[:-1]:
        cur = cur[int(p)] if isinstance(cur, list) else cur[p]
    last = parts[-1]
    if isinstance(cur, list):
        cur[int(last)] = value
        return
    old = cur.get(last)
    if isinstance(old, bool):
        value = str(value).lower() in {"1", "true", "yes"}
    elif isinstance(old, (int, float)):
        value = type(old)(value)
    cur[last] = value


@dataclass
class EvidenceBundle:
    probe: dict = field(default_factory=dict)
    search: dict = field(default_factory=dict)
    candidates: list[dict] = field(default_factory=list)
    match: dict = field(default_factory=dict)
    run: dict = field(default_factory=dict)

    # ---- assembly ----
    def ordered_sections(self) -> list[tuple[str, Any]]:
        """Leaf order is fixed and part of the spec."""
        sections: list[tuple[str, Any]] = [
            ("probe", self.probe),
            ("search", self.search),
        ]
        for i, c in enumerate(self.candidates):
            sections.append((f"candidate[{i}]", c))
        sections.append(("match", self.match))
        sections.append(("run", self.run))
        return sections

    def leaves(self) -> list[bytes]:
        return [_leaf(name, payload) for name, payload in self.ordered_sections()]

    def root(self) -> str:
        return "0x" + merkle_root(self.leaves()).hex()

    def to_json(self) -> dict:
        return {
            "schema": "faceprov/evidence-bundle/v1",
            "faceprov_version": __version__,
            "merkle": {
                "algo": "keccak256",
                "pairing": "sorted",
                "leaf_order": [name for name, _ in self.ordered_sections()],
                "root": self.root(),
            },
            "probe": self.probe,
            "search": self.search,
            "candidates": self.candidates,
            "match": self.match,
            "run": self.run,
        }

    @classmethod
    def from_json(cls, doc: dict) -> "EvidenceBundle":
        return cls(
            probe=doc.get("probe", {}),
            search=doc.get("search", {}),
            candidates=doc.get("candidates", []),
            match=doc.get("match", {}),
            run=doc.get("run", {}),
        )

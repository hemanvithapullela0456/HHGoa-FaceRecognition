"""Merkle / bundle tests — pure, no API keys or network."""
from faceprov.evidence import EvidenceBundle, merkle_proof, merkle_root, keccak


def _bundle() -> EvidenceBundle:
    b = EvidenceBundle()
    b.probe = {"image_sha256": "0xaa", "det_score": 0.99}
    b.search = {"path_taken": "B:visual", "entity_name": None}
    b.candidates = [
        {"source_url": "https://example.com/a", "cosine": 0.41, "accepted": True},
        {"source_url": "https://example.com/b", "cosine": 0.12, "accepted": False},
        {"source_url": "https://example.com/c", "cosine": 0.33, "accepted": False},
    ]
    b.match = {"matched": True, "source_url": "https://example.com/a"}
    b.run = {"faceprov_version": "0.1.0"}
    return b


def test_root_is_deterministic():
    assert _bundle().root() == _bundle().root()


def test_root_changes_when_a_leaf_changes():
    b1 = _bundle()
    b2 = _bundle()
    b2.candidates[0]["cosine"] = 0.40
    assert b1.root() != b2.root()


def test_leaf_count_matches_sections():
    b = _bundle()
    # probe + search + 3 candidates + match + run
    assert len(b.leaves()) == 7


def test_merkle_proof_verifies():
    b = _bundle()
    leaves = b.leaves()
    root = bytes.fromhex(b.root()[2:])
    idx = 2
    node = leaves[idx]
    for sib_hex in merkle_proof(leaves, idx):
        sib = bytes.fromhex(sib_hex[2:])
        lo, hi = (node, sib) if node <= sib else (sib, node)
        node = keccak(lo + hi)
    assert node == root


def test_roundtrip_json():
    b = _bundle()
    doc = b.to_json()
    assert EvidenceBundle.from_json(doc).root() == b.root()

"""Merkle / bundle tests — pure, no API keys or network."""
from faceprov.evidence import (
    EvidenceBundle,
    keccak,
    merkle_proof,
    merkle_root,
    set_leaf_path,
)


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


def test_tamper_breaks_root():
    b = _bundle()
    doc = b.to_json()
    onchain_root = b.root()

    set_leaf_path(doc, "candidates.0.cosine", 0.99)
    tampered_root = EvidenceBundle.from_json(doc).root()

    assert tampered_root != onchain_root


def test_set_leaf_path_keeps_type():
    doc = {"a": {"n": 1, "b": True}, "lst": [{"x": 0.1}]}
    set_leaf_path(doc, "a.n", "5")
    set_leaf_path(doc, "a.b", "false")
    set_leaf_path(doc, "lst.0.x", "0.9")
    assert doc == {"a": {"n": 5, "b": False}, "lst": [{"x": 0.9}]}


# ---- schema v2: the optional `timeline` leaf ----

def test_v1_bundle_root_is_unchanged_by_the_v2_field():
    """Attestations already on chain were sealed without a timeline leaf.

    v2 adds `timeline`, but omits it from the leaf set when empty, so every bundle
    written before this change must still recompute to the exact root it was sealed
    with. If this test ever fails, live attestations stop verifying.
    """
    b = _bundle()                       # no timeline set - i.e. a v1-shaped bundle
    assert b.timeline == {}
    assert [name for name, _ in b.ordered_sections()] == [
        "probe", "search", "candidate[0]", "candidate[1]", "candidate[2]", "match", "run",
    ]
    assert len(b.leaves()) == 7


def test_timeline_adds_a_leaf_when_present():
    b = _bundle()
    without = b.root()
    b.timeline = {"earliest_known_appearance": "2019-03-04T12:00:00+00:00"}

    assert len(b.leaves()) == 8
    assert b.root() != without
    # the timeline is evidence about the match, and is sealed between it and the run
    assert [n for n, _ in b.ordered_sections()][-3:] == ["match", "timeline", "run"]


def test_timeline_is_covered_by_the_root():
    b = _bundle()
    b.timeline = {"earliest_known_appearance": "2019-03-04T12:00:00+00:00"}
    sealed = b.root()

    doc = b.to_json()
    set_leaf_path(doc, "timeline.earliest_known_appearance", "2024-01-01T00:00:00+00:00")
    assert EvidenceBundle.from_json(doc).root() != sealed, "back-dating must be detectable"


def test_timeline_survives_the_json_roundtrip():
    b = _bundle()
    b.timeline = {"earliest_known_appearance": "2019-03-04T12:00:00+00:00", "pages": {}}
    assert EvidenceBundle.from_json(b.to_json()).root() == b.root()

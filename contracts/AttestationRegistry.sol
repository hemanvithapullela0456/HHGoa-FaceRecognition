// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title AttestationRegistry
/// @notice Minimal append-only notary for face-anchored provenance findings.
///         Each attestation records the Merkle root of a canonical evidence bundle,
///         the IPFS CID of that bundle, a hash of the probe image, and the matched
///         post URL. There is no admin, no upgradeability, and no token — records
///         cannot be altered or removed once written.
contract AttestationRegistry {
    struct Attestation {
        address attester;
        bytes32 merkleRoot;   // keccak256 Merkle root of the evidence bundle leaves
        bytes32 probeHash;    // sha256 of the probe image bytes (as bytes32)
        string  cid;          // IPFS CID of the full evidence bundle JSON
        string  matchUrl;     // canonical URL of the matched social post
        uint256 timestamp;    // block time of attestation
    }

    Attestation[] private _attestations;

    event Attested(
        uint256 indexed id,
        address indexed attester,
        bytes32 merkleRoot,
        bytes32 probeHash,
        string  cid,
        string  matchUrl,
        uint256 timestamp
    );

    /// @notice Write a new attestation. Returns its id.
    function attest(
        bytes32 merkleRoot,
        bytes32 probeHash,
        string calldata cid,
        string calldata matchUrl
    ) external returns (uint256 id) {
        require(merkleRoot != bytes32(0), "empty root");
        require(bytes(cid).length > 0, "empty cid");

        id = _attestations.length;
        _attestations.push(
            Attestation({
                attester: msg.sender,
                merkleRoot: merkleRoot,
                probeHash: probeHash,
                cid: cid,
                matchUrl: matchUrl,
                timestamp: block.timestamp
            })
        );

        emit Attested(id, msg.sender, merkleRoot, probeHash, cid, matchUrl, block.timestamp);
    }

    function get(uint256 id) external view returns (Attestation memory) {
        require(id < _attestations.length, "no such id");
        return _attestations[id];
    }

    function count() external view returns (uint256) {
        return _attestations.length;
    }
}

# FaceProv — Face Provenance for Image Verification

**HH Goa 2026 · Shortlisting Task 3 — Face Identification & Blockchain Verification**

## What it does

FaceProv takes a face scan as input, searches the web and social media for where that
person genuinely appears, verifies each candidate match with a second face-recognition
pass, and writes a tamper-evident, timestamped attestation of the finding to a public
blockchain.

```
face scan → detect + encode → web/social search → face re-verification
                                                          │
                                                          ▼
                                            earliest-appearance dating (Wayback)
                                                          │
                                                          ▼
re-verify later ← on-chain attestation ← Merkle-rooted evidence bundle (pinned to IPFS)
```

**Pipeline stages:**
1. **Face detection & encoding** — OpenCV YuNet detector + SFace recognizer (ONNX, no
   extra runtime deps), cosine-similarity identity matching.
2. **Web/social search** — Google Lens + Yandex Images (via SerpApi), with an entity
   router: if a knowledge-graph identity is resolved, it searches that person's
   verified profiles directly; otherwise it unions raw visual matches from both
   engines.
3. **Earliest-appearance dating** — checks each source page against the Internet
   Archive's CDX API to bound how long an image has been online.
4. **Face re-verification** — every candidate image is re-embedded and compared to the
   probe via cosine similarity; only accepted matches proceed.
5. **Evidence bundle** — a canonical JSON bundle (probe hash, search metadata,
   candidate scores, a three-tier page fingerprint per source, timeline data) is
   Merkle-hashed and pinned to IPFS.
6. **Blockchain attestation** — the Merkle root, IPFS CID, and match summary are
   written on-chain via a minimal append-only smart contract.
7. **Re-verification** — a CLI command re-fetches everything (chain record, IPFS
   bundle, source pages, probe) and re-checks it against what was originally
   attested, grading any drift as `ok` / `info` / `warn` / `fail`.

The design deliberately favors **public figures and consenting individuals with an
online presence** over generic "identify any stranger" search, since that's the only
population reverse face search can get meaningful recall on — and it's also where
miscaptioning and deepfake disputes actually need evidence integrity.

## How to run

### Prerequisites
- Python 3.12
- Free-tier API keys: **SerpApi**, **Pinata**, a funded **Ethereum Sepolia** wallet

### Setup
```bash
python -m venv .venv
.venv\Scripts\Activate.ps1          # Windows  (in bash: . .venv/bin/activate)
pip install -r requirements.txt
pip install -e .                    # exposes the `faceprov` command
copy .env.example .env              # then fill in the keys
```
The first `run` auto-downloads the YuNet + SFace models (~37 MB).

### Deploy the contract (one time)
```bash
faceprov deploy
# prints the address; paste it into .env as ATTESTATION_REGISTRY_ADDRESS
```

### Run the pipeline
```bash
python -m faceprov.cli run --image path/to/face.jpg
```
Or use the web UI:
```bash
faceprov serve            # → http://127.0.0.1:8000
```

### Demonstrate tamper-evidence
```bash
python -m faceprov.cli tamper --id 7
```
Edits one field of the attested bundle, recomputes the Merkle root, and shows it no
longer matches the immutable on-chain root.

## Blockchain used

**Ethereum Sepolia** (public testnet, chain id `11155111`) by default.

Contract: `AttestationRegistry.sol` — a minimal, non-upgradeable, admin-free notary
contract that emits an `Attested` event per submission.

| | |
|---|---|
| Address | [`0xB8d2a9E923949EBc6D9Dc4d5e925F5554cEE89C7`](https://sepolia.etherscan.io/address/0xB8d2a9E923949EBc6D9Dc4d5e925F5554cEE89C7) |
| Deploy tx | `0x18d05ff0400b435ee5e051b07be4c7196c5b0110c5b1c30b600ec9ea518f3110` |

The chain is not hard-coded — set `RPC_URL` / `CHAIN_ID` / `EXPLORER_URL` in `.env` to
deploy to any other EVM testnet instead.

## Known limitations

- **Recall on private individuals is inherently low** — they're barely indexed online;
  this is a property of the search space, not a solvable bug.
- **Search results are non-deterministic** — Lens/Yandex results shift over time, so a
  recorded run may not reproduce identically. The evidence bundle captures what was
  seen at attestation time.
- **Yandex via SerpApi caps at ~4 source pages** and requires a public image URL.
- **Deepfake robustness is out of scope** — a convincing synthetic face of a real
  person can pass the recognizer. This tool records provenance; it is not a
  liveness/AIGC detector.
- **Archive dating bounds the page, not the photo** — the earliest known appearance is
  the oldest capture of a *source page*, an upper bound on image age, not proof of when
  the photo was taken.
- **Wayback coverage is uneven and slow** — social permalinks are archived far less
  than news articles, and dating adds roughly 30–60s per run (skippable via
  `FACEPROV_WAYBACK=0`).
- **Free image hosts and public IPFS gateways are volatile** — they rate-limit and
  occasionally go down; the pipeline tries several in sequence but can still fail if
  all are down at once.
- **Sepolia is a testnet** — attestations are real and explorer-visible but not
  economically secured the way mainnet would be.
- **No hosted website** — `faceprov serve` is a local demo UI (single user, in-memory
  jobs), not a deployed service.
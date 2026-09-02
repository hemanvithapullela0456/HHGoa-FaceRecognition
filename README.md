# FaceProv — Face-Anchored Provenance for Image Verification

**HH Goa 2026 · Shortlisting Task 3 — Face Identification & Blockchain Verification**

A pipeline that takes a face scan as input, genuinely searches the web / social media
for where that person appears, verifies each candidate with a second face-recognition
pass, and then writes a **tamper-evident, timestamped attestation** of the finding to a
public blockchain — so that anyone can later re-verify the claim and detect if any
source page was altered.

```
face scan  ──▶  detect + encode  ──▶  web / social search  ──▶  face re-verification
                                          (Google Lens                 (SFace cosine,
                                           + Yandex,                     per-candidate
                                           entity router)                accept/reject)
                                                                              │
                                                                              ▼
   re-verify later  ◀──  on-chain attestation  ◀──  Merkle-rooted evidence bundle
   (CLI verify cmd)      (Ethereum Sepolia, event)   (pinned to IPFS)
```

> **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** is the full technical record — every
> module, the end-to-end data flow, every design decision and the alternatives
> weighed against it, the bundle schema, the threat model, and the known limits.

---

## 1. Why this framing

Reverse-image face search has a hard asymmetry that no model quality fixes: **public
figures are densely indexed** (news, Wikipedia, fan wikis — thousands of crawled
images), while **a private individual has maybe three photos online, often uncrawled.**
An "identify any stranger from a photo" product cannot be delivered well on that
distribution, and it is also the creepy version of the problem.

So we invert it. The population we *can* get recall on — public figures — is exactly
the population that miscaptioned images, deepfakes, and "is this really them?" disputes
actually target. The product is therefore **evidence integrity for image verification**,
not surveillance:

> Given a face in a circulating image, find where that person genuinely appears online,
> and produce an immutable record that *at time T, this face matched these sources at
> these similarity scores* — verifiable by anyone, and provably broken if a source page
> later changes.

That is a real tool for journalists and OSINT researchers, and it satisfies every line
of the task spec unchanged.

---

## 2. Pipeline architecture

### Stage A — Face detection & encoding
- **Detector + encoder:** OpenCV's built-in **YuNet** detector + **SFace** recognizer
  (both ONNX, bundled with `opencv-python` — no compiler or extra runtime, which
  matters on Windows where InsightFace/dlib need MSVC build tools). SFace is an
  ArcFace-family model; output is a 128-d L2-normalized embedding.
- Model files (~37 MB) auto-download once from the official `opencv_zoo` repo.
- Output per input image: list of `(bbox, det_score, normed_embedding, landmarks)`.
- The largest / highest-score face is the **probe**; its normalized bbox is reused as a
  native crop hint for Yandex.
- Identity match is a cosine threshold (default **0.36**, the OpenCV SFace reference).

### Stage B — Web / social search  *(genuine search, no hardcoded results)*

Two recall sources, unioned, because they fail differently:

| Source | Engine | Strength | Limit |
|---|---|---|---|
| **Google Lens** | SerpApi `google_lens` | high recall, `knowledge_graph` entity hints | noisy, needs verification |
| **Yandex Images** | SerpApi `yandex_images` with `crop=l;t;r;b` | strongest on faces, native face-region query | ~4 source pages, URL input only |

**Probe hosting.** Reverse-image APIs fetch the probe by URL. It is pinned to IPFS
for the tamper-evidence record, but public IPFS gateways are rate-limited and
SerpApi's Yandex engine rejects them outright ("not publicly accessible"). So for
the *query* the probe is also pushed to a plain image host (`catbox.moe`, no key)
that both engines fetch reliably; the URL used is recorded in the evidence bundle.
If that host is down the pipeline falls back to the IPFS gateway URL (Lens still
works with it; Yandex recall drops).

**Candidate image sourcing.** Logged-out social pages return almost nothing to a
scraper, so the **SerpApi thumbnail** (the engine's own cached copy of the match) is
the primary, always-fetchable candidate image. Each source page is still harvested
best-effort for its HTML hash, `og:image`, author handle, and caption — and the
`og:image` is verified as a second candidate when present.

**Entity router — two paths, logged and surfaced in CLI output:**

- **Path A · entity resolved.** Lens returns a `knowledge_graph` name (or, when it
  omits one, the top `related_content` query as a lower-confidence guess). We run a
  targeted text search (SerpApi `google`) for that person's verified social profiles,
  harvest images from the top profile, and hand them to Stage C. High precision.
- **Path B · no entity.** Union of Lens + Yandex visual matches → fetch each source
  page → extract candidate face images → Stage C. Lower recall; this is the path that
  occasionally works on ordinary people.

Every run records `path_taken`, per-source candidate counts, and query artifacts.

### Stage C — Face re-verification
- For every candidate image: detect faces, embed, compute **cosine similarity** to the
  probe embedding.
- Accept if `cos ≥ THRESHOLD` (default **0.36**, the OpenCV SFace reference value;
  revisit against the recall study below);
  otherwise **reject with the score shown**.
- A knowledge-graph name is *not* proof — lookalikes are common and a profile can
  contain other people. The face re-check is doing real work here, not decoration.
- If nothing clears threshold → clean `NO_MATCH_FOUND` terminal state listing every
  rejected candidate and its score.

### Stage D — Evidence bundle  *(the keystone — schema settled before contract)*
A canonical JSON bundle is assembled and each leaf is hashed into a **Merkle tree**
(`keccak256`, sorted pairs). Leaves:

1. `probe` — sha256 of probe image bytes, embedding digest, bbox, detector version
2. `search` — engine, path taken, raw query params, timestamp, SerpApi request ids
3. `candidate[i]` — source URL, fetched-at, sha256 of the fetched image bytes,
   sha256 of the fetched page HTML, cosine score, accept/reject
4. `match` — the winning candidate's canonical post URL + platform + author handle
5. `run` — pipeline version, model versions, wall-clock, config hash

The **Merkle root**, the bundle CID (IPFS), and a compact summary go on-chain. The full
bundle is pinned to IPFS so re-verification is public.

### Stage E — Blockchain attestation
- **Chain:** Ethereum Sepolia testnet (chain id 11155111) by default — the chain is
  fully configured by `RPC_URL` / `CHAIN_ID` / `EXPLORER_URL`, so any EVM testnet
  (Base Sepolia, etc.) works with no code change.
- **Contract:** `AttestationRegistry.sol` — `attest(bytes32 merkleRoot, string cid,
  bytes32 probeHash, string matchUrl)` emits `Attested(id, attester, merkleRoot, cid,
  probeHash, matchUrl, timestamp)` and stores a struct keyed by autoincrement id.
- No admin, no upgradeability, no token — a minimal append-only notary.

### Stage F — Re-verification  *(demonstrates tamper-evidence)*
`faceprov verify <attestation-id>`:
1. Read the on-chain record (root, CID, timestamp).
2. Fetch the bundle from IPFS by CID; recompute the Merkle root → must equal on-chain.
3. Re-fetch each source page/image; recompute sha256 → flag any leaf whose hash drifted
   ("source page changed since attestation").
4. Re-run SFace on the winning candidate vs. the probe digest → reprint the score.
5. Print a green/red verdict per leaf.

---

## 3. Repository layout

```
src/faceprov/
  face.py            Stage A — detect + encode (OpenCV YuNet + SFace)
  search/
    ipfs.py          Pinata pin (probe + bundle)
    imagehost.py     catbox upload of the probe for the search query
    lens.py          SerpApi Google Lens client (+ related_content fallback)
    yandex.py        SerpApi Yandex Images client (crop param)
    harvest.py       fetch source pages, extract candidate images
    router.py        entity router (Path A / Path B), engine fusion
  verify.py          Stage C — cosine re-verification
  evidence.py        Stage D — canonical bundle + keccak Merkle tree
  _keccak.py         vendored keccak-256 (no-dependency Merkle hashing)
  chain.py           Stage E/F — web3 deploy, attest, read
  pipeline.py        end-to-end orchestration (+ progress hooks) + reverify + tamper_demo
  cli.py             `faceprov run|serve|verify|tamper|deploy`
web/
  server.py          FastAPI: upload -> background job -> streamed progress -> verify/tamper
  index.html         single-page UI (vanilla JS)
contracts/
  AttestationRegistry.sol
  AttestationRegistry.json   committed abi + bytecode (no solc needed to deploy)
scripts/
  deploy.py          deploy to the configured testnet
eval/
  recall_study.py    30-identity recall benchmark
  identities.csv     15 public figures + 15 consenting private individuals
tests/
```

---

## 4. How to run

### Prerequisites
- Python 3.12
- API keys (all free tier): **SerpApi**, **Pinata**, a funded **Ethereum Sepolia** key
  (Google Cloud Web3 / Alchemy / QuickNode faucet)

### Setup
```bash
python -m venv .venv
.venv\Scripts\Activate.ps1          # Windows PowerShell  (bash: . .venv/bin/activate)
pip install -r requirements.txt
pip install -e .                    # exposes the `faceprov` command
copy .env.example .env              # then fill in the keys
```
First `run` auto-downloads the YuNet + SFace models (~37 MB) to `~/.faceprov/models/`.

### Deploy the contract (one time)
```bash
faceprov deploy                     # or: python -m faceprov.cli deploy
# prints the address; paste it into .env as ATTESTATION_REGISTRY_ADDRESS
# (also written to deployments.json)
```

### Run the pipeline (CLI)
```bash
python -m faceprov.cli run --image path/to/face.jpg
```
Output: path taken, candidates + scores, the matched post, the Merkle root, the IPFS
CID, and the on-chain attestation id + explorer link.

### Run the pipeline (web UI)
```bash
faceprov serve            # -> http://127.0.0.1:8000
```
Drop in a face image, watch each stage stream live (detect → pin → search → verify →
bundle → IPFS → on-chain), then hit **Re-verify against chain** and **Tamper test**
from the result page. This is the easiest thing to screen-record.

### Re-verify an attestation
```bash
python -m faceprov.cli verify --id 7
```

### Demonstrate tamper-evidence
```bash
python -m faceprov.cli tamper --id 7
# or target a specific field:
python -m faceprov.cli tamper --id 7 --field candidates.0.best_cosine --value 0.99
```
Fetches the attested bundle, edits one field, recomputes the Merkle root, and shows
it no longer matches the immutable on-chain root.

---

## 5. Recall study  *(honest, measured result)*

`eval/recall_study.py` runs the full pipeline over 30 identities (15 public, 15
consenting private) and reports recall per group, per engine, and per path.

| Group | n | Recall | Yandex-only hits | Path A | Path B |
|---|---|---|---|---|---|
| Public figures | 15 | _TBD_ | _TBD_ | _TBD_ | _TBD_ |
| Private individuals | 15 | _TBD_ | _TBD_ | 0 | _TBD_ |

_(Table filled from a real run before submission. The point is to quantify the
asymmetry rather than hide it.)_

---

## 6. Known limitations

- **Recall on private individuals is low by nature** — they are barely indexed. The
  study measures exactly how low.
- **Search APIs are non-deterministic** — Lens/Yandex results shift over time, so a
  recorded run may not reproduce identically. The evidence bundle captures what was
  seen at attestation time, which is the point.
- **Yandex via SerpApi caps at ~4 source pages** and needs a public image URL.
- **Deepfake robustness is out of scope** — a good synthetic face of a real person can
  pass SFace. This tool records provenance, it is not a liveness/AIGC detector.
- **Sepolia is a testnet** — records are real and explorer-visible but not
  economically secured like mainnet.
- **No hosted website** — `faceprov serve` is a local demo UI (localhost, single
  user, in-memory jobs), not a deployed service. The task requires no website.

---

## 7. Blockchain used

**Ethereum Sepolia** (public testnet, chain id 11155111) by default.
Contract `AttestationRegistry.sol` — live deployment:

| | |
|---|---|
| Address | [`0xB8d2a9E923949EBc6D9Dc4d5e925F5554cEE89C7`](https://sepolia.etherscan.io/address/0xB8d2a9E923949EBc6D9Dc4d5e925F5554cEE89C7) |
| Deploy tx | `0x18d05ff0400b435ee5e051b07be4c7196c5b0110c5b1c30b600ec9ea518f3110` |

Address + ABI + deploy tx are also in `deployments.json`. Run `faceprov deploy`
to deploy your own instance.

The chain is not hard-coded — set `RPC_URL` / `CHAIN_ID` / `EXPLORER_URL` in `.env`
to deploy the same contract to Base Sepolia, Optimism Sepolia, a local Anvil node,
or any other EVM chain.

---

## 8. License

MIT.

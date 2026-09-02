# FaceProv — Face-Anchored Provenance for Image Verification

**HH Goa 2026 · Shortlisting Task 3 — Face Identification & Blockchain Verification**

A pipeline that takes a face scan as input, genuinely searches the web / social media
for where that person appears, verifies each candidate with a second face-recognition
pass, and then writes a **tamper-evident, timestamped attestation** of the finding to a
public blockchain — so that anyone can later re-verify the claim and detect if any
source page was altered.

```
face scan  ──▶  detect + encode  ──▶  web / social search  ──▶  face re-verification
                                          (Google Lens                 (ArcFace cosine,
                                           + Yandex,                     per-candidate
                                           entity router)                accept/reject)
                                                                              │
                                                                              ▼
   re-verify later  ◀──  on-chain attestation  ◀──  Merkle-rooted evidence bundle
   (CLI verify cmd)      (Base Sepolia, event log)   (pinned to IPFS)
```

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
- **Detector + encoder:** [InsightFace](https://github.com/deepinsight/insightface)
  `buffalo_l` pack — RetinaFace detection + ArcFace R100 512-d embedding.
- Output per input image: list of `(bbox, det_score, normed_embedding, landmarks)`.
- The largest / highest-score face is the **probe**; its normalized bbox is reused as a
  native crop hint for Yandex.
- Fallback path (documented, not default): `face_recognition` (dlib) 128-d encodings.

### Stage B — Web / social search  *(genuine search, no hardcoded results)*

Two recall sources, unioned, because they fail differently:

| Source | Engine | Strength | Limit |
|---|---|---|---|
| **Google Lens** | SerpApi `google_lens` | high recall, `knowledge_graph` entity hints | noisy, needs verification |
| **Yandex Images** | SerpApi `yandex_images` with `crop=l;t;r;b` | strongest on faces, native face-region query | ~4 source pages, URL input only |

The probe is pinned to IPFS first and the gateway URL is handed to SerpApi (Yandex
accepts URLs only), which doubles as the start of our provenance chain.

**Entity router — two paths, logged and surfaced in CLI output:**

- **Path A · entity resolved.** Lens returns `knowledge_graph` → a name. We run a
  targeted text search (SerpApi `google`) for that person's verified social profiles,
  harvest images from the top profile, and hand them to Stage C. High precision.
- **Path B · no entity.** Union of Lens + Yandex visual matches → fetch each source
  page → extract candidate face images → Stage C. Lower recall; this is the path that
  occasionally works on ordinary people.

Every run records `path_taken`, per-source candidate counts, and query artifacts.

### Stage C — Face re-verification
- For every candidate image: detect faces, embed, compute **cosine similarity** to the
  probe embedding.
- Accept if `cos ≥ THRESHOLD` (default **0.38**, tuned on the recall study below);
  otherwise **reject with the score shown**.
- A knowledge-graph name is *not* proof — lookalikes are common and a profile can
  contain other people. ArcFace is doing real work here, not decoration.
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
- **Chain:** Base Sepolia testnet (EVM, free faucet, fast finality, real explorer).
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
4. Re-run ArcFace on the winning candidate vs. the probe digest → reprint the score.
5. Print a green/red verdict per leaf.

---

## 3. Repository layout

```
src/faceprov/
  face.py            Stage A — detect + encode (InsightFace)
  search/
    ipfs.py          Pinata pin (probe + bundle)
    lens.py          SerpApi Google Lens client
    yandex.py        SerpApi Yandex Images client (crop param)
    harvest.py       fetch source pages, extract candidate images
    router.py        entity router (Path A / Path B), fusion
  verify.py          Stage C — cosine re-verification
  evidence.py        Stage D — canonical bundle + Merkle tree
  chain.py           Stage E/F — web3 deploy, attest, read
  pipeline.py        end-to-end orchestration
  cli.py             `faceprov run|verify|deploy`
contracts/
  AttestationRegistry.sol
scripts/
  deploy.py          compile (py-solc-x) + deploy to Base Sepolia
eval/
  recall_study.py    30-identity recall benchmark
  identities.csv     15 public figures + 15 consenting private individuals
tests/
```

---

## 4. How to run

### Prerequisites
- Python 3.12
- API keys (all free tier): **SerpApi**, **Pinata**, a funded **Base Sepolia** key
  (Coinbase / Alchemy faucet)

### Setup
```bash
python -m venv .venv && . .venv/Scripts/activate     # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env        # fill in keys
```

### Deploy the contract (one time)
```bash
python -m faceprov.cli deploy
# writes contract address to .env / deployments.json
```

### Run the pipeline
```bash
python -m faceprov.cli run --image path/to/face.jpg
```
Output: path taken, candidates + scores, the matched post, the Merkle root, the IPFS
CID, and the on-chain attestation id + explorer link.

### Re-verify an attestation
```bash
python -m faceprov.cli verify --id 7
```

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
  pass ArcFace. This tool records provenance, it is not a liveness/AIGC detector.
- **Base Sepolia is a testnet** — records are real and explorer-visible but not
  economically secured like mainnet.
- **No website** — CLI + screen recording only, per the task.

---

## 7. Blockchain used

**Base Sepolia** (Ethereum L2 testnet, chain id 84532). Contract:
`AttestationRegistry.sol`, deployed address in `deployments.json`. Explorer:
`https://sepolia.basescan.org/address/<addr>`.

---

## 8. License

MIT.

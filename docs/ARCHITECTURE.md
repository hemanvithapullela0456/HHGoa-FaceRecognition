# FaceProv — Architecture & Design Record

> Complete technical documentation of the project: what every module does, how the
> pipeline runs end to end, every significant design decision and the alternatives
> weighed against it, the data formats, the threat model, and the known limits.
>
> Read [README.md](../README.md) first for the one-page summary. This document is the
> deep version.

---

## Table of contents

1. [What this is and why it's shaped this way](#1-what-this-is-and-why-its-shaped-this-way)
2. [System overview](#2-system-overview)
3. [End-to-end walkthrough (stages A–F)](#3-end-to-end-walkthrough-stages-af)
4. [Module reference](#4-module-reference)
5. [The evidence bundle & Merkle commitment](#5-the-evidence-bundle--merkle-commitment)
6. [The smart contract](#6-the-smart-contract)
7. [The web UI](#7-the-web-ui)
8. [Design decisions & alternatives considered](#8-design-decisions--alternatives-considered)
9. [Threat model — what an attestation does and does not prove](#9-threat-model)
10. [Known limitations & failure modes](#10-known-limitations--failure-modes)
11. [Cost & quota analysis](#11-cost--quota-analysis)
12. [Testing](#12-testing)
13. [Annotated real run](#13-annotated-real-run)
14. [Extending the project](#14-extending-the-project)
15. [File map](#15-file-map)

---

## 1. What this is and why it's shaped this way

### 1.1 The task

HH Goa 2026 Shortlisting Task 3 asks for a pipeline:

> **face scan input → web/social-media search (find a matching post) → blockchain
> upload/verification of the discovered data**, end to end, with the ability to
> re-verify the data against the on-chain record.

### 1.2 The framing decision

The obvious reading of the task is "identify a stranger from a photo." We deliberately
did **not** build that, for two reasons — one practical, one ethical — and they point
the same way.

**Practical: the index-density asymmetry.** Reverse-image face search only works if the
person's face is already crawled and indexed somewhere. That is true of public figures
(thousands of images across news, Wikipedia, fan wikis) and mostly false for a private
individual (a handful of photos, often behind logins, frequently uncrawled). No amount
of embedding quality fixes an empty index. A "identify anyone" product is therefore
weak exactly where a demo would test it (the judge's own face).

**Ethical:** a working stranger-identifier is a surveillance tool.

**The inversion.** The population we *can* get recall on — public figures — is also the
population that miscaptioned images, deepfakes, and "is this really them?" disputes
actually target. So the product is:

> **Face-anchored provenance for image verification.** Given a face in a circulating
> image, find where that person genuinely appears online, and produce an immutable,
> timestamped record that *at time T, this face matched these sources at these
> similarity scores* — independently re-verifiable by anyone, and provably broken if a
> source page is later altered.

This satisfies every line of the task spec unchanged (face detection/encoding → genuine
search → blockchain record → re-verification) while being a defensible tool: an
evidence-integrity aid for journalists and OSINT researchers, not a people-search
engine. It also makes the blockchain layer *the point* rather than a bolt-on: the value
is the tamper-evident commitment, not the lookup.

---

## 2. System overview

```
                          ┌─────────────────────── faceprov (Python package) ───────────────────────┐
                          │                                                                          │
  face image  ──────────▶ │  face.py        detect + encode (OpenCV YuNet + SFace, 128-d)            │
                          │      │                                                                   │
                          │      ▼                                                                   │
                          │  imagehost.py   upload probe to catbox.moe (search needs a fetchable URL)│
                          │  ipfs.py        pin probe to IPFS via Pinata (the record copy)           │
                          │      │                                                                   │
                          │      ▼                                                                   │
                          │  search/        lens.py + yandex.py  (SerpApi reverse-image)             │
                          │  router.py      entity router: Path A (name→profiles) / Path B (visual)  │
                          │      │          harvest.py fetches each source page (best-effort)        │
                          │      ▼                                                                   │
                          │  verify.py      re-detect + SFace cosine vs probe, per candidate         │
                          │      │          accept if cos ≥ threshold (0.36)                          │
                          │      ▼                                                                   │
                          │  evidence.py    canonical JSON bundle → keccak256 Merkle tree → root     │
                          │  ipfs.py        pin the full bundle to IPFS                               │
                          │      │                                                                   │
                          │      ▼                                                                   │
                          │  chain.py       AttestationRegistry.attest(root, probeHash, cid, url)    │
                          │                 → Ethereum Sepolia (chain 11155111)                       │
                          └──────────┬───────────────────────────────────────────────────────────────┘
                                     │
              re-verify (pipeline.reverify)          tamper demo (pipeline.tamper_demo)
              read chain → fetch bundle from IPFS     fetch bundle → edit one field →
              → recompute root (== on-chain?)         recompute root (≠ on-chain) → detected
              → re-hash every source URL
```

Two front ends call the same `faceprov.pipeline` functions:

| Front end | Entry | Notes |
|---|---|---|
| CLI | `faceprov` / `python -m faceprov.cli` | `run`, `serve`, `verify`, `tamper`, `deploy` |
| Web UI | `faceprov serve` → `web/server.py` (FastAPI) | upload → background thread → polled progress |

### 2.1 External services

| Service | Used for | Auth | Free tier |
|---|---|---|---|
| **SerpApi** | Google Lens + Yandex Images reverse search, Google text search | `SERPAPI_KEY` | 250 searches/mo |
| **Pinata** | pin probe image + evidence bundle to IPFS; read bundle back via gateway | `PINATA_JWT` | 500 files / 1 GB |
| **catbox.moe** | temporary public hosting of the probe for the search query | none | permanent, no key |
| **Ethereum Sepolia** | the attestation ledger | `DEPLOYER_PRIVATE_KEY` (funded) | faucet ETH |
| **opencv_zoo (GitHub)** | one-time model download (YuNet + SFace ONNX) | none | — |

---

## 3. End-to-end walkthrough (stages A–F)

The orchestrator is [`faceprov/pipeline.py`](../src/faceprov/pipeline.py). Everything
below happens inside `run_pipeline(image_path, cfg, attest=True, progress=…)`.

### Stage A — face detection & encoding  (`face.py`)

1. Read the image unicode-safely (`np.fromfile` + `cv2.imdecode` — plain `cv2.imread`
   mishandles non-ASCII Windows paths).
2. **YuNet** (`cv2.FaceDetectorYN`) detects faces → for each: a bounding box, a
   detection score, and 5 landmark points.
3. For each detection, **SFace** (`cv2.FaceRecognizerSF`) does `alignCrop` (warps the
   face to a canonical 112×112 using the landmarks) then `feature` → a 128-float
   embedding, which we L2-normalize.
4. The **probe** is the face with the largest bounding-box area (tie-break: detection
   score). Its normalized bbox `(x1,y1,x2,y2)` in `[0,1]` is kept — Yandex will use it
   as a native crop hint.

Key data structure — `face.Face`:

```python
@dataclass
class Face:
    bbox:        tuple[float,float,float,float]   # pixels
    bbox_norm:   tuple[float,float,float,float]   # [0,1], reused as the Yandex crop
    det_score:   float
    embedding:   np.ndarray                       # 128-d, L2-normalized
    landmarks:   list[list[float]]                # 5 points
    embedding_digest -> str                       # sha256 of the rounded embedding
```

`cosine(a, b)` = dot product of the two normalized vectors, in `[-1, 1]`.

### Stage A′ — hosting the probe

The probe image needs to be at a URL that (a) SerpApi's crawlers can fetch and (b) is
recorded permanently.

- **`ipfs.Pinata.pin_bytes`** pins the raw image → CID. This is the *record* copy; it
  goes into the evidence bundle as `probe.image_cid`.
- **`imagehost.host_probe`** uploads the same bytes to `catbox.moe` and returns a
  direct `https://files.catbox.moe/<id>.jpg` URL. This is the *query* URL handed to the
  search engines. If catbox is unreachable it falls back to the Pinata gateway URL
  (Google Lens still accepts that; Yandex does not — see §8.4).

### Stage B — web / social search  (`search/router.py`)

`run_search(probe, query_url, api_key, threshold, max_candidates, progress)`:

1. **Google Lens** (`lens.search`) — `engine=google_lens&url=<query_url>`. Returns
   `visual_matches` (title, source domain, page URL, thumbnail image URL) and — if the
   person is recognized — an entity name via `knowledge_graph`, or failing that, the
   top `related_content` query as a lower-confidence guess.
2. **Yandex Images** (`yandex.search`) — `engine=yandex_images&url=<query_url>&crop=<l;t;r;b>`.
   The `crop` param restricts the reverse search to the probe's face region. Returns
   `image_results` (title, source domain, page URL, thumbnail object).
3. **Path selection:**
   - **Path A — `A:entity`** — an entity name was resolved. Call `lens.find_social_profiles(name)`:
     a Google text search `"<name> (site:instagram.com OR site:x.com OR …)"` →
     candidate profile page URLs. These become the first candidate specs.
   - **Path B — `B:visual`** — no entity. Only the visual matches are used.
4. **Candidate fusion** — Lens and Yandex visual matches are *interleaved*
   (`_interleave`) so a long Lens list can't consume the whole budget before Yandex
   gets a look. For each visual match, two candidate specs are produced:
   - the **SerpApi thumbnail** URL (`image_origin="serpapi_thumbnail"`) — the engine's
     own cached copy, reliably fetchable;
   - the page's **`og:image`** (`image_origin="og:image"`) — discovered by harvesting.
5. Specs are de-duplicated and truncated to `2 × max_candidates`.
6. **Verification loop** — for each spec, up to `max_candidates` verifications:
   - `harvest.harvest(source_url)` fetches the page once (cached per URL): HTML bytes,
     `og:image`/`twitter:image`, author handle (from URL path or `og:title`), title,
     description, and a platform label (`instagram`, `x`, …) if the domain matches.
   - `verify.verify_candidate(...)` (Stage C) is called with the candidate image URL.
   - The loop **exits early** as soon as an accepted candidate sits on a recognized
     social platform.

Output — `router.SearchOutcome`:

```python
path_taken        "A:entity" | "B:visual"
entity_name       str | None
entity_type       "knowledge_graph" type | "related_content_guess" | None
lens_meta         dict   # search id, had_knowledge_graph, related queries, ai_overview flag
yandex_meta       dict   # search id, crop string
social_profiles   list[dict]   # Path A: the profile search results
candidates        list[CandidateResult]     # everything verified
accepted          list[CandidateResult]     # cos ≥ threshold
best  (property)  the accepted candidate, preferring one on a real platform, then max cosine
```

### Stage C — face re-verification  (`verify.py`)

`verify_candidate(probe, image_url, source_url, engine, image_origin, threshold, …)`:

1. Download the candidate image (`requests`, browser-ish UA). On failure → a
   `CandidateResult` with `best_cosine = -1.0`, `accepted = False`, a `note`.
2. `image_sha256` = sha256 of the exact bytes fetched. `page_sha256` = sha256 of the
   harvested page HTML (or `None`).
3. Write the bytes to a temp file, run `face.detect_and_encode` on it.
4. If no face → rejected with a note. Otherwise `best_cosine` = the **max** cosine
   between the probe embedding and any face found in the candidate image.
5. `accepted = best_cosine ≥ threshold` (default **0.36**).

`CandidateResult` carries: `source_url, image_url, image_origin, engine, fetched_at,
image_sha256, page_sha256, best_cosine, accepted, n_faces, platform, author_handle,
caption, note`. `as_leaf()` returns it as a plain dict — this becomes a Merkle leaf.

### Stage D — evidence bundle & Merkle root  (`evidence.py`)

`pipeline.run_pipeline` assembles an `EvidenceBundle` with five section types:

| Section | Contents |
|---|---|
| `probe` | image sha256, IPFS CID + URL, embedding digest, bbox (pixels + normalized), detection score, detector/encoder version strings |
| `search` | path taken, entity name/type, query image URL + host, Lens metadata, Yandex metadata, Path-A profile results, threshold, timestamp |
| `candidate[i]` | one per verified candidate — the full `CandidateResult` leaf |
| `match` | if matched: the winning candidate's URL, image URL + sha256, page sha256, cosine, engine, platform, handle, caption. If not: `matched:false`, reason, and every rejected candidate with its score |
| `run` | faceprov version, wall-clock seconds, a config hash, timestamp |

The bundle is serialized to a canonical JSON document (`to_json()`), and its
**Merkle root** is computed (see §5). The full document is pinned to IPFS
(`Pinata.pin_json`) → `bundle_cid`.

### Stage E — on-chain attestation  (`chain.py`)

If `attest` and both `registry_address` and `deployer_key` are set:

```
AttestationRegistry.attest(
    merkleRoot = <bundle root, bytes32>,
    probeHash  = <probe image sha256, bytes32>,
    cid        = <bundle_cid, string>,
    matchUrl   = <matched post URL, or "NO_MATCH">
)
```

`Chain.attest` builds an EIP-1559 transaction (`maxFeePerGas = 2 × gas_price`,
`maxPriorityFeePerGas = 1 gwei`, gas = `estimate_gas × 1.2`), signs it locally with
`eth_account`, sends the raw transaction, waits for the receipt, and reads the emitted
`Attested` event to get the auto-incrementing attestation `id`.

Returns `{id, tx, explorer, block}`.

### Stage F — re-verification & tamper demo  (`pipeline.py`)

**`reverify(attestation_id, cfg)`:**

1. `Chain.get(id)` → the on-chain struct: `attester, merkleRoot, probeHash, cid,
   matchUrl, timestamp`.
2. `Pinata.fetch_json(cid)` → the evidence bundle (via the gateway).
3. Rebuild `EvidenceBundle.from_json` and recompute the root. **Check 1:** does it
   equal the on-chain root?
4. For every `candidate[i]`: re-download `image_url`, re-hash, compare to the recorded
   `image_sha256`. A mismatch means *that source changed since attestation*.
5. For the winning `match`: re-download the image and re-run face detection (a hook for
   a full cosine re-check).
6. `verdict = PASS` iff Check 1 holds and no source hash regressed to a definite
   mismatch (unreachable sources are `skip`, not `fail`).

**`tamper_demo(attestation_id, cfg, field_path=None, value=None)`:**

1. Fetch the on-chain root and the bundle.
2. Recompute the original root — it should match the chain.
3. Deep-copy the bundle, mutate **one field** (`set_leaf_path`, e.g.
   `match.source_url` gets `?evil=1` appended, or `candidates.0.best_cosine` bumped).
4. Recompute the root of the mutated bundle → it no longer matches the chain.
5. Return both roots and the `original_matches_chain` / `tampered_matches_chain` flags.

---

## 4. Module reference

### `faceprov/config.py`

`Config` is a frozen dataclass loaded from environment / `.env` (via `python-dotenv`).

| Field | Env var(s) | Default |
|---|---|---|
| `serpapi_key` | `SERPAPI_KEY` | required for search |
| `pinata_jwt` | `PINATA_JWT` | required for search |
| `pinata_gateway` | `PINATA_GATEWAY` | `https://gateway.pinata.cloud` |
| `rpc_url` | `RPC_URL` \| `BASE_SEPOLIA_RPC` \| `SEPOLIA_RPC` | `https://ethereum-sepolia-rpc.publicnode.com` |
| `chain_id` | `CHAIN_ID` | `11155111` |
| `explorer_url` | `EXPLORER_URL` | `https://sepolia.etherscan.io` |
| `deployer_key` | `DEPLOYER_PRIVATE_KEY` | required for chain writes |
| `registry_address` | `ATTESTATION_REGISTRY_ADDRESS` | (from `deployments.json` if unset) |
| `match_threshold` | `FACEPROV_MATCH_THRESHOLD` | `0.36` |
| `max_candidates` | `FACEPROV_MAX_CANDIDATES` | `12` |

`Config.load(require_chain=…, require_search=…)` — the flags control which vars are
*required* vs merely read. `verify`/`tamper`/`serve` load with `require_chain=False`
so they work with only a funded key absent (read-only chain access).

The chain is **fully abstract**: `RPC_URL` / `CHAIN_ID` / `EXPLORER_URL` are the only
things that bind it to Ethereum Sepolia. Point them at Base Sepolia, Optimism Sepolia,
a local Anvil node, or mainnet and nothing else changes.

### `faceprov/face.py`

- `MODEL_DIR = ~/.faceprov/models/` — YuNet (`face_detection_yunet_2023mar.onnx`,
  ~230 KB) and SFace (`face_recognition_sface_2021dec.onnx`, ~37 MB) are downloaded
  once from `github.com/opencv/opencv_zoo/raw/main/models/...`.
- `_detector()` / `_recognizer()` — `functools.lru_cache(maxsize=1)`, so the ONNX
  graphs load once per process.
- `detect_and_encode(path) -> list[Face]` — the workhorse.
- `probe_face(path) -> Face` — largest face; raises `ValueError` if none.
- `DETECTOR_VERSION = "opencv/yunet-2023mar"`, `ENCODER_VERSION = "opencv/sface-2021dec"`
  — recorded in the bundle so a verifier knows which models produced the embedding.

### `faceprov/_keccak.py`

A dependency-free, pure-Python **Keccak-256** (Ethereum's hash — *not* NIST SHA3-256;
they differ in the padding byte). ~90 lines implementing the Keccak-f[1600] permutation.
Verified against the known vectors `keccak256("") = c5d2460186f7233c…` and
`keccak256("abc") = 4e03657aea45a94f…`. Used so `evidence.py` and its tests run with
only the standard library — `web3` is not needed just to hash a Merkle tree.

### `faceprov/evidence.py`

Covered in §5. Public surface: `sha256_bytes`, `keccak`, `merkle_root`, `merkle_proof`,
`set_leaf_path`, and the `EvidenceBundle` dataclass (`leaves()`, `root()`, `to_json()`,
`from_json()`).

### `faceprov/verify.py`

`CandidateResult` dataclass + `verify_candidate(...)`. Covered in Stage C.

### `faceprov/search/ipfs.py`

`Pinata(jwt, gateway)`:
- `pin_bytes(data, name) -> cid` — `POST /pinning/pinFileToIPFS` (multipart).
- `pin_json(obj, name) -> cid` — `POST /pinning/pinJSONToIPFS`.
- `gateway_url(cid) -> str` — `"{gateway}/ipfs/{cid}"`.
- `fetch_json(cid) -> dict` — GET the gateway URL.

Only `pinFileToIPFS` + `pinJSONToIPFS` scopes are needed; reads use the public gateway.

### `faceprov/search/imagehost.py`

`upload_catbox(data, filename) -> url` — `POST https://catbox.moe/user/api.php` with
`reqtype=fileupload`. `host_probe(data, fallback_url) -> (url, host_label)` — try
catbox, fall back to `fallback_url` on any exception.

### `faceprov/search/lens.py`

- `VisualMatch` dataclass — `title, source, source_url, thumbnail, engine`.
- `LensResult` — `entity_name, entity_type, matches, raw_search_metadata`.
- `search(image_url, api_key) -> LensResult` — parses `visual_matches`,
  `knowledge_graph` (dict *or* list), and `related_content` (entity fallback).
- `find_social_profiles(name, api_key) -> list[dict]` — Path A profile lookup.

### `faceprov/search/yandex.py`

- `search(image_url, api_key, bbox_norm) -> (list[VisualMatch], meta)` — builds the
  `crop=l;t;r;b` param from the probe bbox; parses `image_results` (note the
  `thumbnail` field is an **object** `{link, serpapi_link}`, not a string).

### `faceprov/search/harvest.py`

- `platform_of(url) -> str | None` — domain → platform label.
- `harvest(url, retries=1) -> HarvestedPage` — GET with a real browser UA + Accept
  headers, 1 retry, `allow_redirects`. Parses with BeautifulSoup/lxml:
  `og:image`/`twitter:image`, title, description, author handle, all `<img>` srcs.
  Never raises — returns a `HarvestedPage` with `.error` set on failure.

### `faceprov/search/router.py`

The entity router and candidate-fusion logic. `run_search(...)` covered in Stage B.
Helpers: `_interleave`, `_dedupe_specs`. Data: `CandidateSpec`, `SearchOutcome`.

### `faceprov/chain.py`

- `_artifact()` — loads `contracts/AttestationRegistry.json` (committed abi+bytecode);
  falls back to compiling `AttestationRegistry.sol` with `py-solc-x` if the JSON is
  missing.
- `Chain(rpc_url, chain_id, explorer_url, private_key=None)` /
  `Chain.from_config(cfg, signer=bool)`.
- `deploy() -> {address, abi, deploy_tx, chain_id, explorer}` — also writes
  `deployments.json`.
- `attest(address, merkle_root, probe_hash, cid, match_url) -> {id, tx, explorer, block}`.
- `get(address, id) -> {attester, merkle_root, probe_hash, cid, match_url, timestamp}`.
- `_b32(hexstr)` — normalizes a sha256 hex string to a `0x`-prefixed 32-byte value for
  the `bytes32` contract param.

### `faceprov/pipeline.py`

`run_pipeline`, `reverify`, `tamper_demo`. The `progress: Callable[[str, str], None]`
argument is called at every stage boundary (`detect`, `pin_probe`, `host_probe`,
`search`, `verify`, `bundle`, `pin_bundle`, `attest`, `done`) and threaded into
`run_search` for per-candidate lines. The CLI passes nothing (silent); the web server
passes a collector.

### `faceprov/cli.py`

`typer` app. Commands: `run`, `serve`, `verify`, `tamper`, `deploy`. On import it
forces `sys.stdout`/`sys.stderr` to UTF-8 (`reconfigure`) because Windows consoles are
cp1252 and `rich`'s box/emoji glyphs would crash mid-render otherwise.

### `web/server.py`, `web/index.html`

Covered in §7.

---

## 5. The evidence bundle & Merkle commitment

### 5.1 Why a Merkle tree and not one hash

A single `sha256(bundle)` would also be tamper-evident. The Merkle tree buys one thing:
**selective disclosure with proofs.** A verifier — or a future feature — can prove that
*one specific candidate* (`candidate[3]`, say) was part of the attested set, by
presenting that leaf plus a `merkle_proof` (a ~4-hash sibling path), without revealing
the rest of the bundle. `merkle_proof(leaves, index)` and the sorted-pair verification
are implemented and tested (`tests/test_evidence.py::test_merkle_proof_verifies`).

### 5.2 Construction (exactly reproducible)

1. **Sections in fixed order:** `probe`, `search`, `candidate[0]`, `candidate[1]`, …,
   `match`, `run`. This order is part of the schema and is recorded in the bundle as
   `merkle.leaf_order`.
2. **Canonical serialization:** each section is `json.dumps(section, sort_keys=True,
   separators=(",",":"))` — sorted keys, no whitespace, deterministic.
3. **Leaf hash:** `keccak256(b"faceprov-leaf:" + section_name + b":" + canonical_json)`.
   The section name is mixed in as a **domain separator** so an attacker can't move a
   payload from one section to another and keep the same leaf.
4. **Tree:** pairwise `keccak256`, **sibling pair sorted** before hashing
   (`lo, hi = sorted(a, b)`), odd level → last node duplicated. This is the
   OpenZeppelin `MerkleProof` convention, so an on-chain `verify` would work unchanged.
5. **Root:** `"0x" + root.hex()`, a `bytes32`.

### 5.3 Bundle JSON shape (`schema: "faceprov/evidence-bundle/v1"`)

```jsonc
{
  "schema": "faceprov/evidence-bundle/v1",
  "faceprov_version": "0.1.0",
  "merkle": { "algo": "keccak256", "pairing": "sorted",
              "leaf_order": ["probe","search","candidate[0]",...,"match","run"],
              "root": "0x…" },
  "probe":  { "image_sha256":"0x…","image_cid":"Qm…","image_ipfs_url":"https://…",
              "embedding_digest":"0x…","bbox_pixels":[…],"bbox_norm":[…],
              "det_score":0.92,"detector":"opencv/yunet-2023mar",
              "encoder":"opencv/sface-2021dec" },
  "search": { "path_taken":"A:entity","entity_name":"…","entity_type":"…",
              "query_image_url":"https://files.catbox.moe/…","query_image_host":"catbox.moe",
              "lens_meta":{…},"yandex_meta":{…},"social_profiles":[…],
              "threshold":0.36,"searched_at":"2026-…Z" },
  "candidates":[ { "source_url":"…","image_url":"…","image_origin":"serpapi_thumbnail",
                   "engine":"yandex_images","fetched_at":"…","image_sha256":"0x…",
                   "page_sha256":"0x…","best_cosine":0.79,"accepted":true,"n_faces":1,
                   "platform":null,"author_handle":null,"caption":null,"note":"" }, … ],
  "match":  { "matched":true,"source_url":"…","image_url":"…","image_origin":"…",
              "image_sha256":"0x…","page_sha256":"0x…","cosine":0.79,"engine":"…",
              "platform":null,"author_handle":null,"caption":null,"note":"" },
  "run":    { "faceprov_version":"0.1.0","wall_clock_s":29.8,"config_hash":"…",
              "created_at":"2026-…Z" },
  "_pinned_cid": "Qm…"   // added after pinning; not part of the hashed sections
}
```

### 5.4 What goes on-chain vs off-chain

| On-chain (immutable, ~cheap) | Off-chain (IPFS, mutable but hash-committed) |
|---|---|
| `merkleRoot` (bytes32) | the full evidence bundle JSON |
| `probeHash` (bytes32) | the probe image bytes |
| `cid` (string — the IPFS pointer) | every candidate image + page HTML (by reference; only their hashes are in the bundle) |
| `matchUrl` (string) | |
| `attester` (msg.sender), `timestamp` (block time) | |

The chain stores *commitments*; IPFS stores *content*; the bundle stores *hashes of
external content*. Re-verification walks all three layers.

---

## 6. The smart contract

[`contracts/AttestationRegistry.sol`](../contracts/AttestationRegistry.sol) — Solidity
`^0.8.20`, compiled with `solc 0.8.24` (optimizer, 200 runs).

```solidity
struct Attestation {
    address attester;
    bytes32 merkleRoot;
    bytes32 probeHash;
    string  cid;
    string  matchUrl;
    uint256 timestamp;
}

function attest(bytes32 merkleRoot, bytes32 probeHash, string calldata cid, string calldata matchUrl)
    external returns (uint256 id);          // requires merkleRoot != 0 and cid non-empty
function get(uint256 id) external view returns (Attestation memory);
function count() external view returns (uint256);

event Attested(uint256 indexed id, address indexed attester,
               bytes32 merkleRoot, bytes32 probeHash,
               string cid, string matchUrl, uint256 timestamp);
```

Design: **minimal append-only notary.** No owner, no admin functions, no upgrade proxy,
no token, no pausing, no delete. `_attestations` is a private array; `id` is its index.
Anyone can `attest`; nobody can alter or remove a record. The event mirrors the struct
so an indexer never needs storage reads.

**Live deployment (Ethereum Sepolia, chain 11155111):**

| | |
|---|---|
| Address | `0xB8d2a9E923949EBc6D9Dc4d5e925F5554cEE89C7` |
| Deploy tx | `0x18d05ff0400b435ee5e051b07be4c7196c5b0110c5b1c30b600ec9ea518f3110` |
| Explorer | https://sepolia.etherscan.io/address/0xB8d2a9E923949EBc6D9Dc4d5e925F5554cEE89C7 |

`contracts/AttestationRegistry.json` holds the compiled ABI + bytecode and is committed,
so `faceprov deploy` needs **no Solidity toolchain** — `py-solc-x` is a fallback only.

---

## 7. The web UI

`faceprov serve` → `web/server.py` (FastAPI + uvicorn) serving `web/index.html`.

### Endpoints

| Method + path | Purpose |
|---|---|
| `GET /` | the single-page UI |
| `GET /api/config` | chain id, explorer URL, registry address, threshold — for the header |
| `POST /api/run` | multipart `image` + `attest` form field → writes a temp file, spawns a **daemon thread** running `run_pipeline` with a progress collector, returns `{job_id}` |
| `GET /api/jobs/{job_id}` | `{status, events[], result, error, trace}` — polled every 1 s by the page |
| `POST /api/verify/{id}` | `pipeline.reverify` |
| `POST /api/tamper/{id}` | `pipeline.tamper_demo` |

Jobs live in an in-process `dict` (`JOBS`). No database, no queue — single user, single
machine. `Config.load(require_chain=attest)` inside the job means an on-chain run with
no funded key fails loudly into `job["error"]` + `job["trace"]`, surfaced in the UI.

### Front end (`web/index.html`)

One file, vanilla JS, no build step, no framework, no external assets. Dark theme.
Sections: drag/drop upload with preview → live pipeline log (stage badge + elapsed
seconds per line) → result panel (verdict banner, summary tiles, matched-post card,
candidate table, Merkle root, IPFS link, on-chain card) → **Re-verify** and
**Tamper test** buttons that call the corresponding endpoints and render their results
inline.

Progress rendering: the page keeps a `seen` counter and only appends
`events.slice(seen)` each poll, so the log streams smoothly.

---

## 8. Design decisions & alternatives considered

Each subsection: **what we chose**, **what else was on the table**, **why**.

### 8.1 Face detection & recognition — OpenCV YuNet + SFace

**Chosen:** OpenCV's built-in `FaceDetectorYN` (YuNet) + `FaceRecognizerSF` (SFace),
both shipped as ONNX inside `opencv-python`, models auto-downloaded from `opencv_zoo`.
128-d embedding, cosine threshold 0.36 (the OpenCV reference value for SFace).

| Alternative | Why not |
|---|---|
| **InsightFace `buffalo_l` (RetinaFace + ArcFace R100, 512-d)** — the original plan, and a stronger model | On Windows the `insightface` wheel builds a Cython extension (`mesh_core_cython`) and **fails without Visual C++ Build Tools**. The whole `pip install` aborts. A grader on a clean Windows box would be stuck at step one. This was discovered during setup and forced the swap. |
| **`face_recognition` / dlib (128-d)** | dlib has the *same* Windows compiler problem (CMake + MSVC). Prebuilt wheels are unreliable across Python versions. |
| **DeepFace** | Pulls TensorFlow — heavy, slow first import, its own Windows friction. |
| **Cloud APIs — AWS Rekognition, Azure Face, Face++** | Adds a paid account + key, network dependency for a step that should be local, and "send every face to a third party" is at odds with the privacy framing. |
| **MediaPipe / MTCNN for detection** | Fine for detection, but then you still need a separate recognition model — SFace via OpenCV gives both with zero extra dependencies. |

**Trade-off accepted:** SFace is somewhat weaker than ArcFace R100. For public-figure
photos at threshold 0.36 it is entirely adequate, and the `detector`/`encoder` version
strings in the bundle mean the choice is auditable and swappable. The InsightFace path
is documented as an opt-in for anyone who has the build tools.

### 8.2 Search provider — SerpApi, two engines

**Chosen:** SerpApi as the single gateway to **Google Lens** (`google_lens`) and
**Yandex Images** (`yandex_images`), plus a Google text search for Path A.

| Alternative | Why not |
|---|---|
| **Scrape Google Images / Lens / Yandex directly** | Brittle, needs headless browsers + proxies + CAPTCHA handling, ToS-hostile, and would break during judging. SerpApi turns all of that into one HTTP call and one key. |
| **Bing Visual Search API** | Bing's face recall is weaker than Yandex's; adds another key. Kept as a documented future recall source (`bing_reverse_image`). |
| **PimEyes / FaceCheck.ID** (dedicated face search engines) | These are exactly the "identify any stranger" tools the framing rejects. They're also paid, ToS-restricted, and not API-clean. |
| **TinEye** | Excellent for exact-image matches, poor at "same person, different photo" — wrong tool for a face. |
| **One engine only (Google Lens)** | Google Lens has high recall but its results skew to stock/product/meme pages and it often omits the entity. Yandex is materially stronger on *faces* specifically and supports a native face-region crop. They fail differently, so the union is worth the second API call. |

**Why the two engines are unioned and interleaved:** in testing, Google Lens returned
59 matches and Yandex 89 for the same probe; a naive "Lens first" ordering exhausted
the `max_candidates` budget before Yandex was consulted. `_interleave` guarantees both
engines are represented.

### 8.3 Entity resolution — knowledge_graph, then related_content

**Chosen:** use `knowledge_graph.title` from Google Lens when present; otherwise fall
back to the top `related_content` query as a lower-confidence guess (recorded as
`entity_type="related_content_guess"`).

| Alternative | Why not |
|---|---|
| **Only trust `knowledge_graph`** | Google Lens increasingly omits it even for recognizable people (it now returns an `ai_overview` block instead). Path A would almost never fire. |
| **Parse the `ai_overview` free text for a name** | Unstructured, language-dependent, fragile NER. `related_content` queries are short and usually *are* the name. |
| **Run a face → name model locally (e.g. a celebrity classifier)** | A whole extra model, a fixed label set, and it would still need verification against real profiles. The knowledge graph already does this at Google's scale. |

The guess is low-stakes: a wrong entity name just wastes one Google text search, and
every Path-A candidate is still cosine-verified in Stage C.

### 8.4 Hosting the probe for the query — catbox.moe

**Chosen:** upload the probe to `catbox.moe` for the search query URL; keep the Pinata
IPFS pin as the permanent record; fall back to the IPFS gateway URL if catbox is down.

| Alternative | Why not |
|---|---|
| **Hand SerpApi the Pinata gateway URL directly** (the original plan) | Google Lens accepts it, but **SerpApi's Yandex engine rejects it**: `"The URL does not refer to an image, or the image is not publicly accessible."` The public Pinata gateway rate-limits / challenges Yandex's fetcher. Verified: same probe, Lens 59 matches, Yandex 0. |
| **Other IPFS gateways** — `ipfs.io`, `dweb.link`, `w3s.link`, `cloudflare-ipfs.com` | Public IPFS gateways are broadly degraded in 2026: `ipfs.io` → 403, `dweb.link`/`w3s.link` → 500, `cloudflare-ipfs.com` discontinued. None served the image reliably. |
| **A dedicated Pinata gateway** (`<name>.mypinata.cloud`) | Genuinely works and would be cleaner — but requires the user to have set one up. Supported via `PINATA_GATEWAY`, just not the default. |
| **`0x0.st`** | Upload API disabled ("AI botnet spam"). |
| **S3 / Cloudflare R2 / a self-hosted static server** | Another account, another key, another thing to configure and pay for. catbox needs none. |
| **imgur** | Needs an API key and has aggressive rate limits. |

**Trade-off accepted:** catbox is a volunteer-run free host with occasional downtime.
The fallback keeps Lens working even when catbox is unavailable (Yandex recall drops in
that window). The query URL actually used is recorded in the bundle
(`search.query_image_host`).

### 8.5 Candidate images — SerpApi thumbnail first, page scrape second

**Chosen:** the primary candidate image for every visual match is the **SerpApi
thumbnail** (the engine's cached copy). The source page is harvested only best-effort,
for its HTML hash, `og:image` (a second candidate), author handle, and caption.

| Alternative | Why not |
|---|---|
| **Scrape the actual image off the source page** | Logged-out Instagram / X / Facebook / TikTok serve a login wall or near-empty HTML to bots. The image you want is usually unreachable. The thumbnail is the engine's already-fetched copy of that exact image. |
| **Use only the page `og:image`** | Works for news sites, fails on social platforms (same login-wall problem) and on pages with no OpenGraph tags. |
| **Headless browser to render social pages** | Heavy, slow, needs anti-bot evasion, ToS-hostile. |

The `image_origin` field on every candidate (`serpapi_thumbnail` / `og:image`) records
exactly how each image was obtained, so the provenance of the provenance is auditable.

### 8.6 Commitment scheme — keccak256 Merkle tree, sorted pairs

**Chosen:** per-section leaves with a domain separator, keccak256, sorted sibling pairs
(OpenZeppelin convention), root committed on-chain as `bytes32`.

| Alternative | Why not |
|---|---|
| **Single `sha256(whole bundle)` on-chain** | Tamper-evident, but no selective-disclosure proofs. The Merkle tree costs ~30 extra lines and enables proving one leaf without revealing the rest. |
| **Store the whole bundle on-chain (calldata / storage)** | A bundle is several KB; on-chain storage is ~20k gas per 32 bytes. Absurdly expensive and pointless when IPFS + a hash does the job. |
| **SHA-256 tree instead of keccak** | keccak256 is the EVM-native hash; a keccak root can be verified *inside* a contract with `MerkleProof.verify` for free. SHA-256 in the EVM is a precompile but the tooling is all keccak. |
| **Unsorted pairs + explicit index bits in the proof** | Works, but sorted pairs make proofs index-free and match OpenZeppelin exactly, so on-chain verification is drop-in. |

### 8.7 Off-chain storage — IPFS via Pinata

**Chosen:** pin the probe image and the evidence bundle to IPFS through Pinata; read
back via the gateway.

| Alternative | Why not |
|---|---|
| **Arweave** | True permanence, but needs AR tokens and a wallet; overkill for a testnet demo. A documented upgrade path. |
| **Store bundle on S3 / a server** | Not content-addressed — the URL could serve different bytes later. IPFS CIDs *are* the hash, so the pointer itself is tamper-evident. |
| **Raw IPFS node (no Pinata)** | Running and keeping a node online is operational overhead; unpinned content gets garbage-collected. Pinata's free tier (500 files) is plenty. |
| **`web3.storage` / `nft.storage`** | Comparable; Pinata's API is the simplest (`Authorization: Bearer <jwt>`, two endpoints). |

### 8.8 Blockchain — Ethereum Sepolia testnet

**Chosen:** Ethereum Sepolia (chain 11155111), env-configurable to any EVM chain.

| Alternative | Why not / notes |
|---|---|
| **Base Sepolia** (the original plan) | Fine, and still a one-line switch (`RPC_URL`/`CHAIN_ID`/`EXPLORER_URL`). Switched to Ethereum Sepolia only because the Google Cloud faucet dispensed L1 Sepolia ETH, not Base — and L1 Sepolia is the more widely recognized testnet with the canonical Etherscan. |
| **A local chain (Anvil / Hardhat / Ganache)** | Zero cost and instant, but "spin up my local node" is less convincing in a demo than a real public explorer link. Fully supported via env vars for offline dev. |
| **Polygon Amoy / Arbitrum Sepolia / Optimism Sepolia** | All work unchanged. No advantage over Sepolia for this. |
| **Any mainnet** | Real gas cost for a demo, no upside — the task explicitly allows a testnet. |

### 8.9 Contract compilation — committed artifact, py-solc-x fallback

**Chosen:** `contracts/AttestationRegistry.json` (ABI + bytecode) is compiled once and
committed. `chain._artifact()` loads it. `py-solc-x` is used only if the JSON is absent.

| Alternative | Why not |
|---|---|
| **Compile at runtime with `py-solc-x`** (original) | Downloads a `solc` binary from `solc-bin.ethereum.org` on first run — a network dependency that failed in the build sandbox (DNS), needs internet during the demo, and adds a multi-second delay. The committed artifact removes all of that. |
| **Foundry / Hardhat** | A whole Node/Rust toolchain for a 60-line contract. `py-solc-x` (fallback) keeps it one-language. |
| **Vendor `solc.exe` in the repo** | ~9 MB binary in git; the JSON artifact is 8 KB. |

### 8.10 Merkle hashing dependency — vendored keccak

**Chosen:** `_keccak.py`, a pure-Python Keccak-256, verified against known vectors.

| Alternative | Why not |
|---|---|
| **`web3.Web3.keccak`** | Pulls the entire `web3` stack just to hash bytes. `evidence.py` and its tests should run with only the standard library — and they do. |
| **`pysha3` / `eth-hash[pycryptodome]`** | Extra dependency, and `pysha3` is unmaintained / doesn't build on new Pythons. |
| **`hashlib.sha3_256`** | **Wrong hash.** NIST SHA3 and Ethereum's Keccak differ in the domain-separation byte (`0x06` vs `0x01`). Silent incompatibility with the EVM. |

### 8.11 CLI framework — Typer

**Chosen:** `typer` (`>=0.15`) with `rich` output.

| Alternative | Why not |
|---|---|
| **argparse** | More boilerplate for subcommands; no free help formatting. |
| **click** directly | Typer *is* click with type-hint ergonomics. |
| Pinning `typer==0.12.5` | It breaks on `click>=8.2` (`Parameter.make_metavar()` signature change). `typer>=0.15` fixed it. This bit us once — hence the loosened, minimum-version pin. |

### 8.12 Web stack — FastAPI + vanilla JS

**Chosen:** FastAPI + uvicorn backend; a single hand-written HTML/JS file; a background
thread per job; the page polls for progress.

| Alternative | Why not |
|---|---|
| **Streamlit / Gradio** | Fast to write, but you don't control the layout, the progress stream is clunky, and it drags in a large dependency tree. A screen recording benefits from a purpose-built UI. |
| **Flask** | Fine, but FastAPI gives async, typed request parsing, and a free `/api/docs` for debugging. |
| **Server-Sent Events / WebSocket for progress** | Cleaner than polling in theory, but polling a `dict` every second is trivially robust, survives reconnects, and needs no extra client code. For a single-user local tool it's the right amount of engineering. |
| **Celery / RQ / a real job queue** | For one user on localhost, a `threading.Thread` and an in-memory dict is correct. A queue would be theatre. |
| **A React/Vite front end** | A build step, `node_modules`, and a bundle — for one page. Vanilla JS with no external assets loads instantly and is trivial to audit. |

### 8.13 Match selection — prefer a real social platform

**Chosen:** among accepted candidates, `SearchOutcome.best` prefers one whose domain is
a recognized social platform (`platform is not None`), then falls back to highest
cosine. The verification loop also **exits early** the moment an accepted candidate is
on a platform.

**Why:** the task wants a *social media post*. Without this bias, a random blog that
happens to reuse the photo can outscore the actual Instagram post on raw cosine and get
reported as "the match."

---

## 9. Threat model

### What an attestation *does* prove

- **Existence & time:** at block timestamp `T`, address `A` committed to a specific
  Merkle root and IPFS CID. The block time is consensus-backed; nobody can back-date it.
- **Integrity of the finding:** the evidence bundle at that CID hashes to that exact
  root. Change one byte of the bundle and re-verification fails (Merkle check).
- **Integrity of the sources, at re-verification time:** every candidate image and page
  HTML was hashed when the pipeline ran. If a source page later swaps its image,
  `reverify` flags that specific leaf — you can see *which* source drifted and when
  (relative to `T`).
- **Attribution:** `attester` is the signing key. If that key is known to belong to an
  organization, the attestation is attributable to them.

### What it does *not* prove

- **That the match is correct.** The chain faithfully records "SFace scored 0.79
  between the probe and this image." It does not certify that 0.79 means "same person" —
  that's a model judgement with a false-positive rate. A verifier must treat the cosine
  as evidence, not proof.
- **That the probe image is authentic.** A deepfake of a real person can pass SFace.
  FaceProv records provenance; it is not a liveness or AIGC detector.
- **That the source existed before `T`.** The pipeline sees the web *at* `T`. It
  commits what it saw. It cannot prove a post is older than the attestation (though a
  post's own platform timestamp, captured in the page HTML hash, is circumstantial).
- **Anything about who ran it.** Anyone can call `attest`. The registry is a public
  notary, not an authority. Trust in an attestation is exactly trust in its `attester`.

### Attacks and mitigations

| Attack | Mitigation |
|---|---|
| Edit the pinned bundle after attestation | Merkle root no longer matches on-chain → `reverify` FAIL (this is the `tamper` demo). |
| Swap a section between leaves (move `match` payload into `candidate[0]`) | Leaf hash includes a per-section domain separator (`faceprov-leaf:<name>:`). |
| Re-pin a *different* bundle at a URL you control and point people at it | The CID *is* the hash of the content; a different bundle has a different CID; the on-chain `cid` is fixed. |
| Attest a fabricated finding | Possible — anyone can. The defence is social: attestations are only as trustworthy as the `attester`. The system makes fabrication *detectable after the fact* (re-verification against live sources), not impossible. |
| Front-run / censor the `attest` tx | It's a plain testnet tx; no MEV surface (no value, no ordering dependence). |

---

## 10. Known limitations & failure modes

- **Recall on private individuals is low by nature.** They are barely indexed. The
  `eval/recall_study.py` harness exists to *quantify* this (public vs private, per
  engine, per path) rather than hide it.
- **Search APIs are non-deterministic.** Lens/Yandex results shift day to day, so a
  recorded run may not reproduce identically. The bundle captures what was seen at
  attestation time — that snapshot is the point.
- **Stock / sample images mislead the pipeline.** If the probe is a widely-reused stock
  or tutorial image (e.g. OpenCV's bundled `messi5.jpg`), reverse search returns pages
  that reuse *that image* (CV tutorials), not the person's social media. Use genuine
  press/profile photos.
- **Logged-out social pages yield almost nothing to the harvester.** Mitigated by the
  SerpApi-thumbnail-first strategy, but `platform`/`author_handle`/`caption` are often
  `null` because the page HTML is a login wall. A future `instagram_profile` /
  `facebook_profile` SerpApi integration would fix Path A properly.
- **catbox.moe is a free volunteer host.** Occasional downtime; the fallback keeps Lens
  working but drops Yandex recall for that window.
- **SFace < ArcFace R100.** Adequate for public figures at 0.36; the InsightFace path
  is an opt-in for anyone with MSVC build tools.
- **`reverify`'s face re-match is a stub.** It re-downloads the winning image and
  re-detects, but does not currently recompute and re-threshold the cosine end-to-end
  (the hook and the `recorded_cosine` are in place).
- **Sepolia is a testnet** — records are real and explorer-visible but not
  economically secured like mainnet.
- **No hosted website** — `faceprov serve` is localhost, single user, in-memory jobs.

---

## 11. Cost & quota analysis

| Resource | Per pipeline run | Free-tier budget | Runs before exhaustion |
|---|---|---|---|
| **SerpApi searches** | 3 (Lens + Yandex + Path-A Google text) | 250 / month | ~80 |
| **Pinata pins** | 2 (probe + bundle) | 500 files | ~250 |
| **Sepolia gas** | 1 `attest` tx ≈ 150–200k gas | faucet (0.05 ETH) | thousands |
| **catbox upload** | 1 | unlimited | — |
| Model download | once ever (~37 MB) | — | — |

A full demo (3 runs + a few re-verifies) costs ~10 SerpApi searches and a few cents of
testnet gas.

---

## 12. Testing

`tests/test_evidence.py` — 7 tests, **no network, no API keys, standard library only**
(thanks to the vendored keccak):

| Test | Asserts |
|---|---|
| `test_root_is_deterministic` | same bundle → same root, twice |
| `test_root_changes_when_a_leaf_changes` | flip one cosine → different root |
| `test_leaf_count_matches_sections` | probe + search + N candidates + match + run |
| `test_merkle_proof_verifies` | a `merkle_proof` sibling path recomputes the root |
| `test_roundtrip_json` | `to_json` → `from_json` → same root |
| `test_tamper_breaks_root` | `set_leaf_path` edit → root diverges (the tamper primitive) |
| `test_set_leaf_path_keeps_type` | int/bool/float fields keep their JSON type after edit |

`python -m pytest -q` → `7 passed`. All modules also `py_compile`-clean.

The search / chain / IPFS paths are validated by **live end-to-end runs** (see §13 and
the commit messages) rather than mocked unit tests — the value there is in the real API
response shapes, which mocks would only paper over.

---

## 13. Annotated real run

From a live `faceprov serve` upload (progress log, lightly trimmed):

```
 0.0s  detect      detecting and encoding the face (YuNet + SFace)
 0.5s  detect      face found - det_score 0.92, 128-d embedding
 0.5s  pin_probe   pinning the probe image to IPFS (Pinata)
 3.0s  pin_probe   probe CID QmWxawWBsd6sxnJa8apJA9Bf5bLwHtL9D3zMtV5FNZsHKC
 5.1s  host_probe  probe hosted for search via catbox.moe
 5.1s  search      reverse-image search: Google Lens + Yandex Images
 5.4s  search      Google Lens: 59 visual matches, entity 'Lionel Messi'      ← knowledge_graph / related_content hit → Path A
 5.6s  search      Yandex Images: 89 visual matches
 5.8s  search      Path A - 8 candidate social profiles for 'Lionel Messi'    ← Google text search for verified profiles
 6.6s  verify      [accept] cos +0.547  google_lens  https://www.ebay.com/...
 9.0s  verify      [accept] cos +0.629  yandex_images  https://github.com/... ← engines interleaved
12.2s  verify      [accept] cos +0.792  yandex_images  https://luckytaylor.top/...
...
27.4s  search      path A:entity - 12 candidates, 11 above threshold
27.5s  bundle      built evidence bundle - Merkle root 0xa74a1fbf71408647...
29.6s  pin_bundle  evidence bundle pinned to IPFS - CID QmZcgajj4AMkfpRQSz...
29.8s  done        complete in 29.8s
```

Then **Re-verify** → `verdict: PASS` (on-chain root == recomputed root; all 12 source
hashes still match). Then **Tamper test** → edits `match.source_url`, recomputed root
`0x3b1c9e04...` ≠ on-chain `0xa74a1fbf...` → **TAMPER DETECTED**.

*(This particular probe was OpenCV's `messi5.jpg` sample, so the accepted candidates
are CV-tutorial pages that reuse that exact image — see §10. The mechanics are correct;
the input was a stock image.)*

---

## 14. Extending the project

Ordered by value-to-effort:

1. **SerpApi `instagram_profile` / `facebook_profile`** in Path A — replace the fragile
   HTML harvest of profile pages with the structured post feed (`display_url`,
   `media_captions`). This is the single biggest recall win for public figures.
2. **Finish `reverify`'s face re-match** — recompute the cosine between the probe digest
   and the re-fetched winning image and re-threshold it, so the loop is fully closed.
3. **On-chain Merkle proof verification** — a `verifyLeaf(uint256 id, bytes32 leaf,
   bytes32[] proof)` view on the contract; the tree is already OpenZeppelin-compatible.
4. **Recall study numbers** — run `eval/recall_study.py` over 30 identities and fill the
   README table.
5. **Bing reverse image** as a third engine (`bing_reverse_image`).
6. **Arweave** as an alternative permanent store for the bundle.
7. **A dedicated Pinata gateway** as the default probe-query host (removes the catbox
   dependency).
8. **Deepfake / AIGC score** on the probe, recorded as a bundle field (honest signal,
   not a gate).

---

## 15. File map

```
HHGoa-FaceRecognition/
├── README.md                     one-page overview + how to run + blockchain used + limitations
├── LICENSE                       MIT
├── pyproject.toml                package metadata, `faceprov` entry point, pytest config
├── requirements.txt              pinned/ranged dependencies
├── .env.example                  every config var with comments
├── deployments.json              live contract address + ABI + deploy tx (committed)
│
├── contracts/
│   ├── AttestationRegistry.sol   the notary contract (Solidity ^0.8.20)
│   └── AttestationRegistry.json  committed ABI + bytecode (so deploy needs no solc)
│
├── src/faceprov/
│   ├── __init__.py               __version__
│   ├── config.py                 Config dataclass, env loading, chain abstraction
│   ├── face.py                   Stage A — YuNet detect + SFace encode
│   ├── _keccak.py                vendored Keccak-256 (no deps)
│   ├── evidence.py               Stage D — canonical bundle + Merkle tree + proofs
│   ├── verify.py                 Stage C — per-candidate cosine re-verification
│   ├── chain.py                  Stage E/F — deploy / attest / read (web3)
│   ├── pipeline.py               orchestration: run_pipeline / reverify / tamper_demo
│   ├── cli.py                    typer CLI: run | serve | verify | tamper | deploy
│   └── search/
│       ├── ipfs.py               Pinata pin + gateway fetch
│       ├── imagehost.py          catbox upload of the probe for the query
│       ├── lens.py               SerpApi Google Lens + Path-A profile search
│       ├── yandex.py             SerpApi Yandex Images (face-region crop)
│       ├── harvest.py            best-effort source-page fetch + metadata
│       └── router.py             entity router (Path A/B) + candidate fusion
│
├── web/
│   ├── server.py                 FastAPI: upload → background job → polled progress
│   └── index.html                single-page vanilla-JS UI
│
├── eval/
│   ├── identities.csv            15 public + 15 private identity slots
│   └── recall_study.py           runs the pipeline over all of them, tabulates recall
│
├── tests/
│   └── test_evidence.py          7 pure tests (Merkle / bundle / tamper primitive)
│
├── scripts/
│   └── deploy.py                 thin wrapper == `faceprov deploy`
│
└── docs/
    ├── ARCHITECTURE.md           (this file)
    └── DEMO.md                   screen-recording checklist
```

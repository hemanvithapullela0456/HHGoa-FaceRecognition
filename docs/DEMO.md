# Screen-recording checklist

The task requires a plain screen recording of the pipeline working end to end:
**face scan → social post found → blockchain upload/verification.** No editing needed.

## One-time before recording
- [ ] `.env` filled: `SERPAPI_KEY`, `PINATA_JWT`, `DEPLOYER_PRIVATE_KEY` (Ethereum Sepolia, funded via faucet)
- [ ] `faceprov deploy` → copy `ATTESTATION_REGISTRY_ADDRESS` into `.env` (already done — see `deployments.json`)
- [ ] Open `https://sepolia.etherscan.io/address/<contract>` in a browser tab
- [ ] Have 3 probe images ready: **two real photos of well-known public figures**
      (news photos or verified-profile screenshots — NOT stock/sample images) +
      one image of yourself (the expected `NO_MATCH_FOUND`)

---

## Option A — web UI (recommended, easiest to record)

```
faceprov serve          # -> http://127.0.0.1:8000
```

1. **Show the contract on Etherscan** — deployed, current tx count.
2. **Upload public figure A**, keep "write attestation on-chain" checked, hit **Run**.
   Narrate the live log: detect → pin probe to IPFS → host for search → Lens + Yandex
   → per-candidate cosine accept/reject → Merkle root → bundle pinned → on-chain tx.
3. On the result page: point at the matched post link, the candidate table, the
   **Merkle root**, the **IPFS bundle** link, the **attestation #id + tx** link.
4. Click **Re-verify against chain** → verdict **PASS**, every source re-hashed.
5. Click **Tamper test** → recomputed root no longer matches the chain → **TAMPER DETECTED**.
6. **Refresh Etherscan** → the new `Attested` transaction, expand the decoded event.
7. **Open the IPFS bundle link** → the full evidence JSON, point at `merkle.root`.
8. **Upload public figure B** — different inputs, different candidates, new attestation id.
9. **Upload your own face** — clean `NO MATCH FOUND` with sub-threshold scores shown.

---

## Option B — CLI

```
faceprov run    --image demo/figure_a.jpg      # scan -> search -> bundle -> IPFS -> chain
faceprov verify --id <id>                       # re-verify against chain
faceprov tamper --id <id>                       # tamper-evidence demo
```
Run it on 3 different images (2 public figures + your own face for the NO_MATCH case)
so it's visibly not hardcoded.

---

## Upload
YouTube (unlisted) / Google Drive / Loom — paste the link in the submission form
alongside the GitHub repo URL.

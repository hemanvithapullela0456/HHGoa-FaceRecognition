# Screen-recording checklist

The task requires a plain screen recording of the pipeline working end to end:
**face scan → social post found → blockchain upload/verification.** No editing needed.

## One-time before recording
- [ ] `.env` filled: `SERPAPI_KEY`, `PINATA_JWT`, `DEPLOYER_PRIVATE_KEY` (Base Sepolia, funded via faucet)
- [ ] `python -m faceprov.cli deploy` → copy `ATTESTATION_REGISTRY_ADDRESS` into `.env`
- [ ] Open `https://sepolia.basescan.org/address/<contract>` in a browser tab
- [ ] Have 3 probe images ready: two well-known public figures + one image of yourself
      (the expected `NO_MATCH_FOUND`)

## Record this sequence (~4–6 min, unedited)

1. **Show the contract on Basescan** — deployed, 0 transactions so far.
2. **Run 1 — public figure A**
   ```
   python -m faceprov.cli run --image demo/figure_a.jpg
   ```
   Narrate as it prints: face detected → path `A:entity` → entity name resolved →
   social profile found → candidate table with cosine scores → matched post →
   Merkle root + IPFS CID → on-chain attestation id + tx link.
3. **Refresh Basescan** — show the new `Attested` transaction and decoded event args.
4. **Open the IPFS bundle URL** — show the full evidence JSON, point at the `merkle.root`.
5. **Re-verify**
   ```
   python -m faceprov.cli verify --id 0
   ```
   Show: on-chain root == recomputed root, each source re-hashed, verdict **PASS**.
6. **Run 2 — public figure B** — different input, different path/candidates, new id.
   Shows the pipeline is not hardcoded.
7. **Run 3 — your own face** — show a clean `NO_MATCH_FOUND` with the rejected
   candidates and their sub-threshold scores. Honest failure, no crash.
8. **Tamper demo**
   ```
   python -m faceprov.cli tamper --id 0
   ```
   Shows: original bundle root matches the chain; after editing one field the
   recomputed root no longer matches → tamper detected.

## Upload
YouTube (unlisted) / Google Drive / Loom — paste the link in the submission form
alongside the GitHub repo URL.

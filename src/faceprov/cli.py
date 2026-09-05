"""faceprov CLI — `run`, `verify`, `deploy`."""
from __future__ import annotations

import json
import sys

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .config import DEPLOYMENTS_FILE, Config

# Windows consoles default to cp1252, which cannot encode the box/emoji glyphs rich
# emits — force UTF-8 so output never crashes mid-run.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

app = typer.Typer(add_completion=False, help="Face-anchored provenance for image verification.")
console = Console()

_YES = "[green]yes[/green]"
_NO = "[red]no[/red]"

# Re-verification grades drift instead of calling everything a failure; see
# faceprov.pipeline._check for what each level means.
_LEVEL_MARK = {
    "ok": "[green]ok[/green]",
    "info": "[dim]info[/dim]",
    "warn": "[yellow]warn[/yellow]",
    "fail": "[red]FAIL[/red]",
    "skip": "[dim]skip[/dim]",
}
_VERDICT_COLOR = {"PASS": "green", "PASS_WITH_WARNINGS": "yellow", "FAIL": "red"}


@app.command()
def deploy():
    """Compile AttestationRegistry.sol and deploy it to the configured EVM testnet."""
    from .chain import Chain

    cfg = Config.load(require_chain=True, require_search=False)
    console.print(f"[bold]Compiling + deploying (chain id {cfg.chain_id})...[/bold]")
    chain = Chain.from_config(cfg, signer=True)
    info = chain.deploy()
    console.print(Panel.fit(
        f"address:  [green]{info['address']}[/green]\n"
        f"tx:       {info['deploy_tx']}\n"
        f"explorer: {info['explorer']}\n\n"
        f"Add to .env:  ATTESTATION_REGISTRY_ADDRESS={info['address']}",
        title="deployed",
    ))


@app.command()
def run(
    image: str = typer.Option(..., "--image", "-i", help="Path to the face scan image."),
    no_attest: bool = typer.Option(False, "--no-attest", help="Skip the on-chain write."),
    out: str = typer.Option("", "--out", help="Write the full result JSON here."),
):
    """Run the end-to-end pipeline on one image."""
    from .pipeline import run_pipeline

    cfg = Config.load(require_chain=not no_attest, require_search=True)
    res = run_pipeline(image, cfg, attest=not no_attest)

    if not res.get("probe_publicly_fetchable", True):
        console.print(Panel.fit(
            "No image host would serve the probe, so Google Lens and Yandex both\n"
            "fetched a dead URL. Zero candidates here says nothing about the person\n"
            "in the image - re-run when a host is reachable.",
            title="[red]search did not actually run[/red]",
        ))

    first_seen = res.get("earliest_known_appearance")
    console.print(Panel.fit(
        f"path taken:   [bold cyan]{res['path_taken']}[/bold cyan]\n"
        f"entity:       {res.get('entity_name') or '—'}\n"
        f"matched:      {'[green]YES[/green]' if res['matched'] else '[red]NO_MATCH_FOUND[/red]'}\n"
        f"first seen:   {(first_seen[:10] + '  [dim](web.archive.org)[/dim]') if first_seen else '[dim]no archived capture[/dim]'}",
        title="search",
    ))

    tbl = Table("engine", "origin", "cosine", "accepted", "platform", "source")
    for c in res["candidates"]:
        tbl.add_row(
            c["engine"],
            c.get("image_origin", ""),
            f"{c['best_cosine']:.4f}",
            _YES if c["accepted"] else _NO,
            c.get("platform") or "-",
            (c["source_url"] or "")[:60],
        )
    console.print(tbl)

    if res["matched"]:
        m = res["match"]
        console.print(Panel.fit(
            f"post:    [green]{m['source_url']}[/green]\n"
            f"image:   {m['image_url']}\n"
            f"cosine:  {m['cosine']:.4f}",
            title="matched post",
        ))

    console.print(Panel.fit(
        f"merkle root: [yellow]{res['merkle_root']}[/yellow]\n"
        f"bundle CID:  {res['bundle_cid']}\n"
        f"bundle URL:  {res['bundle_url']}",
        title="evidence bundle",
    ))

    if "attestation" in res:
        a = res["attestation"]
        console.print(Panel.fit(
            f"id:       [bold green]{a['id']}[/bold green]\n"
            f"tx:       {a['tx']}\n"
            f"explorer: {a['explorer']}\n\n"
            f"re-verify:  python -m faceprov.cli verify --id {a['id']}",
            title="on-chain attestation",
        ))

    if out:
        with open(out, "w") as f:
            json.dump(res, f, indent=2)
        console.print(f"[dim]wrote {out}[/dim]")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
):
    """Launch the web UI (upload a face, watch the pipeline run, verify)."""
    import sys as _sys
    from pathlib import Path as _Path

    repo_root = _Path(__file__).resolve().parents[2]
    if str(repo_root) not in _sys.path:
        _sys.path.insert(0, str(repo_root))
    try:
        from web.server import serve as _serve
    except ModuleNotFoundError as e:
        raise typer.Exit(
            f"web deps missing ({e}); pip install fastapi uvicorn python-multipart"
        )
    console.print(f"[bold]FaceProv UI:[/bold] http://{host}:{port}")
    _serve(host, port)


@app.command()
def verify(id: int = typer.Option(..., "--id", help="Attestation id to re-verify.")):
    """Re-verify an on-chain attestation against IPFS + live sources."""
    from .pipeline import reverify

    cfg = Config.load(require_chain=False, require_search=True)
    if not cfg.registry_address and DEPLOYMENTS_FILE.exists():
        addr = json.loads(DEPLOYMENTS_FILE.read_text())["address"]
        object.__setattr__(cfg, "registry_address", addr)

    res = reverify(id, cfg)
    color = _VERDICT_COLOR.get(res["verdict"], "red")
    console.print(Panel.fit(
        f"attestation:  {id}\n"
        f"attester:     {res['onchain']['attester']}\n"
        f"cid:          {res['onchain']['cid']}\n"
        f"verdict:      [{color}]{res['verdict']}[/{color}]",
        title="re-verification",
    ))
    tbl = Table("check", "result", "detail")
    for c in res["checks"]:
        mark = _LEVEL_MARK.get(c.get("level", ""), _YES if c.get("ok", True) else _NO)
        detail = {k: v for k, v in c.items() if k not in {"check", "level", "ok"}}
        tbl.add_row(str(c.get("check")), mark, json.dumps(detail)[:96])
    console.print(tbl)
    console.print(
        "[dim]ok = recomputed exactly · info = expected to change (CDN/ad churn) · "
        "warn = worth a look · FAIL = the sealed claim no longer holds[/dim]"
    )


@app.command()
def tamper(
    id: int = typer.Option(..., "--id", help="Attestation id whose bundle to tamper with."),
    field: str = typer.Option("", "--field", help="Dotted path, e.g. candidates.0.best_cosine"),
    value: str = typer.Option("", "--value", help="New value for --field"),
):
    """Demo tamper-evidence: edit one field of the attested bundle, show the root break."""
    from .pipeline import tamper_demo

    cfg = Config.load(require_chain=False, require_search=True)
    if not cfg.registry_address and DEPLOYMENTS_FILE.exists():
        object.__setattr__(cfg, "registry_address", json.loads(DEPLOYMENTS_FILE.read_text())["address"])

    res = tamper_demo(id, cfg, field_path=field or None, value=(value or None))
    orig = "[green]matches chain[/green]" if res["original_matches_chain"] else "[red]MISMATCH[/red]"
    tam = ("[red]DOES NOT MATCH - tamper detected[/red]"
           if not res["tampered_matches_chain"] else "[green]matches (unexpected)[/green]")
    console.print(Panel.fit(
        f"on-chain root:          [yellow]{res['onchain_root']}[/yellow]\n"
        f"original recomputed:    {res['original_recomputed_root']}  {orig}\n\n"
        f"tampered field:         [red]{res['tampered_field']} = {res['tampered_value']}[/red]\n"
        f"tampered root:          {res['tampered_root']}  {tam}",
        title="tamper-evidence demo",
    ))


if __name__ == "__main__":
    app()

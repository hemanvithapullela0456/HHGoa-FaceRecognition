"""FastAPI server exposing the pipeline: upload a face -> run -> stream progress -> verify.

    faceprov serve            # then open http://127.0.0.1:8000
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse

from faceprov.config import DEPLOYMENTS_FILE, Config
from faceprov.pipeline import reverify, run_pipeline, tamper_demo

WEB_DIR = Path(__file__).parent
JOBS: dict[str, dict] = {}

app = FastAPI(title="FaceProv", docs_url="/api/docs")


# ---------------------------------------------------------------- helpers
def _registry_address() -> str:
    addr = Config.load(require_chain=False, require_search=False).registry_address
    if not addr and DEPLOYMENTS_FILE.exists():
        addr = json.loads(DEPLOYMENTS_FILE.read_text()).get("address", "")
    return addr


def _cfg_with_registry(*, require_chain: bool = False) -> Config:
    cfg = Config.load(require_chain=require_chain, require_search=True)
    if not cfg.registry_address:
        object.__setattr__(cfg, "registry_address", _registry_address())
    return cfg


# ---------------------------------------------------------------- pages
@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/config")
def api_config() -> dict:
    cfg = Config.load(require_chain=False, require_search=False)
    return {
        "chain_id": cfg.chain_id,
        "explorer_url": cfg.explorer_url,
        "registry": _registry_address(),
        "threshold": cfg.match_threshold,
    }


# ---------------------------------------------------------------- run job
def _run_job(job_id: str, img_path: Path, attest: bool) -> None:
    job = JOBS[job_id]

    def progress(stage: str, detail: str) -> None:
        job["events"].append(
            {"t": round(time.time() - job["t0"], 1), "stage": stage, "detail": detail}
        )

    try:
        job["status"] = "running"
        cfg = Config.load(require_chain=attest, require_search=True)
        job["result"] = run_pipeline(str(img_path), cfg, attest=attest, progress=progress)
        job["status"] = "done"
    except Exception as e:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = f"{type(e).__name__}: {e}"
        job["trace"] = traceback.format_exc()
        progress("error", job["error"])
    finally:
        img_path.unlink(missing_ok=True)


@app.post("/api/run")
async def api_run(image: UploadFile = File(...), attest: str = Form("true")) -> dict:
    data = await image.read()
    if not data:
        raise HTTPException(400, "empty upload")
    tmp = Path(tempfile.gettempdir()) / f"faceprov-{uuid.uuid4().hex}.jpg"
    tmp.write_bytes(data)

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {
        "status": "queued", "events": [], "t0": time.time(),
        "result": None, "error": None, "trace": None,
    }
    threading.Thread(
        target=_run_job,
        args=(job_id, tmp, attest.lower() in ("1", "true", "yes", "on")),
        daemon=True,
    ).start()
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    return {k: job[k] for k in ("status", "events", "result", "error", "trace")}


# ---------------------------------------------------------------- verify / tamper
@app.post("/api/verify/{attestation_id}")
def api_verify(attestation_id: int) -> dict:
    try:
        return reverify(attestation_id, _cfg_with_registry())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"{type(e).__name__}: {e}")


@app.post("/api/tamper/{attestation_id}")
def api_tamper(attestation_id: int) -> dict:
    try:
        return tamper_demo(attestation_id, _cfg_with_registry())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"{type(e).__name__}: {e}")


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port)

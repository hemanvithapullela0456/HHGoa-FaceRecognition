"""Thin wrapper: `python scripts/deploy.py` == `python -m faceprov.cli deploy`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faceprov.chain import Chain          # noqa: E402
from faceprov.config import Config        # noqa: E402

if __name__ == "__main__":
    cfg = Config.load(require_chain=True, require_search=False)
    info = Chain(cfg.rpc_url, cfg.deployer_key).deploy()
    print(f"deployed: {info['address']}")
    print(f"explorer: {info['explorer']}")
    print(f"\nadd to .env:\nATTESTATION_REGISTRY_ADDRESS={info['address']}")

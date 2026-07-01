import yaml
from pathlib import Path


def load_config() -> dict:
   """Load settings from config.yaml at the package root (saas_infra_agent/config.yaml)."""
   package_root = Path(__file__).resolve().parents[1]
   return yaml.safe_load((package_root / "config.yaml").read_text())


config = load_config()

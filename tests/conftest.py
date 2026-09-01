import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import load_config  # noqa: E402


@pytest.fixture(scope="session")
def cfg():
    return load_config(ROOT / "config.yaml")


@pytest.fixture(scope="session")
def payload():
    """A real Open-Meteo GFS response captured for Manaslu. Keeps tests offline."""
    return json.loads((ROOT / "tests/fixtures/openmeteo_gfs.json").read_text())


@pytest.fixture(scope="session")
def frame(payload, cfg):
    from src.interpolate import interpolate_bands
    from src.metrics import evaluate
    return evaluate(interpolate_bands(payload, cfg), cfg)

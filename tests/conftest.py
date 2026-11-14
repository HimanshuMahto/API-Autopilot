from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
import yaml

from autopilot.config import DEMO_CONFIG, Config
from autopilot.executor import Executor
from autopilot.models import Scenario, Step
from autopilot.oracles import check_all
from autopilot.policy import Policy
from autopilot.spec import load_spec
from demo import shop_api

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def config(tmp_path: Path) -> Config:
    cfg = Config.model_validate(yaml.safe_load(DEMO_CONFIG))
    cfg.spec = str(ROOT / "demo" / "openapi.yaml")
    cfg.root = tmp_path  # keep .autopilot/ state out of the repo
    return cfg


@pytest.fixture
def spec():
    return load_spec(ROOT / "demo" / "openapi.yaml")


@pytest.fixture(autouse=True)
def fresh_demo():
    shop_api.reset()
    yield
    shop_api.reset()


def demo_client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=shop_api.app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765")


def run(coro):
    return asyncio.run(coro)


def scenario(*steps: Step, title: str = "t") -> Scenario:
    return Scenario(id="t", title=title, steps=list(steps))


def execute(config, spec, sc: Scenario):
    async def go():
        async with demo_client() as client:
            exchanges, _ = await Executor(config, spec, Policy(config, spec), client).run(sc)
            return exchanges, check_all(spec, exchanges)

    return run(go())

"""Project configuration (autopilot.yaml)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

CONFIG_FILE = "autopilot.yaml"
_PROD_HOST = re.compile(r"(^|[.-])(prod|production|live)([.-]|$)", re.IGNORECASE)


class ConfigError(ValueError):
    pass


class Identity(BaseModel):
    """How to authenticate as one test user. The LLM only ever sees the name."""

    header: str = "Authorization"
    prefix: str = "Bearer "
    value_env: str | None = None  # read the secret from this environment variable
    value: str | None = None  # literal secret; only sensible for local demo APIs

    @model_validator(mode="after")
    def _one_source(self) -> Identity:
        if not self.value_env and not self.value:
            raise ValueError("an identity needs either value_env or value")
        return self

    def header_value(self, name: str) -> str:
        secret = self.value
        if self.value_env:
            secret = os.environ.get(self.value_env)
            if not secret:
                raise ConfigError(
                    f"Identity {name!r} reads its token from ${self.value_env}, which is not set"
                )
        return f"{self.prefix}{secret}"


class Target(BaseModel):
    base_url: str
    allowed_hosts: list[str] = Field(default_factory=list)
    allow_writes: bool = False  # POST/PUT/PATCH
    allow_deletes: bool = False  # DELETE

    @property
    def host(self) -> str:
        return urlparse(self.base_url).hostname or ""

    @property
    def looks_like_production(self) -> bool:
        return bool(_PROD_HOST.search(self.host))


class Budgets(BaseModel):
    max_requests: int = 200
    max_iterations: int = 3  # adaptive follow-up rounds after the initial plan
    max_llm_calls: int = 12
    request_timeout_s: float = 10.0


DEFAULT_MODELS = {"groq": "openai/gpt-oss-120b", "claude": "claude-opus-5-5"}
DEFAULT_KEY_ENV = {"groq": "GROQ_API_KEY", "claude": "ANTHROPIC_API_KEY"}


class LLMConfig(BaseModel):
    # "heuristic" runs the built-in rule-based planner and needs no API key.
    provider: Literal["groq", "claude", "heuristic"] = "groq"
    model: str | None = None  # defaults per provider, see DEFAULT_MODELS
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    api_key_env: str | None = None  # environment variable holding the key

    def model_for(self, provider: str) -> str:
        # A model named in the config only applies to the configured provider, so
        # `--planner` can switch provider without sending it a foreign model name.
        if self.model and provider == self.provider:
            return self.model
        return DEFAULT_MODELS[provider]

    def key_env_for(self, provider: str) -> str:
        if self.api_key_env and provider == self.provider:
            return self.api_key_env
        return DEFAULT_KEY_ENV[provider]


class Config(BaseModel):
    spec: str
    target: Target
    identities: dict[str, Identity] = Field(default_factory=dict)
    budgets: Budgets = Field(default_factory=Budgets)
    confirmation_attempts: int = Field(default=2, ge=1, le=10)
    llm: LLMConfig = Field(default_factory=LLMConfig)

    # Where this config was loaded from; relative paths resolve against it.
    root: Path = Field(default=Path("."), exclude=True)

    @model_validator(mode="after")
    def _check(self) -> Config:
        if "anonymous" in self.identities:
            raise ValueError("'anonymous' is reserved for unauthenticated requests")
        if not self.target.allowed_hosts:
            self.target.allowed_hosts = [self.target.host]
        return self

    @property
    def spec_source(self) -> str | Path:
        """The spec as given: an http(s) URL, or a file path resolved against the config's folder."""
        if self.spec.startswith(("http://", "https://")):
            return self.spec
        return (self.root / self.spec).resolve()

    @property
    def state_dir(self) -> Path:
        return self.root / ".autopilot"

    @property
    def scenarios_dir(self) -> Path:
        return self.state_dir / "scenarios"

    @property
    def identity_names(self) -> list[str]:
        return [*self.identities, "anonymous"]


def load_config(path: str | Path = CONFIG_FILE) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"{p} not found. Run `autopilot init` to create one.")
    try:
        data = yaml.safe_load(p.read_text()) or {}
        config = Config.model_validate(data)
    except (yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"Invalid {p}:\n{exc}") from None
    config.root = p.resolve().parent
    load_dotenv(config.root / ".env")
    return config


def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines into the environment. Variables already set win.

    Lets you keep API keys in a git-ignored .env file instead of the config.
    """
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


DEMO_CONFIG = """\
# API Autopilot configuration. See docs/PRD.md for what each part does.
spec: demo/openapi.yaml

target:
  base_url: http://127.0.0.1:8765
  allowed_hosts: [127.0.0.1, localhost]   # requests to any other host are refused
  allow_writes: true                      # POST/PUT/PATCH; keep false for shared envs
  allow_deletes: false                    # DELETE

# Test users. The agent only ever sees these names, never the tokens.
# For a real target use value_env so tokens stay out of this file.
identities:
  alice:
    value: alice-token       # demo only; use value_env: ALICE_TOKEN elsewhere
  bob:
    value: bob-token

budgets:
  max_requests: 200
  max_iterations: 3
  max_llm_calls: 12
  request_timeout_s: 10

confirmation_attempts: 2

llm:
  provider: groq             # or "heuristic" to run without an API key
  model: openai/gpt-oss-120b
  effort: medium
  api_key_env: GROQ_API_KEY  # set it in your shell or in a .env file next to this one
"""


def real_api_config(spec: str, base_url: str, header: str, prefix: str, auth_note: str) -> str:
    """autopilot.yaml for a real API. Starts read-only: turn writes on once you trust it."""
    from urllib.parse import urlparse

    host = urlparse(base_url).hostname or ""
    return f"""\
# API Autopilot configuration. Start read-only, review the first report, then
# enable writes if this is a staging/test environment you're allowed to change.
spec: {spec}

target:
  base_url: {base_url}
  allowed_hosts: [{host}]   # requests to any other host are refused
  allow_writes: false        # POST/PUT/PATCH; only enable on staging/test data
  allow_deletes: false       # DELETE

# Two test users, so Autopilot can check that one can't reach the other's data.
# Auth detected from the spec: {auth_note}.
# Tokens are read from environment variables (or a .env file next to this one).
identities:
  user_a:
    header: {header}
    prefix: "{prefix}"
    value_env: USER_A_TOKEN
  user_b:
    header: {header}
    prefix: "{prefix}"
    value_env: USER_B_TOKEN

budgets:
  max_requests: 100
  max_iterations: 2
  max_llm_calls: 10
  request_timeout_s: 10

confirmation_attempts: 2

llm:
  provider: groq             # or "heuristic": no AI, nothing leaves your machine
  model: openai/gpt-oss-120b
  effort: medium
  api_key_env: GROQ_API_KEY
"""

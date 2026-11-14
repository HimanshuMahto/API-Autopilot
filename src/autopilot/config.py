"""Project configuration (autopilot.yaml)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, PrivateAttr, ValidationError, model_validator

CONFIG_FILE = "autopilot.yaml"
_PROD_HOST = re.compile(r"(^|[.-])(prod|production|live)([.-]|$)", re.IGNORECASE)


class ConfigError(ValueError):
    pass


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env(value: Any, *, what: str) -> Any:
    """Replace ${VAR} in strings (recursively) with environment variables.

    A missing variable is an error naming the variable, never its value.
    """
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise ConfigError(f"{what} uses ${{{name}}}, which is not set (add it to .env)")
            return os.environ[name]

        return _ENV_REF.sub(repl, value)
    if isinstance(value, dict):
        return {k: expand_env(v, what=what) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v, what=what) for v in value]
    return value


class LoginConfig(BaseModel):
    """How to get a fresh token for an identity at the start of each run."""

    method: str = "POST"
    path: str  # relative to target.base_url, e.g. /auth/login (or a full URL)
    format: Literal["json", "form"] = "json"  # form = application/x-www-form-urlencoded (OAuth2)
    body: dict[str, Any] = Field(default_factory=dict)  # values may use ${ENV_VAR}
    token_path: str = "body.access_token"  # where the token is in the response


class Identity(BaseModel):
    """How to authenticate as one test user. The LLM only ever sees the name."""

    header: str = "Authorization"
    prefix: str = "Bearer "
    value_env: str | None = None  # read the secret from this environment variable
    value: str | None = None  # literal secret; only sensible for local demo APIs
    login: LoginConfig | None = None  # or log in at the start of each run

    _token: str | None = PrivateAttr(default=None)  # set by autopilot.auth.log_in

    @model_validator(mode="after")
    def _one_source(self) -> Identity:
        if not self.value_env and not self.value and not self.login:
            raise ValueError("an identity needs value_env, value or login")
        return self

    def set_token(self, token: str) -> None:
        self._token = token

    def header_value(self, name: str) -> str:
        if self.login:
            if not self._token:
                raise ConfigError(f"Identity {name!r} hasn't logged in yet")
            return f"{self.prefix}{self._token}"
        secret = self.value
        if self.value_env:
            secret = os.environ.get(self.value_env)
            if not secret:
                raise ConfigError(
                    f"Identity {name!r} reads its token from ${self.value_env}, which is not set"
                )
        return f"{self.prefix}{secret}"


class StartConfig(BaseModel):
    """How to start the app under test. Autopilot stops it again after the run."""

    command: str  # e.g. "uvicorn app.main:app --port 8000"
    cwd: str = "."  # relative to autopilot.yaml
    shell: bool = False  # run through the shell (for &&, pipes, $VARS)
    ready_url: str | None = None  # path or URL to poll; defaults to target.base_url
    timeout_s: float = 60.0
    env: dict[str, str] = Field(default_factory=dict)  # extra environment variables


class Target(BaseModel):
    base_url: str
    allowed_hosts: list[str] = Field(default_factory=list)
    allow_writes: bool = False  # POST/PUT/PATCH
    allow_deletes: bool = False  # DELETE
    start: StartConfig | None = None  # start the app before a run (optional)

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


DEFAULT_MODELS = {
    "claude": "claude-opus-5-5",
    "openai": "gpt-5.4-mini",
    "gemini": "gemini-flash-latest",  # Google's alias for its current Flash model
    "groq": "openai/gpt-oss-120b",
}
DEFAULT_KEY_ENV = {
    "claude": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
}
# The order `provider: auto` checks for a key in. Claude comes last, so it is only
# picked when its key is the only one present.
AUTO_ORDER = ("openai", "gemini", "groq", "claude")
PROVIDERS = ("auto", *DEFAULT_MODELS, "heuristic")


class LLMConfig(BaseModel):
    # "auto" uses whichever AI key is set (see AUTO_ORDER). "heuristic" runs the
    # built-in rule-based planner and needs no key.
    provider: Literal["auto", "claude", "openai", "gemini", "groq", "heuristic"] = "auto"
    model: str | None = None  # defaults per provider, see DEFAULT_MODELS
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    api_key_env: str | None = None  # environment variable holding the key
    # For provider "openai": any OpenAI-compatible API (OpenRouter, a local model server, ...).
    base_url: str | None = None

    def resolve(self, choice: str | None = None) -> str | None:
        """The provider to use: `choice` or the configured one, with "auto" resolved
        to the first provider whose key is set. None if auto finds no key."""
        provider = choice or self.provider
        if provider != "auto":
            return provider
        return next((p for p in AUTO_ORDER if os.environ.get(DEFAULT_KEY_ENV[p])), None)

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

    def base_url_for(self, provider: str) -> str | None:
        return self.base_url if provider == self.provider else None


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
  # Uses whichever AI key it finds: OPENAI_API_KEY, GEMINI_API_KEY, GROQ_API_KEY
  # or ANTHROPIC_API_KEY. To choose, set provider to openai, gemini, groq or
  # claude, or "heuristic" for no AI (nothing leaves your machine). For any other
  # OpenAI-compatible service, use provider openai with base_url and api_key_env.
  provider: auto
  effort: medium
"""


def real_api_config(
    spec: str,
    base_url: str,
    header: str,
    prefix: str,
    auth_note: str,
    start_command: str | None = None,
    login: Any = None,  # spec.LoginHint
) -> tuple[str, list[str]]:
    """autopilot.yaml for a real API, plus the environment variables it needs.

    It starts read-only: turn writes on once you trust what it does.
    """
    import json
    from urllib.parse import urlparse

    host = urlparse(base_url).hostname or ""
    env_vars: list[str] = []
    start = ""
    if start_command:
        start = f"""
  # Autopilot starts the app before a run (unless it's already running) and
  # stops it afterwards. Its output goes to .autopilot/app.log.
  start:
    command: {json.dumps(start_command)}
    timeout_s: 120               # Java/Node apps can take a while to start
"""

    def identity(name: str) -> str:
        upper = name.upper()
        if login is not None:
            env_vars.extend([f"{upper}_USERNAME", f"{upper}_PASSWORD"])
            return f"""  {name}:
    header: {header}
    prefix: "{prefix}"
    login:                   # fresh token at the start of every run
      method: {login.method}
      path: {login.path}
      format: {login.format}
      body:
        {login.username_field}: "${{{upper}_USERNAME}}"
        {login.password_field}: "${{{upper}_PASSWORD}}"
      token_path: {login.token_path}
"""
        env_vars.append(f"{upper}_TOKEN")
        return f"""  {name}:
    header: {header}
    prefix: "{prefix}"
    value_env: {upper}_TOKEN
"""

    how = (
        f"They log in with {login.description}; usernames and passwords come from\n"
        "# environment variables (or a .env file next to this one)."
        if login is not None
        else "Tokens are read from environment variables (or a .env file next to this one)."
    )
    users = identity("user_a") + identity("user_b")
    text = f"""\
# API Autopilot configuration. Start read-only, review the first report, then
# enable writes if this is a staging/test environment you're allowed to change.
spec: {spec}

target:
  base_url: {base_url}
  allowed_hosts: [{host}]   # requests to any other host are refused
  allow_writes: false        # POST/PUT/PATCH; only enable on staging/test data
  allow_deletes: false       # DELETE
{start}
# Two test users, so Autopilot can check that one can't reach the other's data.
# Auth detected from the spec: {auth_note}.
# {how}
identities:
{users}
budgets:
  max_requests: 100
  max_iterations: 2
  max_llm_calls: 10
  request_timeout_s: 10

confirmation_attempts: 2

llm:
  # Uses whichever AI key it finds: OPENAI_API_KEY, GEMINI_API_KEY, GROQ_API_KEY
  # or ANTHROPIC_API_KEY. To choose, set provider to openai, gemini, groq or
  # claude, or "heuristic" for no AI (nothing leaves your machine). For any other
  # OpenAI-compatible service, use provider openai with base_url and api_key_env.
  provider: auto
  effort: medium
"""
    return text, env_vars

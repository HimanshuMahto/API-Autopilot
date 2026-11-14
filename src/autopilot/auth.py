"""Log identities in at the start of a run, so nobody has to paste tokens.

An identity with a `login:` block sends that request (body values may use
${ENV_VAR} from .env), reads the token from the response and uses it for the
rest of the run. Credentials and tokens never appear in errors or reports, and
the planner never sees any of it.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlparse

import httpx

from .config import Config, ConfigError, Identity, expand_env
from .executor import extract


class LoginError(ConfigError):
    pass


def _login_url(config: Config, path: str) -> str:
    if path.startswith(("http://", "https://")):
        return path
    return config.target.base_url.rstrip("/") + "/" + path.lstrip("/")


async def log_in_identity(name: str, identity: Identity, config: Config, client: httpx.AsyncClient) -> None:
    login = identity.login
    assert login is not None
    url = _login_url(config, login.path)
    host = urlparse(url).hostname or ""
    if host not in config.target.allowed_hosts:
        raise LoginError(f"Login for {name!r} goes to {host!r}, which is not in target.allowed_hosts")
    body = expand_env(login.body, what=f"Login for {name!r}")
    kwargs = {"json": body} if login.format == "json" else {"data": body}
    try:
        response = await client.request(
            login.method.upper(),
            url,
            headers={"Accept": "application/json", "User-Agent": "api-autopilot/0.1", "Connection": "close"},
            timeout=config.budgets.request_timeout_s,
            **kwargs,
        )
    except httpx.HTTPError as exc:
        raise LoginError(f"Login for {name!r} failed: could not reach {url} ({type(exc).__name__})") from None
    if not 200 <= response.status_code < 300:
        # The response body is deliberately not shown: it can echo credentials.
        raise LoginError(
            f"Login for {name!r} failed: {login.method.upper()} {login.path} returned "
            f"{response.status_code}. Check the username and password in .env."
        )
    try:
        payload = response.json()
    except ValueError:
        payload = None
    headers = {k.lower(): v for k, v in response.headers.items()}
    try:
        token = extract(login.token_path, response.status_code, payload, headers)
    except KeyError:
        keys = sorted(payload) if isinstance(payload, dict) else []
        raise LoginError(
            f"Login for {name!r} succeeded but no token was found at {login.token_path!r}. "
            f"The response has these top-level fields: {', '.join(keys) or 'none'}. "
            "Set identities.<name>.login.token_path."
        ) from None
    if not isinstance(token, str) or not token:
        raise LoginError(f"Login for {name!r}: the value at {login.token_path!r} is not a token string")
    identity.set_token(token)


async def log_in(config: Config, client: httpx.AsyncClient, on_event: Callable[[str], None] | None = None) -> list[str]:
    """Log in every identity that has a login block. Returns the names logged in."""
    done = []
    for name, identity in config.identities.items():
        if identity.login is None:
            continue
        await log_in_identity(name, identity, config, client)
        done.append(name)
    if done and on_event:
        on_event(f"Logged in as {', '.join(done)}.")
    return done

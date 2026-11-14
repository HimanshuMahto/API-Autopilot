"""Find an API's spec on a running server by trying the addresses frameworks use."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from .spec import ApiSpec, SpecError, parse_spec_text

# Where common frameworks serve their spec by default, most common first.
SPEC_PATHS = (
    "/openapi.json",  # FastAPI, Litestar, flask-smorest, many others
    "/openapi.yaml",
    "/v3/api-docs",  # Spring Boot (springdoc)
    "/api-json",  # NestJS (SwaggerModule.setup("api"))
    "/swagger.json",  # drf-yasg, Go swag, many Express setups
    "/swagger/v1/swagger.json",  # ASP.NET (Swashbuckle)
    "/v2/api-docs",  # Spring Boot (springfox, older)
    "/api-docs",
    "/api-docs.json",
    "/swagger.yaml",
    "/api/openapi.json",
    "/api/swagger.json",
    "/docs/openapi.json",
    "/apispec_1.json",  # Flask (flasgger)
    "/openapi",
    "/api/schema/",  # Django REST Framework (drf-spectacular)
)


@dataclass
class Discovered:
    url: str
    text: str
    spec: ApiSpec


@dataclass
class DiscoveryFailed(SpecError):
    base_url: str
    tried: list[str] = field(default_factory=list)
    reached: bool = False  # did the server answer at all?

    def __str__(self) -> str:
        if not self.reached:
            return f"Nothing is answering at {self.base_url}. Is the app running?"
        return (
            f"No OpenAPI or Swagger spec found at {self.base_url} "
            f"(tried {len(self.tried)} common addresses). Pass the spec's address with --spec."
        )


def candidate_urls(base_url: str) -> list[str]:
    """Each known path under the base URL, then under the server root if they differ."""
    base = base_url.rstrip("/")
    parsed = urlparse(base)
    root = f"{parsed.scheme}://{parsed.netloc}"
    urls = [base + path for path in SPEC_PATHS]
    if root != base:
        urls += [root + path for path in SPEC_PATHS]
    return list(dict.fromkeys(urls))


def discover_spec(base_url: str, client: httpx.Client | None = None, timeout: float = 10.0) -> Discovered:
    """Return the first address that serves a valid spec. Only sends GET requests."""
    owns_client = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=True)
    tried: list[str] = []
    reached = False
    try:
        for url in candidate_urls(base_url):
            tried.append(url)
            try:
                response = client.get(url, headers={"Accept": "application/json, application/yaml;q=0.9, */*;q=0.5"})
            except httpx.HTTPError:
                continue
            reached = True
            if response.status_code != 200 or not response.text.strip():
                continue
            try:
                spec = parse_spec_text(response.text, url)
            except SpecError:
                continue  # an HTML page or some other JSON; keep looking
            return Discovered(url=url, text=response.text, spec=spec)
    finally:
        if owns_client:
            client.close()
    raise DiscoveryFailed(base_url=base_url, tried=tried, reached=reached)

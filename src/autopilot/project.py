"""Find an API spec inside a project folder, without the app running if possible.

Three tiers, cheapest first:
1. A spec file already in the repo (openapi.yaml, swagger.json, docs/swagger.json ...).
2. Export it from code: FastAPI (app.openapi()) and Django REST Framework with
   drf-spectacular (manage.py spectacular). This imports the project's code, using
   the project's own virtualenv when there is one, but does not start a server.
3. Frameworks that only produce a spec while running (Spring Boot, NestJS,
   ASP.NET, Express, Flask): report how to start the app and where the spec is.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .spec import ApiSpec, SpecError, parse_spec_text

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", ".env", "__pycache__", "dist",
    "build", "target", ".autopilot", ".next", ".nuxt", "vendor", "site-packages", ".tox",
    ".mypy_cache", ".pytest_cache", ".idea", ".vscode", "coverage", "bin", "obj",
}
MAX_FILES = 20_000
MAX_SPEC_BYTES = 5_000_000
_SPEC_MARKER = re.compile(rb'["\']?(openapi|swagger)["\']?\s*:\s*["\']?(3\.|2\.0)')


@dataclass
class Framework:
    name: str
    evidence: str  # which file told us
    spec_path: str | None = None  # where the running app serves its spec
    start_hint: str | None = None  # a likely command to start it
    port: int | None = None  # its usual local port


@dataclass
class ProjectScan:
    root: Path
    spec_files: list[tuple[Path, ApiSpec]] = field(default_factory=list)
    frameworks: list[Framework] = field(default_factory=list)
    exported: tuple[str, ApiSpec, str] | None = None  # (text, spec, how)
    export_errors: list[str] = field(default_factory=list)

    @property
    def runtime_frameworks(self) -> list[Framework]:
        return [f for f in self.frameworks if f.spec_path]


def _walk(root: Path):
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            count += 1
            if count > MAX_FILES:
                return
            yield Path(dirpath) / name


def _read(path: Path, limit: int = 200_000) -> str:
    try:
        with path.open("rb") as fh:
            return fh.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def find_spec_files(root: Path) -> list[tuple[Path, ApiSpec]]:
    """Valid OpenAPI/Swagger files in the project, best candidates first."""
    found = []
    for path in _walk(root):
        if path.suffix.lower() not in (".json", ".yaml", ".yml"):
            continue
        try:
            if path.stat().st_size > MAX_SPEC_BYTES:
                continue
            with path.open("rb") as fh:
                head = fh.read(4096)
        except OSError:
            continue
        if not _SPEC_MARKER.search(head):
            continue
        try:
            spec = parse_spec_text(path.read_text(errors="replace"), str(path))
        except SpecError:
            continue
        found.append((path, spec))
    # Prefer OpenAPI 3 over Swagger 2, more operations, then shallower paths.
    found.sort(key=lambda item: (item[1].is_swagger, -len(item[1].operations), len(item[0].parts), str(item[0])))
    return found


def _deps_text(root: Path) -> str:
    names = ("requirements.txt", "requirements-dev.txt", "pyproject.toml", "Pipfile", "setup.py",
             "setup.cfg", "package.json", "pom.xml", "build.gradle", "build.gradle.kts")
    text = "\n".join(_read(root / n) for n in names if (root / n).is_file())
    for req in sorted(root.glob("requirements/*.txt")):
        text += "\n" + _read(req)
    for csproj in list(root.glob("*.csproj")) + list(root.glob("*/*.csproj")):
        text += "\n" + _read(csproj)
    return text.lower()


_FASTAPI_APP = re.compile(r"^(\w+)\s*(?::\s*\w+\s*)?=\s*FastAPI\(", re.MULTILINE)


def find_fastapi_apps(root: Path) -> list[tuple[str, str]]:
    """(module, attribute) pairs for `app = FastAPI(...)` assignments."""
    apps = []
    for path in _walk(root):
        if path.suffix != ".py":
            continue
        text = _read(path)
        if "FastAPI(" not in text:
            continue
        for match in _FASTAPI_APP.finditer(text):
            rel = path.relative_to(root).with_suffix("")
            parts = list(rel.parts)
            if parts and parts[0] == "src":
                parts = parts[1:]
            if parts and parts[-1] == "__init__":
                parts = parts[:-1]
            if parts:
                apps.append((".".join(parts), match.group(1)))
    # main/app modules first, shallow before deep
    apps.sort(key=lambda a: (not re.search(r"(^|\.)(main|app|server|api)$", a[0]), a[0].count("."), a[0]))
    return apps


def detect_frameworks(root: Path) -> list[Framework]:
    deps = _deps_text(root)
    found: list[Framework] = []
    has = lambda *words: any(w in deps for w in words)  # noqa: E731

    if has("fastapi") or find_fastapi_apps(root):
        apps = find_fastapi_apps(root)
        target = f"{apps[0][0]}:{apps[0][1]}" if apps else "main:app"
        found.append(Framework("FastAPI", "dependencies or `FastAPI(` in code", "/openapi.json",
                               f"uvicorn {target} --port 8000", 8000))
    if (root / "manage.py").is_file() and has("django"):
        if has("drf-spectacular", "drf_spectacular"):
            found.append(Framework("Django REST Framework (drf-spectacular)", "manage.py + drf-spectacular",
                                   "/api/schema/", "python manage.py runserver 8000", 8000))
        elif has("drf-yasg", "drf_yasg"):
            found.append(Framework("Django REST Framework (drf-yasg)", "manage.py + drf-yasg",
                                   "/swagger.json", "python manage.py runserver 8000", 8000))
        else:
            found.append(Framework("Django", "manage.py", None, "python manage.py runserver 8000", 8000))
    if has("springdoc"):
        build = "./mvnw spring-boot:run" if (root / "mvnw").exists() else (
            "./gradlew bootRun" if (root / "gradlew").exists() else "mvn spring-boot:run")
        found.append(Framework("Spring Boot (springdoc)", "springdoc dependency", "/v3/api-docs", build, 8080))
    elif has("springfox"):
        found.append(Framework("Spring Boot (springfox)", "springfox dependency", "/v2/api-docs",
                               "mvn spring-boot:run", 8080))
    if has("@nestjs/swagger"):
        found.append(Framework("NestJS", "@nestjs/swagger in package.json", "/api-json", "npm run start", 3000))
    elif has("swagger-jsdoc", "swagger-ui-express", "express-openapi", "tsoa"):
        found.append(Framework("Express (Swagger)", "package.json", "/api-docs.json", "npm start", 3000))
    if has("swashbuckle"):
        found.append(Framework("ASP.NET (Swashbuckle)", ".csproj", "/swagger/v1/swagger.json", "dotnet run", 5000))
    if has("flask-smorest", "flask_smorest"):
        found.append(Framework("Flask (flask-smorest)", "dependencies", "/openapi.json", "flask run --port 5000", 5000))
    elif has("flasgger"):
        found.append(Framework("Flask (flasgger)", "dependencies", "/apispec_1.json", "flask run --port 5000", 5000))
    return found


def project_python(root: Path) -> str:
    """The project's own interpreter if it has a virtualenv, else this one."""
    for venv in (".venv", "venv", "env"):
        for rel in ("bin/python", "Scripts/python.exe"):
            candidate = root / venv / rel
            if candidate.exists():
                return str(candidate)
    return sys.executable


_EXPORT_FASTAPI = """
import json, sys
sys.path[:0] = [{root!r}, {src!r}]
module = __import__({module!r}, fromlist=["_"])
print(json.dumps(getattr(module, {attr!r}).openapi()))
"""


def export_fastapi(root: Path, module: str, attr: str, python: str, timeout: float = 60) -> str:
    code = _EXPORT_FASTAPI.format(root=str(root), src=str(root / "src"), module=module, attr=attr)
    proc = subprocess.run([python, "-c", code], cwd=root, capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        last = (proc.stderr.strip().splitlines() or ["(no output)"])[-1]
        raise RuntimeError(f"importing {module}:{attr} failed: {last}")
    return proc.stdout.strip().splitlines()[-1]  # the JSON is the last line printed


def export_drf_spectacular(root: Path, python: str, timeout: float = 120) -> str:
    proc = subprocess.run([python, "manage.py", "spectacular", "--format", "openapi-json"], cwd=root,
                          capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        last = (proc.stderr.strip().splitlines() or ["(no output)"])[-1]
        raise RuntimeError(f"manage.py spectacular failed: {last}")
    return proc.stdout


def scan_project(root: Path, export: bool = True) -> ProjectScan:
    root = root.resolve()
    scan = ProjectScan(root=root, spec_files=find_spec_files(root), frameworks=detect_frameworks(root))
    if scan.spec_files or not export:
        return scan
    python = project_python(root)
    for fw in scan.frameworks:
        try:
            if fw.name == "FastAPI":
                for module, attr in find_fastapi_apps(root)[:3]:
                    try:
                        text = export_fastapi(root, module, attr, python)
                        json.loads(text)
                        scan.exported = (text, parse_spec_text(text, f"{module}:{attr}"), f"exported from {module}:{attr}")
                        return scan
                    except (RuntimeError, ValueError, SpecError) as exc:
                        scan.export_errors.append(f"FastAPI: {exc}")
            elif "drf-spectacular" in fw.name:
                text = export_drf_spectacular(root, python)
                scan.exported = (text, parse_spec_text(text, "manage.py spectacular"), "exported with manage.py spectacular")
                return scan
        except (RuntimeError, SpecError, subprocess.TimeoutExpired, OSError) as exc:
            scan.export_errors.append(f"{fw.name}: {exc}")
    return scan

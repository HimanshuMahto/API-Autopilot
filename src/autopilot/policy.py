"""Tool gateway: every step is checked here before anything is sent (PRD §9.3, §11).

The LLM proposes; this module decides. Nothing in the LLM output can widen what
is allowed, because every limit comes from the user's config.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from .config import Config
from .models import Step
from .spec import ApiSpec


class PolicyViolation(Exception):
    pass


class BudgetExhausted(Exception):
    pass


@dataclass
class Policy:
    config: Config
    spec: ApiSpec
    requests_sent: int = 0

    def __post_init__(self) -> None:
        target = self.config.target
        self.allow_writes = target.allow_writes
        self.allow_deletes = target.allow_deletes
        # Production-looking hosts are forced read-only no matter what the config says.
        if target.looks_like_production:
            self.allow_writes = False
            self.allow_deletes = False

    def check_step(self, step: Step) -> None:
        """Raise PolicyViolation if this step must not run."""
        try:
            op = self.spec.get(step.operation_id)
        except ValueError as exc:
            raise PolicyViolation(str(exc)) from None
        if step.identity not in self.config.identity_names:
            raise PolicyViolation(f"Unknown identity {step.identity!r}")
        if op.method == "DELETE" and not self.allow_deletes:
            raise PolicyViolation("DELETE requests are disabled (target.allow_deletes)")
        if op.method in {"POST", "PUT", "PATCH"} and not self.allow_writes:
            raise PolicyViolation(f"{op.method} requests are disabled (target.allow_writes)")
        missing = [p for p in op.path_param_names if p not in step.path_params]
        if missing:
            raise PolicyViolation(f"Missing path parameters: {', '.join(missing)}")

    def check_url(self, url: str) -> None:
        host = urlparse(url).hostname or ""
        if host not in self.config.target.allowed_hosts:
            raise PolicyViolation(f"Host {host!r} is not in target.allowed_hosts")

    def spend_request(self) -> None:
        if self.requests_sent >= self.config.budgets.max_requests:
            raise BudgetExhausted(f"Request budget of {self.config.budgets.max_requests} used up")
        self.requests_sent += 1

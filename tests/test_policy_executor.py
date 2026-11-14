import pytest

from autopilot.config import Config
from autopilot.executor import extract, substitute
from autopilot.models import Step
from autopilot.policy import BudgetExhausted, Policy, PolicyViolation

from .conftest import execute, scenario


def test_deletes_blocked_by_default(config, spec):
    exchanges, _ = execute(config, spec, scenario(Step(operation_id="deleteOrder", identity="alice", path_params={"order_id": 1})))
    assert exchanges[0].blocked and "allow_deletes" in exchanges[0].error


def test_unknown_operation_and_identity_blocked(config, spec):
    policy = Policy(config, spec)
    with pytest.raises(PolicyViolation, match="Unknown operation"):
        policy.check_step(Step(operation_id="nope"))
    with pytest.raises(PolicyViolation, match="Unknown identity"):
        policy.check_step(Step(operation_id="getMe", identity="mallory"))


def test_host_allowlist(config, spec):
    policy = Policy(config, spec)
    policy.check_url("http://127.0.0.1:8765/me")
    with pytest.raises(PolicyViolation, match="allowed_hosts"):
        policy.check_url("http://169.254.169.254/latest/meta-data")


def test_production_host_forced_read_only(spec):
    cfg = Config.model_validate(
        {
            "spec": "x",
            "target": {"base_url": "https://api.prod.example.com", "allow_writes": True, "allow_deletes": True},
        }
    )
    policy = Policy(cfg, spec)
    assert not policy.allow_writes and not policy.allow_deletes
    with pytest.raises(PolicyViolation):
        policy.check_step(Step(operation_id="createOrder", identity="anonymous", body={}))


def test_request_budget(config, spec):
    config.budgets.max_requests = 1
    get = Step(operation_id="listProducts")
    with pytest.raises(BudgetExhausted):
        execute(config, spec, scenario(get, get))


def test_tokens_come_from_env(monkeypatch, spec):
    cfg = Config.model_validate(
        {"spec": "x", "target": {"base_url": "http://localhost"}, "identities": {"a": {"value_env": "TOKEN_A"}}}
    )
    monkeypatch.setenv("TOKEN_A", "s3cret")
    assert cfg.identities["a"].header_value("a") == "Bearer s3cret"


def test_substitute_keeps_types():
    assert substitute({"id": "{{n}}", "s": "x-{{n}}"}, {"n": 7}) == {"id": 7, "s": "x-7"}


def test_extract_paths():
    body = {"items": [{"id": 3}]}
    assert extract("body.items.0.id", 200, body, {}) == 3
    assert extract("status", 201, None, {}) == 201
    with pytest.raises(KeyError):
        extract("body.items.5.id", 200, body, {})


def test_unresolved_variable_blocks_step(config, spec):
    exchanges, violations = execute(
        config, spec, scenario(Step(operation_id="getOrder", identity="alice", path_params={"order_id": "{{missing}}"}))
    )
    assert exchanges[0].blocked and "unresolved" in exchanges[0].error
    assert violations == []

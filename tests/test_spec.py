import pytest

from autopilot.spec import SpecError, parse_spec


def test_demo_spec_parses(spec):
    assert len(spec.operations) == 9
    get_order = spec.get("getOrder")
    assert get_order.method == "GET"
    assert get_order.path_param_names == ["order_id"]  # path-level parameter inherited
    assert get_order.requires_auth
    assert not spec.get("listProducts").requires_auth


def test_refs_are_resolved(spec):
    create = spec.get("createOrder")
    assert create.request_schema["properties"]["quantity"]["minimum"] == 1
    assert "$ref" not in str(create.responses)


def test_rejects_unknown_spec_versions():
    with pytest.raises(SpecError, match="OpenAPI 3.x and Swagger 2.0"):
        parse_spec({"swagger": "1.2", "paths": {}})
    with pytest.raises(SpecError, match="openapi: missing"):
        parse_spec({"paths": {}})


def test_rejects_duplicate_operation_ids():
    doc = {
        "openapi": "3.0.3",
        "paths": {
            "/a": {"get": {"operationId": "x", "responses": {}}},
            "/b": {"get": {"operationId": "x", "responses": {}}},
        },
    }
    with pytest.raises(SpecError, match="Duplicate"):
        parse_spec(doc)


def test_empty_security_requirement_means_optional_auth():
    doc = {
        "openapi": "3.0.3",
        "security": [{"bearer": []}],
        "paths": {
            "/open": {"get": {"security": [{}, {"bearer": []}], "responses": {}}},
            "/closed": {"get": {"responses": {}}},
        },
    }
    spec = parse_spec(doc)
    assert not spec.get("get_open").requires_auth
    assert spec.get("get_closed").requires_auth

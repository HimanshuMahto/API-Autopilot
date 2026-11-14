"""Each planted defect is caught by the right oracle; correct behaviour stays clean."""

from autopilot.models import Step
from demo import shop_api

from .conftest import execute, scenario


def create(identity="alice", quantity=1, var="oid"):
    return Step(
        operation_id="createOrder",
        identity=identity,
        body={"product_id": 1, "quantity": quantity},
        extract={var: "body.id"},
    )


def oracles(violations):
    return {v.oracle for v in violations}


def test_d1_unenforced_quantity_minimum(config, spec):
    _, violations = execute(config, spec, scenario(create(quantity=-5)))
    assert "spec.constraint" in oracles(violations)
    constraint = next(v for v in violations if v.oracle == "spec.constraint")
    assert constraint.key == "minimum:quantity"


def test_d2_cross_user_read(config, spec):
    sc = scenario(create(), Step(operation_id="getOrder", identity="bob", path_params={"order_id": "{{oid}}"}))
    _, violations = execute(config, spec, sc)
    assert oracles(violations) == {"builtin.auth_differential"}
    assert violations[0].step_index == 1


def test_d3_double_cancel_crashes(config, spec):
    cancel = Step(operation_id="cancelOrder", identity="alice", path_params={"order_id": "{{oid}}"})
    exchanges, violations = execute(config, spec, scenario(create(), cancel, cancel))
    assert [e.status for e in exchanges] == [201, 200, 500]
    # The 500 is reported once, not also as an undocumented status.
    assert oracles(violations) == {"builtin.server_error"}


def test_d4_price_type_mismatch(config, spec):
    _, violations = execute(config, spec, scenario(Step(operation_id="getProduct", path_params={"product_id": 3})))
    assert oracles(violations) == {"builtin.schema_conformance"}
    assert violations[0].key == "200:type:price"


def test_known_good_behaviour_is_clean(config, spec):
    controls = [
        scenario(Step(operation_id="getMe")),  # 401 to anonymous
        scenario(Step(operation_id="getMe", identity="alice")),
        scenario(Step(operation_id="getProduct", path_params={"product_id": 1})),
        scenario(create(), Step(operation_id="listOrders", identity="bob")),
        scenario(create(), Step(operation_id="cancelOrder", identity="bob", path_params={"order_id": "{{oid}}"})),
        scenario(create(), Step(operation_id="getOrder", identity="anonymous", path_params={"order_id": "{{oid}}"})),
        scenario(Step(operation_id="createOrder", identity="alice", body={"quantity": 1})),  # 400
    ]
    for sc in controls:
        _, violations = execute(config, spec, sc)
        assert violations == [], (sc.steps, violations)


def test_owner_reading_own_order_is_not_cross_user(config, spec):
    sc = scenario(create(), Step(operation_id="getOrder", identity="alice", path_params={"order_id": "{{oid}}"}))
    _, violations = execute(config, spec, sc)
    assert violations == []


def test_fingerprint_ignores_list_position(config, spec):
    """The same defect at a different list index must keep the same fingerprint."""
    list_orders = Step(operation_id="listOrders", identity="alice")
    _, first = execute(config, spec, scenario(create(quantity=0), list_orders))
    shop_api.reset()
    _, second = execute(config, spec, scenario(create(), create(var="o2"), create(quantity=0, var="o3"), list_orders))
    fp = lambda vs: {v.fingerprint() for v in vs if v.operation_id == "listOrders"}  # noqa: E731
    assert fp(first) and fp(first) == fp(second)

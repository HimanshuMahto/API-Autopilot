"""Demo Shop API with deliberately planted defects.

The contract lives in demo/openapi.yaml. This implementation breaks it in four
places so API Autopilot has something real to find:

  D1  POST /orders accepts quantity <= 0 or > 100 (spec says 1..100), producing
      a negative or huge total.                         -> spec.constraint
  D2  GET /orders/{id} has no ownership check, so one user can read another
      user's order.                                     -> builtin.auth_differential
  D3  Cancelling an already-cancelled order crashes with a 500 instead of
      returning 409.                                    -> builtin.server_error
  D4  Product 3 returns its price as a string.          -> builtin.schema_conformance

Everything else (listing own orders, deleting, /me, auth on every protected
route) is implemented correctly and acts as the known-good control.

Run it with:  uv run uvicorn demo.shop_api:app --port 8765
"""

from __future__ import annotations

import itertools

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response

TOKENS = {"alice-token": "alice", "bob-token": "bob"}

app = FastAPI(title="Demo Shop API", openapi_url=None, docs_url=None, redoc_url=None)


def _fresh_state() -> dict:
    return {
        "products": {
            1: {"id": 1, "name": "Keyboard", "price": 49.0, "stock": 100},
            2: {"id": 2, "name": "Mouse", "price": 19.5, "stock": 100},
            # D4: price is a string, the spec says number.
            3: {"id": 3, "name": "Monitor", "price": "199.00", "stock": 10},
        },
        "orders": {},
        "ids": itertools.count(1),
    }


STATE = _fresh_state()


def reset() -> None:
    """Reset all data. Used by tests to start from a clean state."""
    global STATE
    STATE = _fresh_state()


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


def _user(authorization: str | None) -> str | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return TOKENS.get(authorization.removeprefix("Bearer ").strip())


@app.get("/me")
def get_me(authorization: str | None = Header(default=None)):
    user = _user(authorization)
    if not user:
        return _error(401, "Missing or invalid token")
    return {"username": user}


@app.get("/products")
def list_products():
    return list(STATE["products"].values())


@app.get("/products/{product_id}")
def get_product(product_id: int):
    product = STATE["products"].get(product_id)
    if not product:
        return _error(404, "No such product")
    return product


@app.get("/orders")
def list_orders(authorization: str | None = Header(default=None)):
    user = _user(authorization)
    if not user:
        return _error(401, "Missing or invalid token")
    return [o for o in STATE["orders"].values() if o["owner"] == user]


@app.post("/orders", status_code=201)
async def create_order(request: Request, authorization: str | None = Header(default=None)):
    user = _user(authorization)
    if not user:
        return _error(401, "Missing or invalid token")
    try:
        body = await request.json()
    except ValueError:
        return _error(400, "Body must be JSON")
    if not isinstance(body, dict):
        return _error(400, "Body must be an object")
    product_id, quantity = body.get("product_id"), body.get("quantity")
    if not isinstance(product_id, int) or not isinstance(quantity, int):
        return _error(400, "product_id and quantity must be integers")
    # D1: no range check on quantity.
    product = STATE["products"].get(product_id)
    if not product:
        return _error(404, "No such product")
    order_id = next(STATE["ids"])
    order = {
        "id": order_id,
        "product_id": product_id,
        "quantity": quantity,
        "total": round(float(product["price"]) * quantity, 2),
        "status": "placed",
        "owner": user,
    }
    STATE["orders"][order_id] = order
    return JSONResponse(order, status_code=201)


@app.get("/orders/{order_id}")
def get_order(order_id: int, authorization: str | None = Header(default=None)):
    user = _user(authorization)
    if not user:
        return _error(401, "Missing or invalid token")
    order = STATE["orders"].get(order_id)
    # D2: missing `or order["owner"] != user`.
    if not order:
        return _error(404, "No such order")
    return order


@app.delete("/orders/{order_id}")
def delete_order(order_id: int, authorization: str | None = Header(default=None)):
    user = _user(authorization)
    if not user:
        return _error(401, "Missing or invalid token")
    order = STATE["orders"].get(order_id)
    if not order or order["owner"] != user:
        return _error(404, "No such order")
    del STATE["orders"][order_id]
    return Response(status_code=204)


@app.post("/orders/{order_id}/cancel")
def cancel_order(order_id: int, authorization: str | None = Header(default=None)):
    user = _user(authorization)
    if not user:
        return _error(401, "Missing or invalid token")
    order = STATE["orders"].get(order_id)
    if not order or order["owner"] != user:
        return _error(404, "No such order")
    # D3: the "already cancelled" branch looks up a key that doesn't exist.
    if order["status"] == "cancelled":
        raise RuntimeError(order["cancelled_reason"])  # noqa: B904 - deliberate bug
    order["status"] = "cancelled"
    return order


@app.middleware("http")
async def _crash_quietly(request: Request, call_next):
    """Turn crashes into a plain 500 with a one-line log, instead of a full traceback.

    The planted bugs crash on purpose; a wall of tracebacks in the demo terminal
    looks like the demo itself is broken. Clients still get the same 500.
    """
    try:
        return await call_next(request)
    except Exception as exc:  # noqa: BLE001 - every crash becomes a 500, by design
        print(
            f"demo: 500 on {request.method} {request.url.path} "
            f"({type(exc).__name__}: {exc}) - planted bug, expected while testing",
            flush=True,
        )
        return _error(500, "Internal server error")

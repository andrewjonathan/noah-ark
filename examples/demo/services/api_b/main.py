import json
import logging
import os
import urllib.request

from fastapi import FastAPI

API_A = os.environ.get("API_A_URL", "http://127.0.0.1:8001")
log = logging.getLogger("uvicorn.error")
app = FastAPI(title="Service B")


@app.get("/health")
def health():
    return {"status": "ok", "service": "api-b"}


@app.get("/orders/{order_id}")
def get_order(order_id: int):
    with urllib.request.urlopen(f"{API_A}/items/{order_id}", timeout=5) as resp:
        item = json.load(resp)
    log.info("order %s: fetched item from api-a", order_id)
    return {"order": order_id, "item": item}

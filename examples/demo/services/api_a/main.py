import logging

from fastapi import FastAPI

log = logging.getLogger("uvicorn.error")
app = FastAPI(title="Service A")


@app.get("/health")
def health():
    return {"status": "ok", "service": "api-a"}


@app.get("/items/{item_id}")
def get_item(item_id: int):
    log.info("serving item %s", item_id)
    return {"id": item_id, "name": f"item-{item_id}", "from": "api-a"}

"""FastAPI application serving the Highcharts option-tracker dashboard."""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import time
import traceback
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import orjson
import requests
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from option_tracker.utils.pc_utils import get_headers
from option_tracker.webapp.data import (
    build_option_chain_payload,
    build_leap_payload,
    build_spot_payload,
    get_cached_option_chain,
    get_cached_spot_price,
    get_cached_spy_price,
    _compute_gex,
    _compute_charm,
    record_pin,
    get_pin_history,
)

STATIC_DIR = pathlib.Path(__file__).parent / "static"

# Streaming cadence knobs (seconds) — override via environment variables.
WS_INTERVAL_SECONDS = int(os.environ.get("OPTION_STREAM_INTERVAL", "15"))
SPOT_INTERVAL_SECONDS = int(os.environ.get("SPOT_STREAM_INTERVAL", "5"))
SPOT_CLOSED_INTERVAL_SECONDS = int(os.environ.get("SPOT_STREAM_CLOSED_INTERVAL", "60"))


def _et_epoch_ms():
    """Current ET wall-clock encoded as an epoch (matches Nasdaq chart x values)."""
    et = datetime.now(ZoneInfo("America/New_York"))
    return int(et.replace(tzinfo=timezone.utc).timestamp() * 1000)

app = FastAPI(title="Options Pulse")
app.add_middleware(GZipMiddleware, minimum_size=500)


@app.middleware("http")
async def no_store(request, call_next):
    """Always serve fresh HTML/JS/CSS so browsers never run stale dashboard code."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith(("/leap", "/static")):
        response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "option_chain.html")


@app.get("/leap")
def leap_page():
    return FileResponse(STATIC_DIR / "leap.html")


@app.get("/favicon.ico")
def favicon():
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


def _diff_message(last, payload):
    """Build a 'full' or per-expiry 'delta' message for the WebSocket stream."""
    same_structure = last is not None and (
        [e["expiry"] for e in last["expiries"]] == [e["expiry"] for e in payload["expiries"]]
    )
    if not same_structure:
        return {"type": "full", "payload": payload}

    last_by = {e["expiry"]: e for e in last["expiries"]}
    changed = {
        e["expiry"]: e
        for e in payload["expiries"]
        if json.dumps(e, sort_keys=True) != json.dumps(last_by.get(e["expiry"]), sort_keys=True)
    }
    return {
        "type": "delta",
        "lastSalePrice": payload["lastSalePrice"],
        "prevClose": payload["prevClose"],
        "marketStatus": payload["marketStatus"],
        "dataSource": payload["dataSource"],
        "timestamp": payload["timestamp"],
        "expiries": changed,
        "gex": payload["gex"],
        "charm": payload["charm"],
        "wheel": payload["wheel"],
    }


@app.websocket("/ws/option-chain")
async def ws_option_chain(ws: WebSocket):
    """Stream option-chain updates: an initial full payload then per-expiry deltas."""
    await ws.accept()
    last = None
    try:
        while True:
            try:
                payload = await asyncio.to_thread(get_cached_option_chain)
            except Exception as exc:
                traceback.print_exc()
                await ws.send_text(orjson.dumps({"error": str(exc)}).decode())
                await asyncio.sleep(WS_INTERVAL_SECONDS)
                continue
            await ws.send_text(orjson.dumps(_diff_message(last, payload)).decode())
            last = payload
            await asyncio.sleep(WS_INTERVAL_SECONDS)
    except WebSocketDisconnect:
        return
    except Exception:
        traceback.print_exc()
        return


@app.get("/api/option-chain")
def api_option_chain():
    try:
        return get_cached_option_chain()
    except Exception as exc:
        traceback.print_exc()
        return JSONResponse(status_code=502, content={"error": str(exc)})


@app.get("/api/gex")
def api_gex(k: float = Query(0.0, ge=0.0, le=2.0)):
    """Recompute GEX + charm off the cached chain using adjusted OI (OI + k·vol)."""
    try:
        payload = get_cached_option_chain()
        spot = payload.get("lastSalePrice")
        expiries = payload.get("expiries") or []
        gex = _compute_gex(expiries, spot, blend_k=k)
        record_pin(gex)
        return {
            "k": k,
            "gex": gex,
            "charm": _compute_charm(expiries, spot, blend_k=k),
            "pin_history": get_pin_history(),
        }
    except Exception as exc:
        traceback.print_exc()
        return JSONResponse(status_code=502, content={"error": str(exc)})


@app.get("/api/leap")
def api_leap():
    try:
        return build_leap_payload()
    except Exception as exc:
        traceback.print_exc()
        return JSONResponse(status_code=502, content={"error": str(exc)})


@app.get("/api/spot")
def api_spot():
    try:
        return build_spot_payload()
    except Exception as exc:
        traceback.print_exc()
        return JSONResponse(status_code=502, content={"error": str(exc)})


@app.websocket("/ws/spot")
async def ws_spot(ws: WebSocket):
    """Stream TSLA's live last price; the client may set its own cadence."""
    await ws.accept()
    cfg = {"interval": SPOT_INTERVAL_SECONDS}

    async def reader():
        # Client sends {"interval": N} to change this connection's cadence.
        try:
            while True:
                msg = await ws.receive_text()
                try:
                    d = json.loads(msg)
                    if "interval" in d:
                        cfg["interval"] = max(1, min(300, int(d["interval"])))
                except Exception:
                    pass
        except Exception:
            return

    reader_task = asyncio.create_task(reader())
    try:
        while True:
            try:
                price, status = await asyncio.to_thread(get_cached_spot_price)
                spy = await asyncio.to_thread(get_cached_spy_price)
            except Exception:
                await asyncio.sleep(cfg["interval"])
                continue
            await ws.send_text(orjson.dumps(
                {"t": _et_epoch_ms(), "price": price, "spy": spy, "status": status}
            ).decode())
            # Slow the stream when the market is closed (price won't move).
            is_open = bool(status) and "open" in status.lower()
            delay = cfg["interval"] if is_open else max(cfg["interval"], SPOT_CLOSED_INTERVAL_SECONDS)
            await asyncio.sleep(delay)
    except WebSocketDisconnect:
        return
    except Exception:
        return
    finally:
        reader_task.cancel()


@app.get("/api/leap/drilldown")
def api_leap_drilldown(url: str = Query(...)):
    """Server-side proxy for a LEAP contract's drill-down chart image."""
    if not url.startswith("https://app.quotemedia.com/"):
        raise HTTPException(status_code=400, detail="Unsupported drill-down URL.")
    try:
        resp = requests.get(url, headers=get_headers(), timeout=15)
        resp.raise_for_status()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Drill-down fetch failed: {exc}")
    media_type = resp.headers.get("Content-Type", "image/png")
    return Response(content=resp.content, media_type=media_type)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main():
    import uvicorn

    print("Starting option-tracker (Highcharts) on http://0.0.0.0:8050")
    uvicorn.run(app, host="0.0.0.0", port=8050)


if __name__ == "__main__":
    main()

"""Request router for multiple vLLM replicas.

Finds replicas through the headless Service's DNS name, picks one per request
using a routing policy, and streams the replica's response back to the client.
All routing state lives in this one process, so run exactly one worker.
"""

import asyncio
import itertools
import logging
import os
import socket
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

BACKEND_HOST = os.environ.get("BACKEND_HOST", "vllm.default.svc.cluster.local")
BACKEND_PORT = int(os.environ.get("BACKEND_PORT", "8000"))
REFRESH_SECONDS = float(os.environ.get("REFRESH_SECONDS", "2"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("router")


class RoundRobin:
    """Baseline: take turns through the replicas, ignoring the request."""

    def __init__(self):
        self._counter = itertools.count()

    def choose(self, backends: list[str], body: bytes) -> str:
        return backends[next(self._counter) % len(backends)]


policy = RoundRobin()
backends: list[str] = []  # IPs of ready vLLM pods, kept sorted for a stable order
in_flight: dict[str, int] = {}  # requests sent to each backend and not yet finished


async def refresh_backends():
    """Re-resolve the Service name every few seconds so restarted or added pods
    (which come back with new IPs) are picked up and dead ones are dropped."""
    global backends
    loop = asyncio.get_running_loop()
    while True:
        try:
            infos = await loop.getaddrinfo(BACKEND_HOST, BACKEND_PORT, type=socket.SOCK_STREAM)
            found = sorted({info[4][0] for info in infos})
        except socket.gaierror:
            found = []  # no ready pods: the name doesn't resolve at all
        if found != backends:
            log.info("backends changed: %s -> %s", backends, found)
            backends = found
        await asyncio.sleep(REFRESH_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    # No timeout: a long prompt on CPU can take well over a minute.
    client = httpx.AsyncClient(timeout=None, limits=httpx.Limits(max_connections=1000))
    task = asyncio.create_task(refresh_backends())
    yield
    task.cancel()
    await client.aclose()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/stats")
async def stats():
    return {"policy": type(policy).__name__, "backends": backends, "in_flight": in_flight}


@app.post("/v1/{path:path}")
async def proxy(path: str, request: Request):
    body = await request.body()
    if not backends:
        return JSONResponse({"error": "no ready backends"}, status_code=503)

    backend = policy.choose(backends, body)
    upstream = client.build_request(
        "POST",
        f"http://{backend}:{BACKEND_PORT}/v1/{path}",
        content=body,
        headers={"content-type": request.headers.get("content-type", "application/json")},
    )

    in_flight[backend] = in_flight.get(backend, 0) + 1
    try:
        response = await client.send(upstream, stream=True)
    except httpx.HTTPError as e:
        in_flight[backend] -= 1
        return JSONResponse({"error": f"backend {backend} unreachable: {e}"}, status_code=502)

    async def relay():
        # Pass each chunk through as soon as it arrives, so streamed tokens reach
        # the client immediately and time-to-first-token is measured honestly.
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        finally:
            await response.aclose()
            in_flight[backend] -= 1

    return StreamingResponse(
        relay(),
        status_code=response.status_code,
        media_type=response.headers.get("content-type"),
        headers={"x-routed-to": backend},  # lets the load generator see where each request went
    )

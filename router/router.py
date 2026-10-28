"""Request router for multiple vLLM replicas.

Finds replicas through the headless Service's DNS name, picks one per request
using a routing policy, and streams the replica's response back to the client.
All routing state lives in this one process, so run exactly one worker.
"""

import asyncio
import itertools
import json
import logging
import os
import socket
from collections import OrderedDict
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

BACKEND_HOST = os.environ.get("BACKEND_HOST", "vllm.default.svc.cluster.local")
BACKEND_PORT = int(os.environ.get("BACKEND_PORT", "8000"))
REFRESH_SECONDS = float(os.environ.get("REFRESH_SECONDS", "2"))
POLICY = os.environ.get("POLICY", "round_robin")
# Prompts are matched in chunks of this many characters. vLLM caches in blocks of
# 16 tokens (~64 chars), so matching much finer than that gains nothing.
CHUNK_CHARS = int(os.environ.get("CHUNK_CHARS", "128"))
# How many chunks to remember per replica. 1300 x 128 chars ~ 43k tokens, about
# one replica's KV cache, so the table forgets at roughly the rate vLLM evicts.
CHUNKS_PER_BACKEND = int(os.environ.get("CHUNKS_PER_BACKEND", "1300"))
# Hybrid policy: leave the cached replica only if it has at least MIN_GAP more
# in-flight requests than the least-busy one AND at least MIN_RATIO times as many.
# The gap check ignores small-count noise (1 vs 0); the ratio check scales with load.
MIN_GAP = int(os.environ.get("MIN_GAP", "4"))
MIN_RATIO = float(os.environ.get("MIN_RATIO", "1.5"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("router")


def prompt_text(body: bytes) -> str:
    """The prompt text in the order vLLM will see it. For chat requests the messages
    are concatenated, so each new turn's text starts with the previous turn's."""
    try:
        request = json.loads(body)
    except ValueError:
        return ""
    if isinstance(request.get("prompt"), str):
        return request["prompt"]
    return "".join(f"{m.get('role')}:{m.get('content')}\n" for m in request.get("messages", []))


def chunk_hashes(text: str) -> list[int]:
    """Chained hashes of each full chunk: hash i covers all the text up to the end
    of chunk i, so two prompts share hash i only if they share that whole prefix.
    A trailing partial chunk is skipped, like vLLM skips partial blocks."""
    hashes, h = [], 0
    for start in range(0, len(text) - CHUNK_CHARS + 1, CHUNK_CHARS):
        h = hash((h, text[start : start + CHUNK_CHARS]))
        hashes.append(h)
    return hashes


def least_busy(backends: list[str], in_flight: dict[str, int], counter) -> str:
    """Replica with the fewest in-flight requests. Ties rotate, so idle replicas
    share new work instead of the first one in the list getting all of it."""
    start = next(counter) % len(backends)
    rotated = backends[start:] + backends[:start]
    return min(rotated, key=lambda b: in_flight.get(b, 0))


class RoundRobin:
    """Baseline: take turns through the replicas, ignoring the request."""

    def __init__(self):
        self._counter = itertools.count()

    def choose(self, backends: list[str], body: bytes, in_flight: dict[str, int]) -> str:
        return backends[next(self._counter) % len(backends)]

    def forget(self, removed: set[str]):
        pass


class CacheAware:
    """Send each request to the replica that most recently served its longest
    known prefix. Requests with no known prefix go to the least-busy replica."""

    def __init__(self):
        self._counter = itertools.count()
        self.table: OrderedDict[int, str] = OrderedDict()  # chunk hash -> replica, oldest first

    def choose(self, backends: list[str], body: bytes, in_flight: dict[str, int]) -> str:
        hashes = chunk_hashes(prompt_text(body))
        backend = self._longest_match(hashes, backends)
        if backend is None:
            backend = least_busy(backends, in_flight, self._counter)
        self._record(hashes, backend, len(backends))
        return backend

    def _longest_match(self, hashes: list[int], backends: list[str]) -> str | None:
        """Walk the chain until the first unknown chunk. The deepest match wins: that
        replica processed the entire prefix up to that point."""
        backend = None
        for h in hashes:
            owner = self.table.get(h)
            if owner is None or owner not in backends:
                break
            backend = owner
        return backend

    def _record(self, hashes: list[int], backend: str, n_backends: int):
        """The chosen replica will now cache this whole prompt, so point every chunk
        at it, marking them recently used, then drop the least recently used."""
        for h in hashes:
            self.table[h] = backend
            self.table.move_to_end(h)
        while len(self.table) > CHUNKS_PER_BACKEND * n_backends:
            self.table.popitem(last=False)

    def forget(self, removed: set[str]):
        """A replica that disappeared (and any replacement) starts with an empty cache."""
        self.table = OrderedDict((h, b) for h, b in self.table.items() if b not in removed)


class Hybrid(CacheAware):
    """Cache-aware, but abandon the cached replica when it is clearly overloaded
    compared with the least-busy one (see MIN_GAP and MIN_RATIO)."""

    def choose(self, backends: list[str], body: bytes, in_flight: dict[str, int]) -> str:
        hashes = chunk_hashes(prompt_text(body))
        idlest = least_busy(backends, in_flight, self._counter)
        backend = self._longest_match(hashes, backends)
        if backend is None:
            backend = idlest
        else:
            load, idle_load = in_flight.get(backend, 0), in_flight.get(idlest, 0)
            if load - idle_load >= MIN_GAP and load >= MIN_RATIO * idle_load:
                backend = idlest
        # Whichever replica runs the request will cache its prefix, so record that one.
        self._record(hashes, backend, len(backends))
        return backend


POLICIES = {"round_robin": RoundRobin, "cache_aware": CacheAware, "hybrid": Hybrid}
policy = POLICIES[POLICY]()
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
            policy.forget(set(backends) - set(found))
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
    return {
        "policy": type(policy).__name__,
        "backends": backends,
        "in_flight": in_flight,
        "table_chunks": len(getattr(policy, "table", {})),
    }


@app.post("/v1/{path:path}")
async def proxy(path: str, request: Request):
    body = await request.body()
    if not backends:
        return JSONResponse({"error": "no ready backends"}, status_code=503)

    backend = policy.choose(backends, body, in_flight)
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

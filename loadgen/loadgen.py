"""Load generator: simulated chat users sending multi-turn conversations.

New sessions arrive at a fixed average rate (Poisson), independent of how fast
the system responds, so a slow or overloaded replica shows up as growing latency
instead of quietly reducing the load. Inside a session, each turn waits for the
previous reply (plus a pause for the "user" to type) and resends the whole
conversation so far, which is what creates prefix reuse.

Writes per-replica vLLM cache counters for the run as one JSON line after a
"---METRICS---" marker, then one CSV row per request after a "---CSV---" marker,
both to stdout. Progress and a summary go to stderr.
"""

import asyncio
import csv
import json
import os
import random
import re
import socket
import statistics
import sys
import time
import uuid
from collections import Counter

import httpx

ROUTER_URL = os.environ.get("ROUTER_URL", "http://router:8080")
VLLM_HOST = os.environ.get("VLLM_HOST", "vllm.default.svc.cluster.local")
MODEL = os.environ.get("MODEL", "Qwen/Qwen2.5-0.5B-Instruct")

SESSION_RATE = float(os.environ.get("SESSION_RATE", "0.2"))  # new sessions per second
DURATION = float(os.environ.get("DURATION", "120"))  # seconds during which sessions arrive
N_SYSTEM_PROMPTS = int(os.environ.get("N_SYSTEM_PROMPTS", "4"))  # distinct shared prefixes
SYSTEM_PROMPT_CHARS = int(os.environ.get("SYSTEM_PROMPT_CHARS", "3000"))
TURNS = int(os.environ.get("TURNS", "3"))  # turns per session
USER_MSG_CHARS = int(os.environ.get("USER_MSG_CHARS", "200"))
THINK_TIME = float(os.environ.get("THINK_TIME", "2"))  # mean pause between turns, seconds
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "32"))
SEED = int(os.environ.get("SEED", "0"))

WORDS = (
    "account order refund shipping policy customer support agent delivery invoice "
    "payment return warranty product service request issue status update manager "
    "approval days business tracking address billing credit replacement exchange"
).split()


def text(rng: random.Random, chars: int) -> str:
    out = []
    while sum(len(w) + 1 for w in out) < chars:
        out.append(rng.choice(WORDS))
    return " ".join(out)


async def replica_ips() -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(VLLM_HOST, 8000, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


async def warmup(client: httpx.AsyncClient, ips: list[str]):
    """Send one small request straight to every replica: a replica's first request
    is far slower than later ones and would distort the measurements."""
    await asyncio.gather(*(
        client.post(f"http://{ip}:8000/v1/completions",
                    json={"model": MODEL, "prompt": "warmup", "max_tokens": 4})
        for ip in ips
    ))
    log(f"warmed up {len(ips)} replicas: {ips}")


# vLLM counters read before and after the run; the difference is this run's share.
COUNTERS = ["prefix_cache_queries_total", "prefix_cache_hits_total", "prompt_tokens_total"]


async def read_counters(client: httpx.AsyncClient, ips: list[str]) -> dict[str, dict[str, float]]:
    """Each replica's cumulative counters, summed over label sets."""
    async def one(ip):
        text = (await client.get(f"http://{ip}:8000/metrics")).text
        return {
            name: sum(float(v) for v in re.findall(rf"^vllm:{name}(?:{{[^}}]*}})? (\S+)$", text, re.M))
            for name in COUNTERS
        }
    return dict(zip(ips, await asyncio.gather(*(one(ip) for ip in ips))))


def counter_deltas(before: dict, after: dict) -> dict:
    """Per-replica and total cache activity during the run. Query and hit counts
    are in tokens: hits / queries is the fraction of prompt tokens served from cache."""
    per_replica = {
        ip: {name: after[ip][name] - before[ip][name] for name in COUNTERS}
        for ip in before if ip in after
    }
    totals = {name: sum(r[name] for r in per_replica.values()) for name in COUNTERS}
    for stats in [*per_replica.values(), totals]:
        q = stats["prefix_cache_queries_total"]
        stats["hit_rate"] = stats["prefix_cache_hits_total"] / q if q else None
    return {"replicas": per_replica, "total": totals}


async def send(client: httpx.AsyncClient, messages: list[dict]) -> dict:
    """One streamed chat request. Time to first token is measured at the first
    chunk that carries generated text."""
    start = time.perf_counter()
    ttft, reply, routed_to, status = None, [], "", 0
    try:
        async with client.stream(
            "POST", f"{ROUTER_URL}/v1/chat/completions",
            json={"model": MODEL, "messages": messages, "max_tokens": MAX_TOKENS, "stream": True},
        ) as response:
            status, routed_to = response.status_code, response.headers.get("x-routed-to", "")
            async for line in response.aiter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                choices = json.loads(line[6:]).get("choices") or [{}]
                content = choices[0].get("delta", {}).get("content")
                if content:
                    if ttft is None:
                        ttft = time.perf_counter() - start
                    reply.append(content)
    except httpx.HTTPError as e:
        log(f"request failed: {e!r}")
    return {
        "status": status,
        "routed_to": routed_to,
        "ttft": ttft,
        "total": time.perf_counter() - start,
        "reply": "".join(reply),
        "output_chunks": len(reply),
    }


async def session(client, rng, session_id, system_prompts, t0, rows):
    prompt_id = rng.randrange(len(system_prompts))
    messages = [{"role": "system", "content": system_prompts[prompt_id]}]
    for turn in range(TURNS):
        messages.append({"role": "user", "content": text(rng, USER_MSG_CHARS)})
        sent_at = time.perf_counter() - t0
        result = await send(client, messages)
        rows.append({
            "session": session_id,
            "turn": turn,
            "system_prompt": prompt_id,
            "sent_at": round(sent_at, 3),
            "prompt_chars": sum(len(m["content"]) for m in messages),
            "routed_to": result["routed_to"],
            "status": result["status"],
            "ttft": None if result["ttft"] is None else round(result["ttft"], 4),
            "total": round(result["total"], 4),
            "output_chunks": result["output_chunks"],
        })
        messages.append({"role": "assistant", "content": result["reply"]})
        if turn < TURNS - 1:
            await asyncio.sleep(rng.expovariate(1 / THINK_TIME))


async def main():
    rng = random.Random(SEED)
    # A fresh run id in every system prompt, so prefixes cached by earlier runs
    # can't produce hits in this one.
    run_id = uuid.uuid4().hex[:8]
    system_prompts = [f"[run {run_id} prompt {i}] " + text(rng, SYSTEM_PROMPT_CHARS)
                      for i in range(N_SYSTEM_PROMPTS)]
    rows: list[dict] = []
    async with httpx.AsyncClient(timeout=600, limits=httpx.Limits(max_connections=None)) as client:
        ips = await replica_ips()
        await warmup(client, ips)
        before = await read_counters(client, ips)
        t0 = time.perf_counter()
        tasks, session_id = [], 0
        next_arrival = rng.expovariate(SESSION_RATE)
        while next_arrival < DURATION:
            await asyncio.sleep(max(0, next_arrival - (time.perf_counter() - t0)))
            tasks.append(asyncio.create_task(session(
                client, random.Random(rng.random()), session_id, system_prompts, t0, rows)))
            session_id += 1
            next_arrival += rng.expovariate(SESSION_RATE)
        log(f"{session_id} sessions started; waiting for them to finish")
        await asyncio.gather(*tasks)
        # A replica that restarted mid-run has reset counters; it is skipped below
        # if its IP changed, so check the log for "backends changed" if totals look low.
        cache = counter_deltas(before, await read_counters(client, ips))

    summarize(rows)
    log(f"prefix cache hit rate: {cache['total']['hit_rate']:.1%} overall, " + ", ".join(
        f"{ip} {r['hit_rate']:.1%}" for ip, r in cache["replicas"].items() if r["hit_rate"] is not None))
    print("---METRICS---")
    print(json.dumps(cache))
    print("---CSV---")
    writer = csv.DictWriter(sys.stdout, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(sorted(rows, key=lambda r: r["sent_at"]))


def summarize(rows: list[dict]):
    ok = [r for r in rows if r["status"] == 200 and r["ttft"] is not None]
    ttfts = sorted(r["ttft"] for r in ok)
    log(f"requests: {len(rows)} ok: {len(ok)}  per replica: {dict(Counter(r['routed_to'] for r in rows))}")
    if ttfts:
        p95 = ttfts[min(len(ttfts) - 1, int(0.95 * len(ttfts)))]
        log(f"TTFT p50={statistics.median(ttfts):.2f}s p95={p95:.2f}s max={ttfts[-1]:.2f}s")


def log(msg: str):
    print(msg, file=sys.stderr, flush=True)


if __name__ == "__main__":
    asyncio.run(main())

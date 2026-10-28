# vllm-prefix-router

Prefix-cache-aware request routing for multi-replica vLLM on Kubernetes.

> **Status:** in progress. The cluster and vLLM deployment work; the router and benchmarks are being built.

## The problem

vLLM caches the KV state of prompt prefixes it has already processed (automatic prefix caching). When a new request shares a prefix with an earlier one, such as the same system prompt or an earlier turn of the same conversation, vLLM skips recomputing it. On a single replica this is a large win. In a quick test on this setup, a ~2.3k-token prompt took 12.4s cold and 0.32s when its prefix was cached.

With several replicas behind a standard load balancer, that benefit mostly disappears:

- **Repeated misses:** a prefix misses once on *every* replica instead of once overall.
- **Duplicated cache:** each replica caches the same popular prefixes, so the cluster's effective cache capacity is divided by the number of replicas, and prefixes get evicted sooner.

Routing every request to the replica that holds its prefix fixes this, but it creates hotspots: if one prefix is very popular, a pure cache-affinity router sends all of it to one replica while the others sit idle.

## Approach

A router in front of the vLLM replicas that compares three policies:

| Policy | Behavior |
|---|---|
| Round-robin | Baseline. Ignores the request content. |
| Cache-aware | Sends each request to the replica with the longest matching prefix. |
| Hybrid | Cache-aware, but falls back to the least-loaded replica when the preferred one's in-flight request count exceeds the least-loaded one's by more than a threshold. |

All three policies run inside the same router, so measured differences come from the policy rather than from the network path. The questions to answer:

- How much do cache hit rate and time-to-first-token improve over round-robin, and under which traffic patterns (shared system prompts, multi-turn chat, unique prompts)?
- Where does pure cache affinity start to hurt because of load imbalance?
- How should the hybrid threshold be set, and does it depend on the workload?
- How do the gains change with the number of replicas and with cache pressure?

## Setup

Runs locally on [kind](https://kind.sigs.k8s.io/) on an Apple Silicon Mac (CPU only), with Docker Desktop given ~12GB of memory.

| Component | Choice |
|---|---|
| Cluster | kind, single node |
| Inference server | `vllm/vllm-openai-cpu:v0.31.0-arm64` |
| Model | Qwen2.5-0.5B-Instruct |
| Replicas | 2 |

```bash
mkdir -p hf-cache
kind create cluster --config kind-config.yaml
kind load docker-image vllm/vllm-openai-cpu:v0.31.0-arm64 --name vllm
kubectl apply -f vllm-deployment.yaml -f vllm-service.yaml
kubectl rollout status deployment/vllm

# Router
docker build -t vllm-router:dev router/
kind load docker-image vllm-router:dev --name vllm
kubectl apply -f router-deployment.yaml
kubectl port-forward svc/router 8080:8080   # then send OpenAI-style requests to localhost:8080
```

The router adds an `x-routed-to` response header naming the replica that served each request, and `GET /stats` shows the current replicas and in-flight counts.

### Notes on running vLLM on CPU in Kubernetes

These settings were needed to fit two replicas in ~12GB. Each is explained in `vllm-deployment.yaml`.

- **Image version:** `v0.32.0-arm64` crashes on the first request on arm64 CPU, so the image is pinned to `v0.31.0`.
- **Shared memory:** vLLM needs more than the container default of 64MB of `/dev/shm`, so a memory-backed `emptyDir` is mounted there.
- **Thread count:** `OMP_NUM_THREADS` must match the CPU limit. Otherwise vLLM sizes its thread pool to the host's cores and gets throttled by the cgroup CPU quota.
- **No compilation:** `--enforce-eager` skips torch.compile, whose startup memory peak exceeds the per-pod limit.
- **Single process:** `--distributed-executor-backend=uni` runs the model in the engine process rather than a separate worker process, which saves ~1.5GB per pod.
- **Small KV cache:** `--kv-cache-memory-bytes` is set to 512MB (about 43k tokens).
- **`strategy: Recreate`:** a rolling update would briefly need a third pod, which doesn't fit.

Measured memory is ~4.6GiB per pod.

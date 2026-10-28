#!/usr/bin/env bash
# Run one load test against one routing policy and save the per-request CSV.
#
#   ./run-experiment.sh <policy> <name> [VAR=value ...]
#   ./run-experiment.sh cache_aware shared-4 SESSION_RATE=0.3 N_SYSTEM_PROMPTS=4
#
# VAR=value pairs are passed to the load generator (see loadgen/loadgen.py).
# Results go to results/<name>-<policy>.csv.
set -euo pipefail

policy=$1
name=$2
shift 2

# Keep the Mac from idle-sleeping while this script runs; a sleep mid-run stalls
# the cluster and corrupts the latency measurements.
if command -v caffeinate >/dev/null; then
  caffeinate -i -w $$ &
fi

# Restart the router so every run starts with an empty routing table.
kubectl set env deployment/router POLICY="$policy" >/dev/null
kubectl rollout restart deployment/router >/dev/null
kubectl rollout status deployment/router --timeout=120s >/dev/null

env_lines=""
for kv in "$@"; do
  env_lines+="            - {name: \"${kv%%=*}\", value: \"${kv#*=}\"}"$'\n'
done

kubectl delete job loadgen --ignore-not-found >/dev/null
kubectl create -f - >/dev/null <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: loadgen
spec:
  backoffLimit: 0 # a failed run should be looked at, not silently retried
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: loadgen
          image: vllm-loadgen:dev
          imagePullPolicy: Never
          env:
            - {name: SEED, value: "0"}
${env_lines}
EOF

echo "running: policy=$policy $*" >&2
kubectl wait --for=condition=complete job/loadgen --timeout=3600s >/dev/null
mkdir -p results
kubectl logs job/loadgen | sed -n '/^---CSV---$/,$p' | tail -n +2 > "results/$name-$policy.csv"
kubectl logs job/loadgen | sed '/^---CSV---$/,$d' >&2
echo "saved results/$name-$policy.csv" >&2

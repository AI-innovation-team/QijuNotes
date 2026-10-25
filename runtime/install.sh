#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
node_binary="$(python3 "$project_root/runtime/node_runtime.py")"
export PATH="$(dirname -- "$node_binary"):$PATH"
cd "$project_root/runtime/npm"
npm ci --ignore-scripts --no-audit --no-fund
"$node_binary" node_modules/@deepseek-ai/dsh/lib/bin.js --version
DSH_NODE="$node_binary" python3 "$project_root/runtime/node_runtime.py" --remember >/dev/null

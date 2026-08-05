#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-help}"
CLEAN=0

if [[ "$MODE" != "help" ]]; then
  shift
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --clean)
      CLEAN=1
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
  shift
done

usage() {
  cat <<'EOF'
Usage:
  bash scripts/run_skill_e2e.sh foreground-smoke [--clean]
  bash scripts/run_skill_e2e.sh real-foreground [--clean]

Modes:
  foreground-smoke  Deterministic init/finish/complete run in a disposable installed skill repo.
  real-foreground   Prepare a disposable repo and open the real Codex TUI for a human-driven Goal run.

The deterministic mode validates control-plane mechanics and runs in CI. The real mode requires
local Codex authentication and is never represented as mock/model validation.
EOF
}

require_tool() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required tool: $1" >&2
    exit 1
  fi
}

copy_skill() {
  local destination="$1"
  mkdir -p "$destination/references" "$destination/scripts" "$destination/agents"
  cp "$ROOT/SKILL.md" "$destination/SKILL.md"
  cp "$ROOT/references/"*.md "$destination/references/"
  cp \
    "$ROOT/scripts/autoresearch.py" \
    "$ROOT/scripts/autoresearch_core.py" \
    "$ROOT/scripts/autoresearch_report.py" \
    "$ROOT/scripts/autoresearch_state.py" \
    "$destination/scripts/"
  cp "$ROOT/agents/openai.yaml" "$destination/agents/openai.yaml"
}

prepare_repo() {
  local fixture="$1"
  local temporary="$2"
  local repo="$temporary/repo"
  cp -R "$ROOT/tests/e2e-fixtures/$fixture" "$repo"
  find "$repo" -type d -name __pycache__ -prune -exec rm -rf {} +
  find "$repo" -type f -name '*.pyc' -delete
  copy_skill "$repo/.agents/skills/codex-autoresearch"
  git -C "$repo" init -b main >/dev/null
  git -C "$repo" config user.name e2e
  git -C "$repo" config user.email e2e@example.com
  git -C "$repo" add .
  git -C "$repo" commit -m "fixture baseline" >/dev/null
  printf '%s\n' "$repo"
}

cleanup() {
  local temporary="$1"
  if [[ "$CLEAN" -eq 1 ]]; then
    rm -rf "$temporary"
  else
    echo "Demo repository kept at: $temporary/repo"
  fi
}

assert_status() {
  local control="$1"
  local repo="$2"
  local expected="$3"
  python3 - "$control" "$repo" "$expected" <<'PY'
import json
import subprocess
import sys

control, repo, expected = sys.argv[1:]
payload = json.loads(
    subprocess.check_output(
        [sys.executable, control, "status", "--repo", repo],
        text=True,
        encoding="utf-8",
    )
)
if payload["status"] != expected:
    raise SystemExit(f"expected status {expected}, got {payload}")
PY
}

run_foreground_smoke() {
  require_tool python3
  require_tool git
  local temporary repo control
  temporary="$(mktemp -d)"
  repo="$(prepare_repo interactive_unittest_fix "$temporary")"
  control="$repo/.agents/skills/codex-autoresearch/scripts/autoresearch.py"

  python3 "$control" init \
    --repo "$repo" \
    --goal "Reduce the unit-test failure count to zero" \
    --scope src \
    --metric-name failure_count \
    --direction lower \
    --verify "python3 scripts/score.py" \
    --metric-key failure_count \
    --target 0 >/dev/null

  python3 - "$repo/src/math_utils.py" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
old = "return a - b"
if text.count(old) != 1:
    raise SystemExit(f"expected exactly one fixture bug in {path}")
path.write_text(text.replace(old, "return a + b"), encoding="utf-8")
PY
  python3 "$control" finish --repo "$repo" --description "correct integer addition" >/dev/null
  assert_status "$control" "$repo" complete
  python3 -m unittest discover -s "$repo/tests" -q
  echo "foreground smoke: OK"
  cleanup "$temporary"
}

assert_completed_repo() {
  local control="$1"
  local repo="$2"
  local expected_iterations="$3"
  python3 - "$control" "$repo" "$expected_iterations" <<'PY'
import json
import subprocess
import sys
from pathlib import Path

control, repo_text, expected_text = sys.argv[1:]
repo = Path(repo_text)
expected = int(expected_text)
status = json.loads(
    subprocess.check_output(
        [sys.executable, control, "status", "--repo", str(repo)],
        text=True,
        encoding="utf-8",
    )
)
if status["status"] != "complete" or status["metric"]["current"] != status["metric"]["target"]:
    raise SystemExit(f"run did not reach its target: {status}")
if status["iterations"] != expected:
    raise SystemExit(f"expected {expected} iterations, got {status['iterations']}")
events = [
    json.loads(line)
    for line in Path(status["events_path"]).read_text(encoding="utf-8").splitlines()
]
if events[0]["event"] != "baseline" or events[-1]["event"] != "complete":
    raise SystemExit(f"invalid event boundary: {events}")
if sum(event["event"] == "iteration" for event in events) != expected:
    raise SystemExit(f"iteration event count mismatch: {events}")
if any(event["event"] == "iteration" and event["outcome"] != "keep" for event in events):
    raise SystemExit(f"demo unexpectedly retained a discard: {events}")
dirty = subprocess.check_output(
    ["git", "-C", str(repo), "status", "--short"],
    text=True,
    encoding="utf-8",
).strip()
if dirty:
    raise SystemExit(f"demo repository is dirty after completion: {dirty}")
subjects = subprocess.check_output(
    ["git", "-C", str(repo), "log", "--format=%s", f"-{expected + 1}"],
    text=True,
    encoding="utf-8",
)
if subjects.count("autoresearch:") < expected:
    raise SystemExit(f"missing retained autoresearch commits: {subjects}")
PY
}

run_real_foreground() {
  require_tool codex
  require_tool python3
  require_tool git
  local temporary repo control prompt terminal iterations
  temporary="$(mktemp -d)"
  repo="$(prepare_repo interactive_unittest_fix "$temporary")"
  control="$repo/.agents/skills/codex-autoresearch/scripts/autoresearch.py"
  prompt="$(cat "$repo/prompt.txt")"
  echo "Starting real foreground demo in: $repo"
  echo "Submit the skill prompt, confirm foreground, then approve with go."
  if ! TERM="${CODEX_E2E_TERM:-xterm-256color}" codex \
    --dangerously-bypass-approvals-and-sandbox --no-alt-screen -C "$repo" \
    "$prompt"; then
    cleanup "$temporary"
    return 1
  fi
  terminal="$(python3 "$control" status --repo "$repo")"
  if ! iterations="$(python3 -c '
import json, sys
status = json.loads(sys.argv[1])
if status["status"] != "complete" or status["iterations"] < 1:
    raise SystemExit(f"real foreground run did not complete: {status}")
print(status["iterations"])
' "$terminal")"; then
    cleanup "$temporary"
    return 1
  fi
  if ! assert_completed_repo "$control" "$repo" "$iterations"; then
    cleanup "$temporary"
    return 1
  fi
  if ! python3 -m unittest discover -s "$repo/tests" -q; then
    cleanup "$temporary"
    return 1
  fi
  echo "real foreground: OK ($iterations iterations)"
  cleanup "$temporary"
}

case "$MODE" in
  foreground-smoke)
    run_foreground_smoke
    ;;
  real-foreground)
    run_real_foreground
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    echo "Unknown mode: $MODE" >&2
    usage >&2
    exit 2
    ;;
esac

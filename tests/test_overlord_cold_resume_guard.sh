#!/usr/bin/env bash
# Behavioral test for the COLD-RESUME COST GUARD in `overlord` (repo root).
#
# Why a shell scratch harness instead of pytest: the guard lives entirely in
# the bash launcher and its trigger conditions (transcript mtime, tail-parsed
# usage tokens, live --bg roster state) are filesystem/process facts a Python
# unit test can't cheaply fake without re-implementing the bash logic. This
# script builds isolated fake-$HOME sandboxes with fixture .jsonl transcripts
# and a stub `claude` binary, then runs the real `overlord` script against
# each and asserts on its stdout/exit behavior — the same technique used to
# verify this by hand during development.
#
# Never touches Ben's real ~/.claude — every sandbox gets its own throwaway
# $HOME under a mktemp dir, cleaned up on exit.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OVERLORD_SRC="$REPO_ROOT/overlord"
VENV_PY="$REPO_ROOT/.venv/bin/python3"

WORK="$(mktemp -d /tmp/overlord-guard-test.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0

ok()   { PASS=$((PASS+1)); printf '  [ok]   %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  [FAIL] %s\n' "$1"; }

# --- fake `claude` -----------------------------------------------------------
# Handles just enough surface for the launcher to run end to end without a
# real daemon: --version, agents --json (backed by a small state file so a
# --bg standup shows up as live), --bg (background standup), attach, --resume,
# --session-id.
BIN="$WORK/bin"
mkdir -p "$BIN"
cat > "$BIN/claude" <<'FAKE_CLAUDE'
#!/usr/bin/env bash
LOG="${CLAUDE_FAKE_LOG:-/dev/null}"
STATE="${CLAUDE_FAKE_STATE:-/dev/null}"
printf '%s\n' "$*" >> "$LOG"
case "$1" in
    --version) echo "2.1.999 (Claude Code)"; exit 0 ;;
    agents)
        if [[ -s "$STATE" ]]; then cat "$STATE"; else echo "[]"; fi
        exit 0
        ;;
    daemon) exit 1 ;;
    --bg)
        sid="" prev=""
        for a in "$@"; do [[ "$prev" == "--resume" ]] && sid="$a"; prev="$a"; done
        job_id="$(printf '%08x' "$RANDOM")"
        echo "[{\"id\":\"$job_id\",\"kind\":\"background\",\"status\":\"running\",\"sessionId\":\"${sid:-fake-live-sid}\"}]" > "$STATE"
        echo "backgrounded · ${job_id}"
        exit 0
        ;;
    attach) echo "FAKE: attached to $2"; exit 0 ;;
    --resume) echo "FAKE: would resume session $2"; exit 0 ;;
    --session-id) echo "FAKE: would start fresh session $2"; exit 0 ;;
    *) echo "FAKE: claude $*"; exit 0 ;;
esac
FAKE_CLAUDE
chmod +x "$BIN/claude"

# --- sandbox builder ----------------------------------------------------------
# Args: name input_tokens cache_creation cache_read age_secs with_handoff
# Prints: "<root> <sid>"
build_sandbox() {
    local name="$1" input="$2" cc="$3" cr="$4" age="$5" handoff="$6"
    local root="$WORK/$name"
    mkdir -p "$root/home/Projects/overlord-bridge" "$root/home/.cache"
    ln -sfn "$REPO_ROOT/.venv" "$root/home/Projects/overlord-bridge/.venv"
    printf 'OVERLORD_BRAIN=claude\n' > "$root/home/Projects/overlord-bridge/.env"
    chmod 600 "$root/home/Projects/overlord-bridge/.env"
    cp "$OVERLORD_SRC" "$root/home/Projects/overlord-bridge/overlord"
    chmod +x "$root/home/Projects/overlord-bridge/overlord"

    local key
    key="$("$VENV_PY" -c "
from claude_agent_sdk import project_key_for_directory
print(project_key_for_directory('$root/home'))
")"
    local proj="$root/home/.claude/projects/$key"
    mkdir -p "$proj/memory"

    local sid="00000000-1111-2222-3333-$(printf '%012d' "$RANDOM")"
    "$VENV_PY" -c "
import json
with open('$proj/$sid.jsonl', 'w') as fh:
    rec = {'type': 'assistant', 'message': {'usage': {
        'input_tokens': $input,
        'cache_creation_input_tokens': $cc,
        'cache_read_input_tokens': $cr,
    }}}
    fh.write(json.dumps(rec) + '\n')
"
    touch -d "@$(( $(date +%s) - age ))" "$proj/$sid.jsonl"
    printf '%s' "$sid" > "$root/home/Projects/overlord-bridge/.session.claude"

    if [[ "$handoff" == "yes" ]]; then
        printf '## Session checkpoint\ntest handoff body\n' > "$proj/memory/_session_handoff.md"
    fi
    printf '%s %s\n' "$root" "$sid"
}

run_overlord() {
    local root="$1"; shift
    HOME="$root/home" PATH="$BIN:$PATH" \
        CLAUDE_FAKE_LOG="$WORK/claude-calls.log" CLAUDE_FAKE_STATE="$WORK/claude-fake-state.json" \
        bash "$root/home/Projects/overlord-bridge/overlord" "$@" 2>&1
}

# --- scenarios ----------------------------------------------------------------
# COLD threshold default 3300s (55min); BIG threshold default 150000 tokens.

# A: cold (65min) + big (429K) -> guard MUST fire
read -r rootA sidA < <(build_sandbox "cold_big" 100 4000 425000 3900 yes)
outA="$(run_overlord "$rootA" --no-shared)"
if grep -q "Starting fresh from the handoff instead" <<<"$outA" \
    && grep -q "$sidA" <<<"$outA" \
    && [[ -f "$rootA/home/.cache/near-limit-handoff/pending-handoff" ]]; then
    ok "cold+big fires the guard and arms the handoff flag"
else
    bad "cold+big did not fire as expected:"$'\n'"$outA"
fi

# B: warm (5min) + big (429K) -> guard must NOT fire
read -r rootB sidB < <(build_sandbox "warm_big" 100 4000 425000 300 yes)
outB="$(run_overlord "$rootB" --no-shared)"
if ! grep -q "Starting fresh from the handoff instead" <<<"$outB" \
    && grep -q "Resuming shared Overlord session $sidB" <<<"$outB"; then
    ok "warm+big resumes normally (no guard)"
else
    bad "warm+big unexpectedly triggered the guard:"$'\n'"$outB"
fi

# C: cold (65min) + small (41K) -> guard must NOT fire
read -r rootC sidC < <(build_sandbox "cold_small" 100 1000 40000 3900 yes)
outC="$(run_overlord "$rootC" --no-shared)"
if ! grep -q "Starting fresh from the handoff instead" <<<"$outC" \
    && grep -q "Resuming shared Overlord session $sidC" <<<"$outC"; then
    ok "cold+small resumes normally (no guard)"
else
    bad "cold+small unexpectedly triggered the guard:"$'\n'"$outC"
fi

# D: --new must bypass the guard entirely (already fresh)
read -r rootD sidD < <(build_sandbox "new_flag" 100 4000 425000 3900 yes)
outD="$(run_overlord "$rootD" --new)"
if ! grep -q "resuming it would cost" <<<"$outD" \
    && grep -q "Starting a FRESH Overlord session" <<<"$outD"; then
    ok "--new bypasses the guard"
else
    bad "--new interacted with the guard unexpectedly:"$'\n'"$outD"
fi

# E: `resume <sid>` must bypass the guard entirely (explicit ask)
read -r rootE sidE < <(build_sandbox "resume_sid" 100 4000 425000 3900 yes)
outE="$(run_overlord "$rootE" resume "$sidE")"
if ! grep -q "resuming it would cost" <<<"$outE" \
    && grep -q "Resuming Overlord session $sidE" <<<"$outE"; then
    ok "explicit 'resume <sid>' bypasses the guard"
else
    bad "'resume <sid>' interacted with the guard unexpectedly:"$'\n'"$outE"
fi

# F: cold+big but a live shared --bg job already holds the session -> must NOT fire
rootF="$WORK/live_job"
mkdir -p "$rootF/home/Projects/overlord-bridge" "$rootF/home/.cache"
ln -sfn "$REPO_ROOT/.venv" "$rootF/home/Projects/overlord-bridge/.venv"
printf 'OVERLORD_BRAIN=claude\n' > "$rootF/home/Projects/overlord-bridge/.env"
chmod 600 "$rootF/home/Projects/overlord-bridge/.env"
cp "$OVERLORD_SRC" "$rootF/home/Projects/overlord-bridge/overlord"
chmod +x "$rootF/home/Projects/overlord-bridge/overlord"
keyF="$("$VENV_PY" -c "
from claude_agent_sdk import project_key_for_directory
print(project_key_for_directory('$rootF/home'))
")"
projF="$rootF/home/.claude/projects/$keyF"
mkdir -p "$projF"
sidF="00000000-1111-2222-3333-999999999999"
"$VENV_PY" -c "
import json
with open('$projF/$sidF.jsonl', 'w') as fh:
    rec = {'type': 'assistant', 'message': {'usage': {
        'input_tokens': 100, 'cache_creation_input_tokens': 4000, 'cache_read_input_tokens': 425000,
    }}}
    fh.write(json.dumps(rec) + '\n')
"
touch -d "@$(( $(date +%s) - 3900 ))" "$projF/$sidF.jsonl"
printf '%s' "$sidF" > "$rootF/home/Projects/overlord-bridge/.env.placeholder" 2>/dev/null || true
printf '%s' "$sidF" > "$rootF/home/Projects/overlord-bridge/.session.claude"
binF="$rootF/bin"
mkdir -p "$binF"
cat > "$binF/claude" <<FAKE_CLAUDE_F
#!/usr/bin/env bash
case "\$1" in
    --version) echo "2.1.999 (Claude Code)"; exit 0 ;;
    agents) echo '[{"id":"jobabc12","kind":"background","status":"running","sessionId":"$sidF"}]'; exit 0 ;;
    daemon) exit 1 ;;
    *) echo "FAKE: claude \$*"; exit 0 ;;
esac
FAKE_CLAUDE_F
chmod +x "$binF/claude"
outF="$(HOME="$rootF/home" PATH="$binF:$PATH" bash "$rootF/home/Projects/overlord-bridge/overlord" --no-shared 2>&1)"
if ! grep -q "resuming it would cost" <<<"$outF" \
    && grep -q "Resuming shared Overlord session $sidF" <<<"$outF"; then
    ok "cold+big with a live shared job does not fire (would orphan a live job)"
else
    bad "cold+big with a live shared job unexpectedly fired:"$'\n'"$outF"
fi

# G: fail-open on a corrupt/unparseable transcript
rootG="$WORK/corrupt"
mkdir -p "$rootG/home/Projects/overlord-bridge" "$rootG/home/.cache"
ln -sfn "$REPO_ROOT/.venv" "$rootG/home/Projects/overlord-bridge/.venv"
printf 'OVERLORD_BRAIN=claude\n' > "$rootG/home/Projects/overlord-bridge/.env"
chmod 600 "$rootG/home/Projects/overlord-bridge/.env"
cp "$OVERLORD_SRC" "$rootG/home/Projects/overlord-bridge/overlord"
chmod +x "$rootG/home/Projects/overlord-bridge/overlord"
keyG="$("$VENV_PY" -c "
from claude_agent_sdk import project_key_for_directory
print(project_key_for_directory('$rootG/home'))
")"
projG="$rootG/home/.claude/projects/$keyG"
mkdir -p "$projG"
sidG="00000000-1111-2222-3333-888888888888"
printf 'not valid json at all\n{also not json}\n' > "$projG/$sidG.jsonl"
touch -d "@$(( $(date +%s) - 3900 ))" "$projG/$sidG.jsonl"
printf '%s' "$sidG" > "$rootG/home/Projects/overlord-bridge/.session.claude"
set +e
outG="$(HOME="$rootG/home" PATH="$BIN:$PATH" bash "$rootG/home/Projects/overlord-bridge/overlord" --no-shared 2>&1)"
rcG=$?
set -e
if [[ $rcG -eq 0 ]] && ! grep -q "resuming it would cost" <<<"$outG" \
    && grep -q "Resuming shared Overlord session $sidG" <<<"$outG"; then
    ok "corrupt/unparseable transcript fails open (resumes normally, exit 0)"
else
    bad "corrupt transcript did not fail open (exit=$rcG):"$'\n'"$outG"
fi

echo
echo "passed: $PASS  failed: $FAIL"
[[ "$FAIL" -eq 0 ]]

#!/usr/bin/env bash
# tools/lane/cross_family_call.sh -- cross-family headless call helper
# (harmonic-forge#366). Any of Claude/Codex/Gemini invokes one or two
# sibling CLIs headlessly with a cold, self-contained brief and gets back
# one normalized JSON-lines envelope per family on stdout.
#
# Read ADR-007's "Cross-family headless calls" section before changing a
# posture mapping. Two rules are load-bearing and must never move to a
# call site:
#
#   1. Family order is fixed by the caller (never selectable, Gemini never
#      second) -- see `order=(...)` below.
#   2. `read-only` posture's Gemini boundary is an admin-tier deny policy
#      (`gemini-read-only-deny.toml`, sibling of this file), not
#      `--approval-mode plan`. Plan mode was the original design; live
#      reproduction on #366 showed the model can still call write_file and
#      narrate a false success while doing so. `--admin-policy` removes
#      denied tools from the model's tool list entirely (proven live,
#      #326) -- that removal, not the model's own report text, is what
#      TC2/TC6 verify.
#
# `probe` posture is the only posture that ever passes Gemini `--yolo`; it
# requires `--cwd` naming an isolated scratch directory the caller is
# responsible for creating and removing (never this repo's own worktree).
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: cross_family_call.sh --caller claude|codex|gemini --families 2|3 \
         --posture read-only|probe --brief PATH [--cwd PATH]

  --caller    which CLI is making this call (determines target order)
  --families  2 (caller's primary sibling only) or 3 (both siblings)
  --posture   read-only (centralized deny boundary) or probe (--yolo,
              requires --cwd)
  --brief     path to a self-contained cold-brief file (no memories, no
              conversation -- see ADR-007)
  --cwd       isolated scratch directory; required for probe posture
EOF
  exit 2
}

caller="" families="" posture="" brief="" cwd=""
while [ $# -gt 0 ]; do
  case "$1" in
    --caller) caller="${2:-}"; shift 2 ;;
    --families) families="${2:-}"; shift 2 ;;
    --posture) posture="${2:-}"; shift 2 ;;
    --brief) brief="${2:-}"; shift 2 ;;
    --cwd) cwd="${2:-}"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "cross_family_call: unrecognized argument: $1" >&2; usage ;;
  esac
done

case "$caller" in
  claude|codex|gemini) ;;
  *) echo "cross_family_call: --caller must be claude, codex, or gemini" >&2; exit 2 ;;
esac
case "$families" in
  2|3) ;;
  *) echo "cross_family_call: --families must be 2 or 3" >&2; exit 2 ;;
esac
case "$posture" in
  read-only|probe) ;;
  *) echo "cross_family_call: --posture must be read-only or probe" >&2; exit 2 ;;
esac
if [ -z "$brief" ] || [ ! -f "$brief" ]; then
  echo "cross_family_call: --brief PATH must name an existing file" >&2
  exit 2
fi
if [ "$posture" = probe ]; then
  if [ -z "$cwd" ] || [ ! -d "$cwd" ]; then
    echo "cross_family_call: --cwd PATH is required and must be an existing directory for probe posture" >&2
    exit 2
  fi
fi

# Locked caller-order table (harmonic-forge#366, operator 2026-08-22).
# Never selectable by the caller; Gemini is never in the second slot.
case "$caller" in
  claude) order=(codex gemini) ;;
  codex)  order=(claude gemini) ;;
  gemini) order=(claude codex) ;;
esac
targets=("${order[0]}")
if [ "$families" = 3 ]; then
  targets+=("${order[1]}")
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly_policy="$script_dir/gemini-read-only-deny.toml"

# --- per-family invocation, native stdout on fd 1, native stderr discarded ---

invoke_claude() {
  local brief="$1" cwd="$2"
  (
    if [ -n "$cwd" ]; then cd "$cwd"; fi
    claude -p "$(cat "$brief")" --output-format json </dev/null 2>/dev/null
  )
}

invoke_codex() {
  local posture="$1" brief="$2" cwd="$3"
  local sandbox="read-only"
  local cd_args=()
  if [ "$posture" = probe ]; then
    sandbox="workspace-write"
    [ -n "$cwd" ] && cd_args=(-C "$cwd")
  fi
  codex exec "${cd_args[@]}" --sandbox "$sandbox" --json "$(cat "$brief")" </dev/null 2>/dev/null
}

invoke_gemini() {
  local posture="$1" brief="$2" cwd="$3"
  local mode_args=()
  if [ "$posture" = probe ]; then
    mode_args=(--yolo)
  else
    mode_args=(--admin-policy "$readonly_policy")
  fi
  (
    if [ -n "$cwd" ]; then cd "$cwd"; fi
    env -u GOOGLE_API_KEY -u GEMINI_API_KEY \
      "GOOGLE_CLOUD_PROJECT=${GOOGLE_CLOUD_PROJECT:-hrse-497421}" \
      GIT_PAGER=cat GH_PAGER=cat PAGER=cat GIT_EDITOR=true \
      gemini --skip-trust "${mode_args[@]}" -m gemini-2.5-pro \
        -p "$(cat "$brief")" -o json </dev/null 2>/dev/null
  )
}

# --- normalization: native output -> {family,posture,status,exit_code,report,native} ---

emit_envelope() {
  local family="$1" posture="$2" exit_code="$3" native_file="$4"

  if [ "$exit_code" -ne 0 ] || [ ! -s "$native_file" ]; then
    jq -n --arg family "$family" --arg posture "$posture" --argjson exit_code "$exit_code" \
      '{family:$family, posture:$posture, status:"process-error", exit_code:$exit_code, report:null, native:null}'
    return
  fi

  local native_json text
  case "$family" in
    claude)
      native_json="$(cat "$native_file")"
      text="$(jq -r '.result // empty' "$native_file" 2>/dev/null || true)"
      ;;
    codex)
      native_json="$(jq -s '.' "$native_file" 2>/dev/null || echo 'null')"
      text="$(jq -rs '[.[] | select(.type=="item.completed" and .item.type=="agent_message")] | last | .item.text // empty' "$native_file" 2>/dev/null || true)"
      ;;
    gemini)
      native_json="$(cat "$native_file")"
      text="$(jq -r '.response // empty' "$native_file" 2>/dev/null || true)"
      ;;
  esac

  # Strip a markdown fence some families wrap their JSON reply in
  # (```json ... ``` or plain ``` ... ```) before parsing.
  local unfenced
  unfenced="$(printf '%s' "$text" | sed -e '/^```/d')"

  local report_json status
  if [ -n "$unfenced" ] && report_json="$(printf '%s' "$unfenced" | jq -c '.' 2>/dev/null)" \
     && printf '%s' "$report_json" | jq -e 'type == "object" and (.findings | type == "array")' >/dev/null 2>&1; then
    status="ok"
  else
    report_json="null"
    status="invalid-report"
  fi

  jq -n --arg family "$family" --arg posture "$posture" --arg status "$status" --argjson exit_code "$exit_code" \
    --argjson report "$report_json" --argjson native "$native_json" \
    '{family:$family, posture:$posture, status:$status, exit_code:$exit_code, report:$report, native:$native}'
}

# --- dispatch ---

for family in "${targets[@]}"; do
  tmp_out="$(mktemp)"
  exit_code=0
  case "$family" in
    claude) invoke_claude "$brief" "$cwd" >"$tmp_out" || exit_code=$? ;;
    codex)  invoke_codex "$posture" "$brief" "$cwd" >"$tmp_out" || exit_code=$? ;;
    gemini) invoke_gemini "$posture" "$brief" "$cwd" >"$tmp_out" || exit_code=$? ;;
  esac
  emit_envelope "$family" "$posture" "$exit_code" "$tmp_out"
  rm -f "$tmp_out"
done

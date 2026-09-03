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
#
# `verify` posture (harmonic-forge#448) is Codex-only and deterministic:
# `--ignore-user-config` means the reviewer inherits NOTHING from
# `~/.codex/config.toml` -- no ambient model, no trust levels, and critically
# no `[mcp_servers.*]` (the live user config grants Gmail/Drive/Docs/Sheets/
# Slides through `workspace-mcp`, entirely unconfined by `--sandbox`).
# Removing that grant by construction, rather than by asking the reviewer not
# to use it, is the point of the posture. See the "verify posture -- what is
# and is not gated" note above `invoke_codex` before changing any flag here.
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: cross_family_call.sh --caller claude|codex|gemini --families 2|3 \
         --posture read-only|probe|verify --brief PATH [--cwd PATH]

  --caller    which CLI is making this call (determines target order)
  --families  2 (caller's primary sibling only) or 3 (both siblings)
  --posture   read-only (centralized deny boundary), probe (--yolo,
              requires --cwd), or verify (Codex-only, --ignore-user-config,
              read-only sandbox, requires --cwd)
  --brief     path to a self-contained cold-brief file (no memories, no
              conversation -- see ADR-007)
  --cwd       isolated scratch directory; required for probe and verify
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
  read-only|probe|verify) ;;
  *) echo "cross_family_call: --posture must be read-only, probe, or verify" >&2; exit 2 ;;
esac
if [ -z "$brief" ] || [ ! -f "$brief" ]; then
  echo "cross_family_call: --brief PATH must name an existing file" >&2
  exit 2
fi
if [ "$posture" = probe ] || [ "$posture" = verify ]; then
  if [ -z "$cwd" ] || [ ! -d "$cwd" ]; then
    echo "cross_family_call: --cwd PATH is required and must be an existing directory for $posture posture" >&2
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

# `verify` is Codex-only, and this is enforced on the RESOLVED TARGET LIST
# rather than on `--caller`/`--families` separately (harmonic-forge#448).
# `invoke_claude` takes no posture argument at all and `invoke_gemini` maps
# every non-`probe` posture to its read-only admin policy, so an unguarded
# `verify` would not fail -- it would silently run one of those two under a
# posture whose guarantees (`--ignore-user-config`, no MCP grant, pinned
# model) exist only in the Codex branch. Silently ignoring an unimplementable
# security posture is the failure mode worth being loud about, so this exits
# non-zero and names the offending family.
if [ "$posture" = verify ]; then
  for family in "${targets[@]}"; do
    if [ "$family" != codex ]; then
      echo "cross_family_call: --posture verify is Codex-only; --caller $caller --families $families resolves to target '$family'" >&2
      exit 2
    fi
  done
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly_policy="$script_dir/gemini-read-only-deny.toml"

# Pinned here, not inherited from `~/.codex/config.toml` -- `verify` passes
# `--ignore-user-config`, so without an explicit `-m` the reviewer would fall
# back to a packaged default rather than the model this posture was validated
# against. Overridable for a deliberate experiment; never left to ambient
# config (harmonic-forge#448).
VERIFY_MODEL="${CROSS_FAMILY_VERIFY_MODEL:-gpt-5.6-sol}"

# Appended to every brief, for every family, regardless of what the caller
# wrote (harmonic-forge#366 correction: a Codex probe brief with no explicit
# reply-shape instruction produced a fully correct plain-prose answer that
# emit_envelope's codex branch then classified invalid-report, because
# nothing told Codex to answer in the shared {summary,findings} contract.
# Baking this into the helper -- not into each brief -- means every caller
# gets a working contract even if it never thinks to ask for one).
read -r -d '' REPORT_CONTRACT <<'EOF' || true

---
Reply with ONLY a single JSON object as your final message, no prose before
or after, no markdown code fence, matching exactly this shape:
{"summary": "<one sentence>", "findings": [{"claim": "<what is wrong>", "evidence": "<the exact quoted text that proves it>"}]}
If you find nothing, reply {"summary": "no defect found", "findings": []}.
EOF

# `verify` posture's extension to the contract above (harmonic-forge#448).
#
# The base contract has nowhere to say "I checked assumption 3 and it is
# false" -- a finding carries a claim and evidence, but no verdict and no tie
# back to the specific asserted assumption under review. That is the whole
# product of a verify pass, so it gets its own key rather than being encoded
# in prose inside `summary`.
#
# The three verdict tokens are closed, and `uncheckable` is load-bearing: a
# reviewer that cannot reach the evidence must say so rather than reason from
# the brief's own text and return `confirmed`. That failure -- well-argued
# prose that reads convincingly and is false -- is the exact thing this issue
# exists to catch, so an assumption asserted `confirmed`/`refuted` with no
# executed evidence is normalized DOWN to `uncheckable` by `emit_envelope`.
# The model cannot talk its way past that check, because the check is on the
# presence of the evidence field, not on the persuasiveness of the argument.
read -r -d '' VERIFY_CONTRACT <<'EOF' || true

Additionally, the object MUST carry an "assumptions" key: one entry per
asserted assumption listed in the brief, in the same order, shaped
{"assumption": "<restated in your own words>", "verdict": "confirmed"|"refuted"|"uncheckable", "evidence": "<the exact output of a command you actually ran, or the exact quoted file text you actually read>"}
Rules for "verdict":
  confirmed  - you executed something that proves it true.
  refuted    - you executed something that proves it false.
  uncheckable - you could not reach the evidence from here.
"evidence" must be output you actually obtained. Do NOT reason from the
brief's own text and report "confirmed" -- if you did not run or read
something, the verdict is "uncheckable". A confirmed/refuted verdict with an
empty "evidence" will be discarded and recorded as "uncheckable".
EOF

prompt_text() {
  local posture="$1" brief="$2"
  if [ "$posture" = verify ]; then
    printf '%s%s%s' "$(cat "$brief")" "$REPORT_CONTRACT" "$VERIFY_CONTRACT"
  else
    printf '%s%s' "$(cat "$brief")" "$REPORT_CONTRACT"
  fi
}

# --- per-family invocation, native stdout on fd 1, native stderr discarded ---

# Takes `posture` only to thread it into `prompt_text`; it deliberately does
# NOT branch on it. Claude has no posture-specific invocation here, and
# `verify` can never reach this function -- the target-list guard above exits
# non-zero first. Threading the real value (rather than hardcoding a
# placeholder) keeps that guarantee checkable instead of assumed.
invoke_claude() {
  local posture="$1" brief="$2" cwd="$3"
  (
    if [ -n "$cwd" ]; then cd "$cwd"; fi
    claude -p "$(prompt_text "$posture" "$brief")" --output-format json </dev/null 2>/dev/null
  )
}

# --- verify posture: what is and is not gated (harmonic-forge#448) ---
#
# GATED BY CONSTRUCTION, verified live at implementation time (Codex v0.152.0):
#
#   * `--ignore-user-config` does not load `$CODEX_HOME/config.toml`, which is
#     where `[mcp_servers.*]` lives. The reviewer therefore has no
#     Gmail/Drive/Docs/Sheets/Slides tools at all -- not "is asked not to use
#     them". Auth still resolves via `CODEX_HOME` (the flag's own help text),
#     so this needs no credential copying.
#   * `-m` pins the model instead of inheriting whatever `config.toml` names.
#   * `--sandbox read-only` blocks filesystem writes.
#   * Dropping `config.toml` also drops its `[projects."<path>"] trust_level`
#     records, so trust is re-added explicitly for exactly this one cwd. Live:
#     without it, in a non-git directory, Codex refuses to start at all
#     ("Not inside a trusted directory and --skip-git-repo-check was not
#     specified"). Passing the key for the one directory we are about to hand
#     it keeps the posture deterministic rather than dependent on the target
#     happening to be a git repo.
#
# NOT GATED -- stated plainly rather than assumed away:
#
#   * `gh` mutations. Codex hooks DO NOT FIRE under `--ignore-user-config`:
#     tested live in both discovery forms (a project-level `.codex/hooks.json`
#     and an inline `-c hooks={...}` table), with a `.*` matcher returning a
#     PreToolUse deny. Both parse -- `--strict-config` accepts the inline
#     `hooks` key and rejects unknown keys, so the shape is right -- and
#     neither executed: the probe's shell command ran unblocked in both runs.
#     The only flag that would change this is
#     `--dangerously-bypass-hook-trust`, which is not shippable in a security
#     control. So the reviewer's gh-mutation boundary is currently PROSE ONLY
#     (the brief tells it not to), and this posture must not be described as
#     gating writes to GitHub. See this issue's Ambiguity Gate.
invoke_codex() {
  local posture="$1" brief="$2" cwd="$3"
  local sandbox="read-only"
  local cd_args=() config_args=() model_args=()
  if [ "$posture" = probe ]; then
    sandbox="workspace-write"
    [ -n "$cwd" ] && cd_args=(-C "$cwd")
  elif [ "$posture" = verify ]; then
    cd_args=(-C "$cwd")
    model_args=(--ignore-user-config -m "$VERIFY_MODEL")
    config_args=(-c "projects.\"$cwd\".trust_level=\"trusted\"")
  fi
  codex exec "${cd_args[@]}" "${model_args[@]}" "${config_args[@]}" \
    --sandbox "$sandbox" --json "$(prompt_text "$posture" "$brief")" </dev/null 2>/dev/null
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
        -p "$(prompt_text "$posture" "$brief")" -o json </dev/null 2>/dev/null
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

  # `verify`'s per-assumption verdicts (harmonic-forge#448). Enforced here in
  # the helper, not left to the caller, for the same reason REPORT_CONTRACT is
  # appended here: a guarantee every consumer has to remember to re-apply is
  # not a guarantee. Three normalizations, all of them downgrades:
  #
  #   * a `confirmed`/`refuted` verdict whose `evidence` is missing, empty or
  #     whitespace becomes `uncheckable` -- the model asserted a check it did
  #     not show, which is precisely the confabulation this issue targets;
  #   * any verdict token outside the closed set becomes `uncheckable`;
  #   * a missing or non-array `assumptions` key under `verify` makes the
  #     whole report `invalid-report`, because a verify pass that returned no
  #     verdicts produced nothing.
  #
  # Nothing here can upgrade a verdict, so a malformed report can only ever
  # come out weaker than the model claimed, never stronger.
  if [ "$posture" = verify ] && [ "$status" = "ok" ]; then
    if printf '%s' "$report_json" | jq -e '.assumptions | type == "array"' >/dev/null 2>&1; then
      report_json="$(printf '%s' "$report_json" | jq -c '
        .assumptions = [
          .assumptions[]
          | .evidence = (.evidence // "")
          | .verdict = (
              if (.verdict | IN("confirmed", "refuted", "uncheckable")) | not then "uncheckable"
              elif (.verdict != "uncheckable") and ((.evidence | gsub("^\\s+|\\s+$"; "")) == "") then "uncheckable"
              else .verdict end
            )
        ]')"
    else
      report_json="null"
      status="invalid-report"
    fi
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
    claude) invoke_claude "$posture" "$brief" "$cwd" >"$tmp_out" || exit_code=$? ;;
    codex)  invoke_codex "$posture" "$brief" "$cwd" >"$tmp_out" || exit_code=$? ;;
    gemini) invoke_gemini "$posture" "$brief" "$cwd" >"$tmp_out" || exit_code=$? ;;
  esac
  emit_envelope "$family" "$posture" "$exit_code" "$tmp_out"
  rm -f "$tmp_out"
done

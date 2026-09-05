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
# It also carries the reviewer's read-only instruction, and that placement is
# load-bearing rather than tidy. Codex hooks DO NOT FIRE under
# `--ignore-user-config` (verified live -- see the note above `invoke_codex`),
# so after the operator's 2026-09-03 decision to ship that way, this prose IS
# the reviewer's entire gh-mutation boundary. A boundary that is prose only
# must at minimum exist and be applied unconditionally, so it lives in the
# helper-appended contract rather than in any individual brief -- a caller
# that forgets to write it still gets it. It is a real control in the weak
# sense that the model usually complies, and no control at all against a model
# that does not; do not describe it as gating writes.
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

You are a READ-ONLY reviewer. Do not mutate anything, on GitHub or on disk.
Specifically: no `gh issue close`, `gh pr merge`, `gh issue comment`, `gh api`
with a write method, no commits, pushes, branch or label changes, and no file
writes. Read commands (`gh issue view`, `gh api` GET, `git log/show/diff`,
reading files) are exactly what you are here to run -- use them freely. If
answering an assumption would require a mutation, the verdict is
"uncheckable"; say so rather than performing it. Report back; you are not the
actor.
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
    # harmonic-forge#462: `~/.gemini/settings.json` pins
    # `selectedType: "oauth-personal"` (the operator's interactive login),
    # and Gemini honors that pin -- and any cached
    # `~/.gemini/oauth_creds.json` -- over a `GEMINI_API_KEY` env var even
    # when one is present. oauth-personal is also the auth tier Google has
    # discontinued for this client (`IneligibleTierError`), so every
    # invocation using the real HOME dies there before `GEMINI_API_KEY` is
    # ever consulted. A throwaway HOME carrying only
    # `selectedType: "gemini-api-key"` is the only way to force the key
    # path without touching the operator's real global settings --
    # confirmed live: without it, `API key not valid` is never reached at
    # all, the process dies on the auth-tier error first. Scoped to this
    # subshell's own trap so it never leaks into a caller that also
    # invokes Codex or Claude in the same `cross_family_call.sh` run.
    #
    # Two follow-on findings from preclose-inspection on this same issue,
    # both against Gemini's OWN config-root resolution
    # (`homedir()` in the installed CLI bundle), which checks
    # `GEMINI_CLI_HOME` before falling back to `$HOME`:
    #
    #   1. If the caller's environment exports `GEMINI_CLI_HOME`, `env
    #      HOME=...` alone does not override it -- Gemini resolves back to
    #      the operator's real `~/.gemini/`, silently defeating this whole
    #      mechanism. `GEMINI_CLI_HOME` is unset explicitly so the
    #      throwaway `HOME` is the only candidate `homedir()` can return.
    #   2. Gemini's dotenv fallback (`<homedir>/.gemini/.env`, then
    #      `<homedir>/.env`) resolves via the same `homedir()` -- so once a
    #      working key exists, an operator who supplies it via
    #      `~/.gemini/.env` (the vendor-documented location that keeps the
    #      secret out of the process environment, rather than exporting it)
    #      would find it invisible here, a silent regression versus the
    #      pre-fix behavior. `GEMINI_API_KEY`/`GOOGLE_API_KEY` are passed
    #      through explicitly from the real environment (if set) precisely
    #      so the exported-var path keeps working even though the dotenv
    #      path cannot be reached from an isolated HOME.
    gemini_authtype_home="$(mktemp -d)"
    trap 'rm -rf "$gemini_authtype_home"' EXIT
    mkdir -p "$gemini_authtype_home/.gemini"
    cat >"$gemini_authtype_home/.gemini/settings.json" <<'SETTINGS'
{"security":{"auth":{"selectedType":"gemini-api-key"}}}
SETTINGS
    # If the operator supplies the key via Gemini's documented dotenv path
    # rather than exporting it, that file lives under the REAL home and
    # would otherwise be invisible from the throwaway one -- copy it in
    # (not symlink: keeps the throwaway dir self-contained after the real
    # HOME's file changes mid-run).
    if [ -f "$HOME/.gemini/.env" ]; then
      cp "$HOME/.gemini/.env" "$gemini_authtype_home/.gemini/.env"
    fi
    env -u GEMINI_CLI_HOME \
      "HOME=$gemini_authtype_home" \
      ${GEMINI_API_KEY:+"GEMINI_API_KEY=$GEMINI_API_KEY"} \
      ${GOOGLE_API_KEY:+"GOOGLE_API_KEY=$GOOGLE_API_KEY"} \
      "GOOGLE_CLOUD_PROJECT=${GOOGLE_CLOUD_PROJECT:-hrse-497421}" \
      GIT_PAGER=cat GH_PAGER=cat PAGER=cat GIT_EDITOR=true \
      gemini --skip-trust "${mode_args[@]}" -m gemini-2.5-pro \
        -p "$(prompt_text "$posture" "$brief")" -o json </dev/null 2>/dev/null
  )
}

# harmonic-forge#467: the minimum escaping the failure envelope needs, so it
# can be built without `jq` -- see the dispatch loop for why depending on `jq`
# there would defeat the fix. Backslash first, then quote, or the quote's own
# escape gets re-escaped. A literal newline in a `mktemp` path would still
# break the JSON; that is accepted rather than handled, because `mktemp` and
# `TMPDIR` do not produce one and a full escaper here would be more code than
# the failure path deserves.
json_escape() {
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}


# --- normalization: native output -> {family,posture,status,exit_code,report,native} ---

emit_envelope() {
  local family="$1" posture="$2" exit_code="$3" native_file="$4"

  if [ "$exit_code" -ne 0 ] || [ ! -s "$native_file" ]; then
    jq -n --arg family "$family" --arg posture "$posture" --argjson exit_code "$exit_code" \
      '{family:$family, posture:$posture, status:"process-error", exit_code:$exit_code, report:null, native:null}'
    return
  fi

  # harmonic-forge#466: every payload below stays in a FILE and is never
  # assigned to a shell variable that later becomes an argv element. Linux
  # caps a *single* argument at MAX_ARG_STRLEN (32 pages = 131,072 bytes),
  # independently of ARG_MAX (2,097,152 here) -- so the total-argument budget
  # was never the constraint and raising it would not have helped. A
  # `read-only` review of a real codebase produces a native stream far past
  # 128 KB because the stream carries every tool call and reasoning item, not
  # just the final message: measured at 754,034 bytes on 2026-09-04, 5.75x the
  # cap. The helper broke precisely when it was doing its job.
  local native_norm text_file unfenced_file report_file report_tmp
  native_norm="$(mktemp)"; text_file="$(mktemp)"
  unfenced_file="$(mktemp)"; report_file="$(mktemp)"; report_tmp="$(mktemp)"
  # Cleanup is local to this function and restores nothing, because
  # `emit_envelope` is never called from a trap-bearing subshell -- the only
  # existing trap belongs to the Gemini home dir (line 314) and is scoped to
  # its own subshell.
  trap 'rm -f "$native_norm" "$text_file" "$unfenced_file" "$report_file" "$report_tmp"' RETURN

  # One normalized native VALUE per family, written to disk. `jq -s '.[0]'`
  # for the single-object families is what `cat` used to produce; `jq -s '.'`
  # for Codex's JSONL is what its own `-s` already produced. Both keep the
  # `|| null` fallback the Codex branch had, extended to the other two: a
  # malformed stream now yields `native: null` rather than aborting the
  # envelope, which is strictly better than the old behaviour on that path.
  case "$family" in
    claude)
      jq -s '.[0]' "$native_file" >"$native_norm" 2>/dev/null || printf 'null\n' >"$native_norm"
      jq -r '.result // empty' "$native_file" >"$text_file" 2>/dev/null || : >"$text_file"
      ;;
    codex)
      jq -s '.' "$native_file" >"$native_norm" 2>/dev/null || printf 'null\n' >"$native_norm"
      jq -rs '[.[] | select(.type=="item.completed" and .item.type=="agent_message")] | last | .item.text // empty' "$native_file" >"$text_file" 2>/dev/null || : >"$text_file"
      ;;
    gemini)
      jq -s '.[0]' "$native_file" >"$native_norm" 2>/dev/null || printf 'null\n' >"$native_norm"
      jq -r '.response // empty' "$native_file" >"$text_file" 2>/dev/null || : >"$text_file"
      ;;
  esac

  # Strip a markdown fence some families wrap their JSON reply in
  # (```json ... ``` or plain ``` ... ```) before parsing. `sed` reads the
  # file rather than a `printf "$text"` pipeline: `printf` is a builtin and so
  # was never itself at risk, but the AC is that no unbounded value is an argv
  # element anywhere, and a grep cannot tell a builtin from an exec.
  sed -e '/^```/d' "$text_file" >"$unfenced_file"

  local status
  if [ -s "$unfenced_file" ] && jq -c '.' "$unfenced_file" >"$report_file" 2>/dev/null \
     && jq -e 'type == "object" and (.findings | type == "array")' "$report_file" >/dev/null 2>&1; then
    status="ok"
  else
    printf 'null\n' >"$report_file"
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
  # The jq PROGRAM below is unchanged -- only its input moved from argv to a
  # file. These three downgrades are load-bearing (harmonic-forge#448) and
  # nothing here may weaken them.
  if [ "$posture" = verify ] && [ "$status" = "ok" ]; then
    if jq -e '.assumptions | type == "array"' "$report_file" >/dev/null 2>&1; then
      jq -c '
        .assumptions = [
          .assumptions[]
          | .evidence = (.evidence // "")
          | .verdict = (
              if (.verdict | IN("confirmed", "refuted", "uncheckable")) | not then "uncheckable"
              elif (.verdict != "uncheckable") and ((.evidence | gsub("^\\s+|\\s+$"; "")) == "") then "uncheckable"
              else .verdict end
            )
        ]' "$report_file" >"$report_tmp" && mv "$report_tmp" "$report_file"
    else
      printf 'null\n' >"$report_file"
      status="invalid-report"
    fi
  fi

  # `--slurpfile` reads a file into an ARRAY of the JSON values it holds, so
  # each is indexed back out with `[0]`. Both files are guaranteed to hold
  # exactly one valid JSON value by construction above -- including the
  # literal `null` on every failure path -- so neither index can be missing.
  # The four remaining `--arg`/`--argjson` values are bounded scalars.
  jq -n --arg family "$family" --arg posture "$posture" --arg status "$status" --argjson exit_code "$exit_code" \
    --slurpfile report "$report_file" --slurpfile native "$native_norm" \
    '{family:$family, posture:$posture, status:$status, exit_code:$exit_code, report:$report[0], native:$native[0]}'
}

# --- dispatch ---
#
# harmonic-forge#467: a formatting failure must never destroy a verdict that
# was successfully produced, and must never look like a clean pass.
#
# **What actually swallows the status, corrected from the handoff.** The
# handoff assumed command substitution in this loop absorbed it. There is no
# command substitution here -- `emit_envelope` writes straight to stdout --
# and `set -e` propagates a failing envelope step out of the script correctly:
# forcing the E2BIG failure against the pre-fix script exits **126**, not 0.
# The exit 0 the incident recorded was therefore introduced OUTSIDE this
# script, by whatever the caller wrapped the invocation in; a pipe is the
# usual culprit, since a pipeline reports its last command's status unless the
# caller also sets `pipefail`.
#
# That makes the exit code the half of this fix a caller can most easily
# discard, and the stderr diagnostic and the preserved-output path the halves
# that survive it. All three are implemented; only the last two are robust to
# a caller that drops the status.
#
# The loop also no longer dies on the first bad family. Aborting mid-loop was
# its own defect: with `--families 3` the remaining target never ran, the
# earlier one's envelope had already been printed, and nothing in the output
# said a family was missing.
overall_status=0
preserve_dir="${CROSS_FAMILY_PRESERVE_DIR:-${TMPDIR:-/tmp}}"

for family in "${targets[@]}"; do
  tmp_out="$(mktemp)"
  exit_code=0
  case "$family" in
    claude) invoke_claude "$posture" "$brief" "$cwd" >"$tmp_out" || exit_code=$? ;;
    codex)  invoke_codex "$posture" "$brief" "$cwd" >"$tmp_out" || exit_code=$? ;;
    gemini) invoke_gemini "$posture" "$brief" "$cwd" >"$tmp_out" || exit_code=$? ;;
  esac

  # Buffered, not streamed: a half-written envelope emitted before the failure
  # would be worse than none, because it parses as truncated JSON rather than
  # failing outright. `if cmd; then` is also what suspends `set -e` for this
  # one call so the failure can be handled here instead of killing the run.
  envelope_out="$(mktemp)"
  envelope_err="$(mktemp)"
  if emit_envelope "$family" "$posture" "$exit_code" "$tmp_out" \
       >"$envelope_out" 2>"$envelope_err"; then
    cat "$envelope_out"
    rm -f "$tmp_out"
  else
    envelope_rc=$?
    overall_status=1
    mkdir -p "$preserve_dir"
    preserved="$preserve_dir/cross-family-${family}-${posture}-$$.native"
    cp "$tmp_out" "$preserved" 2>/dev/null || preserved="(could not preserve; original at $tmp_out)"
    {
      printf 'cross_family_call: FAILED to build the result envelope\n'
      printf '  family:   %s\n' "$family"
      printf '  posture:  %s\n' "$posture"
      printf '  failure:  envelope construction exited %s\n' "$envelope_rc"
      printf '  native output preserved at: %s\n' "$preserved"
      if [ -s "$envelope_err" ]; then
        printf '  underlying error:\n'
        sed -e 's/^/    /' "$envelope_err"
      fi
    } >&2
    # The failing family still gets a row on stdout, so a consumer reading
    # only stdout sees a family that failed rather than a family that
    # vanished. `native_preserved_at` appears ONLY here -- on a path that
    # previously emitted nothing at all, so no existing consumer can regress.
    #
    # **Built with `printf`, not `jq`, and that is the point.** The envelope
    # step just failed; the overwhelmingly likely reason is `jq` itself, as it
    # was in the incident (harmonic-forge#466). A failure envelope that
    # depended on the tool that failed would emit nothing exactly when it is
    # needed, which is this issue's entire complaint one level down. Only two
    # values are interpolated and both are escaped; `family` and `posture` are
    # closed token sets validated long before dispatch.
    printf '{"family":"%s","posture":"%s","status":"envelope-error","exit_code":%s,"report":null,"native":null,"native_preserved_at":"%s"}\n' \
      "$family" "$posture" "$exit_code" "$(json_escape "$preserved")"
    # `tmp_out` is deliberately NOT removed when preservation failed: the
    # message above points at it, and the incident this issue records was
    # recovered only because such a file happened to survive. Luck is now a
    # mechanism.
    case "$preserved" in
      "(could not preserve"*) : ;;
      *) rm -f "$tmp_out" ;;
    esac
  fi
  rm -f "$envelope_out" "$envelope_err"
done

exit "$overall_status"

#!/usr/bin/env bash
# engine.sh -- agent detection and state classification for tmux-agent.
# Sourced by bin/tmux-agent and by tests/test_tmux_agent.py.
#
# Detection walks the pane's process tree looking for a known coding-agent
# CLI. Classification is heuristic: CPU-time deltas say "working", and the
# bottom of the pane's captured output says what the agent waits on or that
# it finished. States:
#   blocked -- agent needs input or approval
#   error   -- the last turn ended on an API error (rate limit, overload...)
#   working -- agent is actively running
#   ready   -- work finished, the pane has not been looked at since
#   idle    -- finished and seen
#
# shellcheck shell=bash

[[ -n "${_TA_ENGINE_LOADED:-}" ]] && return 0
_TA_ENGINE_LOADED=1

# CPU ticks (100ths of a second) gained across a poll interval to count as
# working when the captured pane output also changed. Idle TUIs periodically
# wake for housekeeping, so CPU activity alone is not evidence of work.
declare -r TA_CPU_TICKS_WORKING=25

# Known agent CLI names. Detection matches these against the process name
# (prefix, so "claude-code" counts) and against argv as a path/word boundary.
declare -r TA_AGENT_NAMES='opencode claude codex gemini aider amp droid grok copilot qoder kiro hermes antigravity'

# Every state, most urgent first. Loops over states use this, so a new state
# is added in one place.
# shellcheck disable=SC2034 # used by the scripts that source this file
declare -r TA_STATES='blocked error working ready idle'

ta_state_rank() {
    case "$1" in
    blocked) echo 0 ;;
    error) echo 1 ;;
    working) echo 2 ;;
    ready) echo 3 ;;
    idle) echo 4 ;;
    *) echo 5 ;;
    esac
}

ta_rollup() {
    # Most urgent of the given states; empty in, empty out.
    local worst='' st rank best=99
    for st in "$@"; do
        [[ -z "$st" ]] && continue
        rank=$(ta_state_rank "$st")
        if ((rank < best)); then
            best=$rank
            worst=$st
        fi
    done
    printf '%s\n' "$worst"
}

ta_state_color() {
    # tmux colour name per state.
    case "$1" in
    blocked) echo red ;;
    error) echo magenta ;;
    working) echo yellow ;;
    ready) echo blue ;;
    idle) echo green ;;
    *) echo white ;;
    esac
}

ta_state_ansi() {
    # ANSI colour per state, for fzf --ansi.
    case "$1" in
    blocked) printf '\033[31m' ;;
    error) printf '\033[35m' ;;
    working) printf '\033[33m' ;;
    ready) printf '\033[34m' ;;
    idle) printf '\033[32m' ;;
    *) printf '\033[37m' ;;
    esac
}

ta_state_char() {
    # Font-native mark per state: shape differs so it reads without colour.
    case "$1" in
    blocked) echo '◆' ;;
    error) echo '✖' ;;
    working) echo '●' ;;
    ready) echo '◉' ;;
    *) echo '○' ;;
    esac
}

ta_state_mark() {
    # State mark coloured and reset afterwards, for text lists (fzf picker,
    # status) so the rest of the line keeps the normal text colour.
    printf '%s%s\033[0m' "$(ta_state_ansi "$1")" "$(ta_state_char "$1")"
}

ta_agent_glyph() {
    # Nerd Font codicons: nf-cod-claude (U+EC82), nf-cod-openai (U+EC81)
    # for Codex, nf-cod-agent (U+EC67) for the rest. Overrides still accept
    # any terminal-renderable glyph.
    case "$1" in
    claude) echo "${TMUX_AGENT_ICON_CLAUDE:-}" ;;
    codex) echo "${TMUX_AGENT_ICON_CODEX:-}" ;;
    opencode) echo "${TMUX_AGENT_ICON_OPENCODE:-}" ;;
    *) echo "${TMUX_AGENT_ICON_DEFAULT:-}" ;;
    esac
}

ta_hook_state() {
    # $1 agent lifecycle event. Prints the state it reports, "end" when the
    # agent session closed, or nothing for events that say nothing about
    # state. Claude Code and Codex share hook event names; the OpenCode
    # plugin forwards its (v2) server bus event types.
    case "$1" in
    SessionStart) echo idle ;;
    UserPromptSubmit | PreToolUse | PostToolUse | PreCompact) echo working ;;
    session.execution.started | permission.replied | form.replied | form.cancelled) echo working ;;
    PermissionRequest | Notification) echo blocked ;;
    permission.asked | form.created) echo blocked ;;
    Stop) echo ready ;;
    # Codex fires no Stop for a turn the user interrupted.
    Interrupt) echo ready ;;
    # Fired when a turn ends on an API error (rate_limit, overloaded, billing_error...).
    StopFailure) echo error ;;
    session.execution.succeeded | session.execution.interrupted) echo ready ;;
    session.execution.failed) echo error ;;
    SessionEnd) echo end ;;
    esac
}

ta_match_blocked() {
    grep -qiE \
        'do you want|\[y/n\]|\[y/N\]|\[Y/n\]|\(y/n\)|press enter to|(^|[^[:alnum:]])(allow|approve|grant|permission)[^?]{0,80}\?|^[^[:alnum:]]{0,6}1\. (yes|no)' \
        <<<"$1"
}

ta_match_working() {
    # Spinner glyphs are alternations, not a bracket class: under a C
    # locale grep would match their bytes individually and light up on
    # unrelated box-drawing characters.
    grep -qiE \
        'esc to interrupt|esc to stop|working\.\.\.|thinking\.\.\.|running\.\.\.|(thinking|doodling|working|running|searching|planning|reading|writing|generating)…|⠋|⠙|⠹|⠸|⠼|⠴|⠦|⠧|⠇|⠏|◐|◓|◑|◒|⣾|⣽|⣻|⢿|⡿|⣟|⣯|⣷|◜|◠|◝|◞|◡|◟' \
        <<<"$1"
}

ta_match_interrupted() {
    # Claude Code's interrupt line: "⎿  Interrupted by user" (older) or
    # "Interrupted · What should Claude do instead?" (newer). Case-sensitive
    # and narrow so code output mentioning "interrupted" does not match.
    grep -qE 'Interrupted by user|Interrupted ·' <<<"$1"
}

ta_match_ready() {
    grep -qiE \
        '✓|✔|(^|[^[:alnum:]])(done\.?|completed|finished|all done|task complete|success)([^[:alnum:]]|$)' \
        <<<"$1"
}

ta_classify() {
    # $1 pane tail text, $2 CPU ticks gained since last poll,
    # $3 previous state, $4 seen-since-last-change (1/0),
    # $5 captured output changed since last poll (1/0). Prints the state.
    local tail=${1:-} cpu_delta=${2:-0} prev=${3:-} seen=${4:-0}
    local output_changed=${5:-0} state=''
    if ta_match_blocked "$tail"; then
        state=blocked
    elif ta_match_working "$tail"; then
        state=working
    elif ((cpu_delta >= TA_CPU_TICKS_WORKING)) && [[ $output_changed == 1 ]]; then
        state=working
    elif ta_match_ready "$tail"; then
        state=ready
    elif [[ $prev == working ]]; then
        # It was going, now it is quiet: the work finished.
        state=ready
    else
        state=${prev:-idle}
    fi
    if [[ $state == ready && $seen == 1 ]]; then
        state=idle
    fi
    printf '%s\n' "$state"
}

ta_agent_name_of() {
    # $1 process name, $2 argv. Prints the matched agent name, if any.
    # macOS ps reports comm as the executable path: match its basename.
    local comm=${1,,} args=${2,,} name
    comm=${comm##*/}
    for name in $TA_AGENT_NAMES; do
        case "$comm" in
        "$name" | "$name"-*)
            printf '%s\n' "$name"
            return 0
            ;;
        esac
        if grep -qE "(^|[/[:space:]])$name([/:.[:space:]]|$)" <<<"$args"; then
            printf '%s\n' "$name"
            return 0
        fi
    done
    return 1
}

ta_ps_snapshot() {
    # Load the process table once into globals so a scan can detect many
    # panes without a ps per pane. Always reloads when called.
    declare -gA TA_PS_CHILDREN=() TA_PS_COMMS=() TA_PS_ARGS=()
    local pid ppid comm args
    while read -r pid ppid comm args; do
        TA_PS_CHILDREN[$ppid]="${TA_PS_CHILDREN[$ppid]:-} $pid"
        TA_PS_COMMS[$pid]=$comm
        TA_PS_ARGS[$pid]=$args
    done < <(ps -eo pid=,ppid=,comm=,args= 2>/dev/null)
    TA_PS_LOADED=1
}

ta_detect_agent() {
    # $1 pane pid. Prints "<agent name> <agent pid>" for the first agent
    # process found in the pane's process tree (the pane process included).
    # Uses the ta_ps_snapshot tables, loading them if nobody has.
    local root=${1:-}
    [[ -n "$root" ]] || return 1
    [[ -n "${TA_PS_LOADED:-}" ]] || ta_ps_snapshot
    local pid next name
    local queue=("$root") visited=' '
    while ((${#queue[@]} > 0)); do
        pid=${queue[0]}
        queue=("${queue[@]:1}")
        case "$visited" in
        *" $pid "*) continue ;;
        esac
        visited="$visited$pid "
        if name=$(ta_agent_name_of "${TA_PS_COMMS[$pid]:-}" "${TA_PS_ARGS[$pid]:-}"); then
            printf '%s %s\n' "$name" "$pid"
            return 0
        fi
        for next in ${TA_PS_CHILDREN[$pid]:-}; do
            queue+=("$next")
        done
    done
    return 1
}

ta_proc_cpu() {
    # utime+stime+cutime+cstime in clock ticks for $1 and its waited-for
    # children, from /proc/<pid>/stat (fields 14-17). Without procfs
    # (macOS), the process's own CPU time from ps, in hundredths.
    local stat f
    if [[ -r /proc/$1/stat ]]; then
        stat=$(cat "/proc/$1/stat" 2>/dev/null) || return 1
        read -r -a f <<<"${stat##*) }"
        echo $((${f[11]:-0} + ${f[12]:-0} + ${f[13]:-0} + ${f[14]:-0}))
        return 0
    fi
    stat=$(ps -o time= -p "$1" 2>/dev/null) || return 1
    ta_ps_time_ticks "$stat"
}

ta_ps_time_ticks() {
    # ps "time" ([dd-][hh:]mm:ss[.cc]) in hundredths of a second.
    local t=${1//[[:space:]]/} days=0 cs=0 secs=0 part
    [[ $t =~ ^([0-9]+-)?[0-9:]+(\.[0-9]+)?$ ]] || return 1
    if [[ $t == *-* ]]; then
        days=${t%%-*}
        t=${t#*-}
    fi
    if [[ $t == *.* ]]; then
        cs=${t##*.}
        cs=${cs:0:2}
        ((${#cs} == 2)) || cs="${cs}0"
        t=${t%.*}
    fi
    local IFS=:
    for part in $t; do
        secs=$((secs * 60 + 10#$part))
    done
    echo $(((10#$days * 86400 + secs) * 100 + 10#$cs))
}

# Keys the send command may inject must stay inside bin/tmux-agent's
# ta_send_key_ok allowlist (Enter Escape Tab Space BSpace Up Down Left Right
# C-c and single y n Y N 0-9).
ta_approve_key() {
    # $1 agent. Key that approves a pending permission prompt; none if unknown.
    case "$1" in
    claude) echo 1 ;; # permission dialog: "1. Yes"
    codex) echo y ;;
    *) return 1 ;;
    esac
}

ta_deny_key() {
    # $1 agent. Key that dismisses a pending permission prompt.
    case "$1" in
    claude | codex | opencode) echo Escape ;;
    *) return 1 ;;
    esac
}

ta_sanitize() {
    # $1 text, $2 max characters (default 200). One line safe for a
    # notification, OSC sequence or the picker: control characters gone
    # (whitespace ones become spaces first), "|" swapped for "¦" (the field
    # separator of detail files and picker rows), whitespace collapsed.
    local s max=${2:-200}
    s=$(printf '%s' "${1:-}" | tr '\n\t\r' '   ' | tr -d '\000-\037\177' | tr -s ' ')
    s=${s//|/¦}
    s=${s# }
    s=${s% }
    if ((${#s} > max)); then
        s="${s:0:max-1}…"
    fi
    printf '%s\n' "$s"
}

ta_server_key() {
    # Short filesystem-safe name of the tmux server we belong to, so state
    # for `tmux -L a` and `-L b` does not mix. $TMUX is set inside panes,
    # which keeps hooks fork-free; outside tmux ask the server, else default.
    local key sock=${TMUX:-}
    sock=${sock%%,*}
    [[ -n "$sock" ]] || sock=$(tmux display-message -p '#{socket_path}' 2>/dev/null) || sock=
    key=${sock##*/}
    key=${key//[^A-Za-z0-9._-]/_}
    printf '%s\n' "${key:-default}"
}

ta_detail_epoch() {
    # $1 state dir, $2 pane id. Prints the epoch field of the detail file;
    # fails silently when missing, empty or not a number.
    local line
    [[ -s "$1/detail-$2" ]] || return 1
    IFS= read -r line <"$1/detail-$2" || [[ -n "$line" ]] || return 1
    line=${line%%|*}
    [[ "$line" =~ ^[0-9]+$ ]] || return 1
    printf '%s\n' "$line"
}

ta_detail_read() {
    # $1 state dir, $2 pane id. Detail file is one "epoch|event|text" line;
    # prints the text (it never contains "|"), fails when there is none.
    local line rest
    [[ -s "$1/detail-$2" ]] || return 1
    IFS= read -r line <"$1/detail-$2" || [[ -n "$line" ]] || return 1
    rest=${line#*|}
    rest=${rest#*|}
    [[ -n "$rest" ]] || return 1
    printf '%s\n' "$rest"
}

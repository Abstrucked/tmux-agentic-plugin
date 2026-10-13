#!/usr/bin/env bash
# pick.sh -- the fzf picker over agent panes. Sourced by bin/tmux-agent, whose
# SELF, refresh, each_agent, age_str and goto_pane it uses.
#
# shellcheck shell=bash

[[ -n "${_TA_PICK_LOADED:-}" ]] && return 0
_TA_PICK_LOADED=1

pad() {
    # $1 text, $2 width: left-aligned, padded with spaces. Counts characters,
    # not bytes, so it lines up where printf's %-Ns would not.
    printf '%s%*s' "$1" "$(($2 > ${#1} ? $2 - ${#1} : 0))" ''
}

pick_rows() {
    # One agent per line, most urgent first: mark, agent, directory, target and
    # age, then a dim detail column (local panes only), then the pane id, the
    # bare agent name and the detail epoch (local panes only) after tabs. fzf
    # shows field 1 only. Fails when there are no agents.
    local st ag host tgt age id path i d e
    local -a sts=() ags=() dirs=() tgts=() ages=() panes=() names=() dets=() epochs=()
    while IFS='|' read -r st ag host tgt age id path; do
        sts+=("$st") ags+=("$ag${host:+@$host}") dirs+=("${path##*/}") tgts+=("$tgt")
        panes+=("$id") ages+=("$(age_str "$age")") names+=("$ag")
        d='' e=''
        if [[ $id != */* ]]; then
            d=$(ta_detail_read "$TA_STATE_DIR" "$id" 2>/dev/null) || d=''
            e=$(ta_detail_epoch "$TA_STATE_DIR" "$id" 2>/dev/null) || e=''
        fi
        epochs+=("$e")
        dets+=("$(ta_sanitize "$d" 60)")
    done < <(each_agent)
    ((${#panes[@]})) || return 1
    # Pad columns to their widest entry. Tabs would jump to the next 8-column
    # stop whenever a name is 8 characters long ("opencode").
    local wa=0 wd=0 wt=0
    for i in "${!panes[@]}"; do
        ((${#ags[i]} > wa)) && wa=${#ags[i]}
        ((${#dirs[i]} > wd)) && wd=${#dirs[i]}
        ((${#tgts[i]} > wt)) && wt=${#tgts[i]}
    done
    for i in "${!panes[@]}"; do
        printf '%s  %s  %s  %s  %s' "$(ta_state_mark "${sts[i]}")" \
            "$(pad "${ags[i]}" "$wa")" "$(pad "${dirs[i]}" "$wd")" "$(pad "${tgts[i]}" "$wt")" \
            "${ages[i]}"
        [[ -z ${dets[i]} ]] || printf '  \033[2m%s\033[0m' "${dets[i]}"
        printf '\t%s\t%s\t%s\n' "${panes[i]}" "${names[i]}" "${epochs[i]}"
    done
}

pick_reload() {
    # What fzf's reload runs: a fresh scan, then the rows.
    refresh
    pick_rows
}

PICK_HEADER='enter go · ^y approve · ^n deny/esc · ^e reply · ^s seen · ^d diff · ^l reload'

pick_msg() {
    # $1 optional message: rewrites the header file fzf shows after an action.
    [[ -n ${TA_PICK_DIR:-} ]] || return 0
    {
        printf '%s\n' "$PICK_HEADER"
        [[ -z ${1:-} ]] || ta_sanitize "$1" 120
    } >"$TA_PICK_DIR/header"
}

pick_action() {
    # $1 action, $2 pane id, $3 agent, $4 detail epoch: what the fzf binds run. fzf quotes the
    # placeholders, so row text never reaches a shell unquoted. Failures go to
    # the header, never to the exit status (that would abort the +reload).
    local act=${1:-} pane=${2:-} agent=${3:-} epoch=${4:-} key err reply
    case $act in
    approve)
        key=$(ta_approve_key "$agent") || {
            pick_msg "no approve key for $agent"
            return 0
        }
        # Guarded: the pane may have moved on since the list was drawn.
        # Also guarded by the request's epoch when the row has one.
        if err=$("$SELF" send --pane "$pane" --expect-state blocked \
            ${epoch:+--expect-epoch "$epoch"} --key "$key" 2>&1); then
            pick_msg
        else
            pick_msg "$err"
        fi
        ;;
    deny)
        key=$(ta_deny_key "$agent") || {
            pick_msg "no deny key for $agent"
            return 0
        }
        # No state guard: Escape also interrupts a working agent.
        err=$("$SELF" send --pane "$pane" --key "$key" 2>&1) || pick_msg "$err"
        ;;
    reply)
        read -r -e -p 'reply> ' reply || reply=''
        [[ -n $reply ]] || return 0
        err=$("$SELF" send --pane "$pane" --text "$reply" --key Enter 2>&1) || pick_msg "$err"
        ;;
    seen)
        "$SELF" seen "$pane" || true
        ;;
    toggle)
        [[ -n ${TA_PICK_DIR:-} ]] || return 0
        if [[ -e $TA_PICK_DIR/diff ]]; then rm -f "$TA_PICK_DIR/diff"; else : >"$TA_PICK_DIR/diff"; fi
        ;;
    preview)
        if [[ -n ${TA_PICK_DIR:-} && -e $TA_PICK_DIR/diff ]]; then
            "$SELF" diff --pane "$pane"
        else
            "$SELF" detail --pane "$pane"
            "$SELF" read --pane "$pane" --lines 20 --ansi
        fi
        ;;
    esac
    return 0
}

pick_fzf_min() {
    # $1 major.minor: whether the installed fzf is at least that version.
    local v
    v=$(fzf --version 2>/dev/null) || return 1
    v=${v%% *}
    local -a ver_have=() ver_want=()
    IFS=. read -ra ver_have <<<"$v"
    IFS=. read -ra ver_want <<<"$1"
    ((ver_have[0] > ver_want[0])) && return 0
    ((ver_have[0] == ver_want[0] && ${ver_have[1]:-0} >= ver_want[1]))
}

fzf_listen_start() {
    # $1 the fzf field holding a row's unique id (the pane id), then a command
    # whose stdout is the row list. Sets FZF_LISTEN_ARGS (for fzf's argv),
    # FZF_LISTEN_TICKER (the ticker's pid) and FZF_LISTEN_DIR.
    # fzf 0.71+: --track --id-nth keep the cursor on the same agent when a
    # reload reorders the rows (a plain reload keeps the same index, so the
    # highlight would jump to another agent). Any reload needs it, the user's
    # ctrl-l too, so it does not depend on the ticker.
    # Ticker (fzf 0.71+ with --listen, and curl): every 2 s it runs the
    # command into a snapshot file, then POSTs reload-sync(cat file). While
    # fzf searches for the tracked row the UI blocks and drops keys, so the
    # reload itself must be instant; the slow command runs here instead.
    # Older fzf gets no ticker: an index-based reload every 2 s is the jump.
    # It exports FZF_API_KEY, a random key that stops other local users from
    # driving fzf through the listen port, and TA_LIVE_ROWS, which fzf's shell
    # expands. Call fzf_listen_stop afterwards.
    local idf=$1 port dir
    shift
    declare -g -a FZF_LISTEN_ARGS=()
    declare -g FZF_LISTEN_TICKER='' FZF_LISTEN_DIR=''
    pick_fzf_min 0.71 || return 0
    FZF_LISTEN_ARGS=(--track "--id-nth=$idf")
    fzf --help 2>/dev/null | grep -q -- '--listen' &&
        command -v curl >/dev/null 2>&1 || return 0
    dir=$(mktemp -d "${TMPDIR:-/tmp}/ta-live.XXXXXX") || return 0
    FZF_LISTEN_DIR=$dir
    export TA_LIVE_ROWS=$dir/rows
    port=$((20000 + RANDOM % 20000))
    FZF_API_KEY=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
    export FZF_API_KEY
    FZF_LISTEN_ARGS+=("--listen=$port")
    # sleep and the command run as jobs we wait on: bash runs the TERM trap
    # at once when wait is interrupted, but would defer it behind a foreground
    # child, and the ticker must die promptly without orphans.
    (
        kid=''
        trap '[[ -z ${kid:-} ]] || kill "$kid" 2>/dev/null; exit 0' TERM
        while :; do
            sleep 2 &
            kid=$!
            wait "$kid"
            "$@" >"$dir/rows.tmp" 2>/dev/null &
            kid=$!
            # No agents left exits non-zero; the empty list is the truth.
            wait "$kid" || true
            kid=''
            mv -f "$dir/rows.tmp" "$dir/rows"
            # The key goes to curl on stdin, not argv, where ps would show it.
            # shellcheck disable=SC2016 # expanded by fzf's shell, not ours
            printf 'header = "x-api-key: %s"\n' "$FZF_API_KEY" |
                curl -s -m 2 -K - -XPOST "localhost:$port" \
                    -d 'reload-sync(cat "$TA_LIVE_ROWS")' >/dev/null 2>&1 || break
        done
    ) &
    FZF_LISTEN_TICKER=$!
}

fzf_listen_stop() {
    # Safe to repeat and when nothing was started.
    if [[ -n ${FZF_LISTEN_TICKER:-} ]]; then
        kill "$FZF_LISTEN_TICKER" 2>/dev/null || true
        wait "$FZF_LISTEN_TICKER" 2>/dev/null || true
    fi
    [[ -z ${FZF_LISTEN_DIR:-} ]] || rm -rf "$FZF_LISTEN_DIR"
    FZF_LISTEN_TICKER='' FZF_LISTEN_DIR=''
    unset TA_LIVE_ROWS
}

pick_cleanup() {
    # $1 temp dir.
    fzf_listen_stop
    rm -rf "$1"
}

cmd_pick() {
    command -v fzf >/dev/null 2>&1 || {
        echo "tmux-agent pick needs fzf" >&2
        return 1
    }
    refresh
    local rows sel pane th='' reload dir
    rows=$(pick_rows) || {
        echo "no agent panes" >&2
        return 1
    }
    dir=$(mktemp -d "${TMPDIR:-/tmp}/ta-pick.XXXXXX") || return 1
    export TA_PICK_DIR=$dir
    # Ctrl-C at the shell, a closed popup or a kill must not leave the temp
    # dir or the ticker behind.
    trap 'pick_cleanup "$dir"; trap - INT TERM HUP; exit 130' INT TERM HUP
    pick_msg
    reload="reload($SELF pick-rows)"
    # transform-header (fzf 0.40) shows the last failure; older fzf keeps the
    # plain key list.
    # shellcheck disable=SC2016 # expanded by fzf's shell, not ours
    pick_fzf_min 0.40 && th='+transform-header(cat "$TA_PICK_DIR/header")'
    fzf_listen_start 2 "$SELF" pick-rows
    sel=$(fzf --ansi --reverse --header "$PICK_HEADER" ${FZF_LISTEN_ARGS[@]+"${FZF_LISTEN_ARGS[@]}"} \
        --delimiter=$'\t' --with-nth=1 \
        --bind "ctrl-y:execute-silent($SELF pick-action approve {2} {3} {4})$th+$reload" \
        --bind "ctrl-n:execute-silent($SELF pick-action deny {2} {3})$th+$reload" \
        --bind "ctrl-e:execute($SELF pick-action reply {2} {3})$th+$reload" \
        --bind "ctrl-s:execute-silent($SELF pick-action seen {2} {3})+$reload" \
        --bind "ctrl-d:execute-silent($SELF pick-action toggle {2} {3})+refresh-preview" \
        --bind "ctrl-l:$reload" \
        --preview "$SELF pick-action preview {2} {3}" \
        --preview-window 'right,60%,border-left' <<<"$rows") || sel=''
    pick_cleanup "$dir"
    trap - INT TERM HUP
    [[ -n $sel ]] || return 0
    IFS=$'\t' read -r _ pane _ <<<"$sel"
    goto_pane "$pane"
}

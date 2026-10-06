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
    # age, then a dim detail column (local panes only), then the pane id and
    # the bare agent name after tabs. fzf shows field 1 only. Fails when there
    # are no agents.
    local st ag host tgt age id path i d
    local -a sts=() ags=() dirs=() tgts=() ages=() panes=() names=() dets=()
    while IFS='|' read -r st ag host tgt age id path; do
        sts+=("$st") ags+=("$ag${host:+@$host}") dirs+=("${path##*/}") tgts+=("$tgt")
        panes+=("$id") ages+=("$(age_str "$age")") names+=("$ag")
        d=''
        [[ $id == */* ]] || d=$(ta_detail_read "$TA_STATE_DIR" "$id" 2>/dev/null) || d=''
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
        printf '\t%s\t%s\n' "${panes[i]}" "${names[i]}"
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
    # $1 action, $2 pane id, $3 agent: what the fzf binds run. fzf quotes the
    # placeholders, so row text never reaches a shell unquoted. Failures go to
    # the header, never to the exit status (that would abort the +reload).
    local act=${1:-} pane=${2:-} agent=${3:-} key err reply
    case $act in
    approve)
        key=$(ta_approve_key "$agent") || {
            pick_msg "no approve key for $agent"
            return 0
        }
        # Guarded: the pane may have moved on since the list was drawn.
        if err=$("$SELF" send --pane "$pane" --expect-state blocked --key "$key" 2>&1); then
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
        [[ $pane == */* ]] || "$SELF" seen "$pane" || true
        ;;
    toggle)
        [[ -n ${TA_PICK_DIR:-} ]] || return 0
        if [[ -e $TA_PICK_DIR/diff ]]; then rm -f "$TA_PICK_DIR/diff"; else : >"$TA_PICK_DIR/diff"; fi
        ;;
    preview)
        if [[ -n ${TA_PICK_DIR:-} && -e $TA_PICK_DIR/diff ]]; then
            "$SELF" diff --pane "$pane"
        else
            [[ $pane == */* ]] || "$SELF" detail --pane "$pane"
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

pick_cleanup() {
    # $1 temp dir, $2 ticker pid (may be empty).
    [[ -z ${2:-} ]] || kill "$2" 2>/dev/null || true
    rm -rf "$1"
}

cmd_pick() {
    command -v fzf >/dev/null 2>&1 || {
        echo "tmux-agent pick needs fzf" >&2
        return 1
    }
    refresh
    local rows sel pane port ticker='' th='' reload dir
    local -a listen=()
    rows=$(pick_rows) || {
        echo "no agent panes" >&2
        return 1
    }
    dir=$(mktemp -d "${TMPDIR:-/tmp}/ta-pick.XXXXXX") || return 1
    export TA_PICK_DIR=$dir
    # Ctrl-C at the shell, a closed popup or a kill must not leave the temp
    # dir or the ticker behind.
    trap 'pick_cleanup "$dir" "$ticker"; trap - INT TERM HUP; exit 130' INT TERM HUP
    pick_msg
    reload="reload($SELF pick-rows)"
    # transform-header (fzf 0.40) shows the last failure; older fzf keeps the
    # plain key list.
    # shellcheck disable=SC2016 # expanded by fzf's shell, not ours
    pick_fzf_min 0.40 && th='+transform-header(cat "$TA_PICK_DIR/header")'
    # A random API key stops other local users from driving the picker through
    # the listen port; FZF_API_KEY needs fzf 0.43.
    if pick_fzf_min 0.43 && fzf --help 2>/dev/null | grep -q -- '--listen' && command -v curl >/dev/null 2>&1; then
        port=$((20000 + RANDOM % 20000))
        FZF_API_KEY=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
        export FZF_API_KEY
        listen=("--listen=$port")
        # The key goes to curl on stdin, not argv, where ps would show it.
        (while sleep 2; do
            printf 'header = "x-api-key: %s"\n' "$FZF_API_KEY" |
                curl -s -K - -XPOST "localhost:$port" -d "$reload" >/dev/null 2>&1 || break
        done) &
        ticker=$!
    fi
    sel=$(fzf --ansi --reverse --header "$PICK_HEADER" ${listen[@]+"${listen[@]}"} \
        --delimiter=$'\t' --with-nth=1 \
        --bind "ctrl-y:execute-silent($SELF pick-action approve {2} {3})$th+$reload" \
        --bind "ctrl-n:execute-silent($SELF pick-action deny {2} {3})$th+$reload" \
        --bind "ctrl-e:execute($SELF pick-action reply {2} {3})$th+$reload" \
        --bind "ctrl-s:execute-silent($SELF pick-action seen {2} {3})+$reload" \
        --bind "ctrl-d:execute-silent($SELF pick-action toggle {2} {3})+refresh-preview" \
        --bind "ctrl-l:$reload" \
        --preview "$SELF pick-action preview {2} {3}" \
        --preview-window 'right,60%,border-left' <<<"$rows") || sel=''
    pick_cleanup "$dir" "$ticker"
    trap - INT TERM HUP
    [[ -n $sel ]] || return 0
    IFS=$'\t' read -r _ pane _ <<<"$sel"
    goto_pane "$pane"
}

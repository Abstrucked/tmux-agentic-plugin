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
    # One agent per line, most urgent first: mark, agent, directory, target
    # and age, then the pane id after a tab, hidden by fzf. Fails when there
    # are no agents.
    local st ag host tgt age id path i
    local -a sts=() ags=() dirs=() tgts=() ages=() panes=()
    while IFS='|' read -r st ag host tgt age id path; do
        sts+=("$st") ags+=("$ag${host:+@$host}") dirs+=("${path##*/}") tgts+=("$tgt")
        panes+=("$id") ages+=("$(age_str "$age")")
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
        printf '%s  %s  %s  %s  %s\t%s\n' "$(ta_state_mark "${sts[i]}")" \
            "$(pad "${ags[i]}" "$wa")" "$(pad "${dirs[i]}" "$wd")" "$(pad "${tgts[i]}" "$wt")" \
            "${ages[i]}" "${panes[i]}"
    done
}

cmd_pick() {
    refresh
    local rows sel
    rows=$(pick_rows) || {
        echo "no agent panes" >&2
        return 1
    }
    sel=$(fzf --ansi --reverse \
        --delimiter=$'\t' --with-nth=1 \
        --preview "$SELF read --pane {2} --lines 20 --ansi" \
        --preview-window 'right,60%,border-left' <<<"$rows") || return 0
    goto_pane "${sel##*$'\t'}"
}

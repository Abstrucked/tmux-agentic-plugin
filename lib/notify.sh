#!/usr/bin/env bash
# notify.sh -- desktop notifications for agent transitions. Sourced by
# bin/tmux-agent, whose SELF and TA_LOCK_FD it uses.
#
# shellcheck shell=bash

[[ -n "${_TA_NOTIFY_LOADED:-}" ]] && return 0
_TA_NOTIFY_LOADED=1

notify_wait() {
    # $1 urgency, $2 pane id, $3 title, $4 body. Clicking the notification
    # (its default action) focuses the pane. --wait holds until the
    # notification closes; libnotify before 0.7.9 has no --wait, so fall back
    # to a plain notification.
    local action
    if ! action=$(notify-send -a tmux-agent -u "$1" -A default=Show --wait "$3" "$4"); then
        notify-send -a tmux-agent -u "$1" "$3" "$4" || true
        return 0
    fi
    if [[ $action == default ]]; then
        "$SELF" focus --pane "$2"
    fi
}

notify() {
    # $1 agent, $2 state, $3 tmux target, $4 working directory, $5 pane id.
    [[ $(tmux show-option -gqv @tmux-agent-notify 2>/dev/null) != off ]] || return 0
    local what=finished urgency=normal title body
    if [[ $2 == blocked ]]; then
        what='needs input'
        urgency=critical
    fi
    title="$1 $what" body="${4##*/} · $3"
    if command -v notify-send >/dev/null 2>&1; then
        # Waits for the click in the background, off the scan lock (the
        # child would hold it until the notification closes) and off the
        # status job's output, which tmux reads to EOF.
        (
            [[ -z ${TA_LOCK_FD:-} ]] || exec {TA_LOCK_FD}>&-
            trap '' HUP
            notify_wait "$urgency" "$5" "$title" "$body"
        ) </dev/null >/dev/null 2>&1 &
    elif command -v osascript >/dev/null 2>&1; then
        # Text goes in as arguments, so quotes in paths need no escaping.
        osascript -e 'on run argv' \
            -e 'display notification (item 2 of argv) with title (item 1 of argv)' \
            -e 'end run' "$title" "$body" >/dev/null 2>&1 || true
    fi
}

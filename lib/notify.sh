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

notify_desktop_ok() {
    # Whether a desktop notification can reach the user: notify-send needs a
    # display (tmux's global environment knows it when this process, started
    # by a key binding or hook, lost it); osascript needs nothing.
    local v
    if command -v notify-send >/dev/null 2>&1; then
        [[ -z ${DISPLAY:-}${WAYLAND_DISPLAY:-} ]] || return 0
        for v in DISPLAY WAYLAND_DISPLAY; do
            [[ $(tmux show-environment -g "$v" 2>/dev/null) == "$v="?* ]] && return 0
        done
    fi
    command -v osascript >/dev/null 2>&1
}

notify_terminal() {
    # $1 pane id, $2 title, $3 body. Asks every attached terminal to raise
    # the notification itself, which also works over ssh and without a
    # display. One printf per client so a sequence is never split.
    local forced id esc=$'\033' bel=$'\a' tty term kind
    forced=$(ta_opt TMUX_AGENT_OSC @tmux-agent-osc '')
    id="ta${1//[^0-9]/}"
    while IFS='|' read -r tty term; do
        [[ -n $tty && -w $tty ]] || continue
        case ${forced:-$term} in
            99 | *kitty*) kind=99 ;;
            777 | foot* | *vte* | *konsole* | *rxvt*) kind=777 ;;
            *) kind=9 ;;
        esac
        # shellcheck disable=SC1003 # ESC \ is the string terminator
        case $kind in
            99) printf '%s]99;i=%s:d=0;%s%s\\%s]99;i=%s:p=body;%s%s\\' \
                "$esc" "$id" "$2" "$esc" "$esc" "$id" "$3" "$esc" ;;
            # ";" separates the fields, so it cannot appear in them.
            777) printf '%s]777;notify;%s;%s%s' "$esc" "${2//;/}" "${3//;/}" "$bel" ;;
            *) printf '%s]9;%s: %s%s' "$esc" "$2" "$3" "$bel" ;;
        esac >>"$tty" 2>/dev/null || true
    done < <(tmux list-clients -F '#{client_tty}|#{client_termname}' 2>/dev/null)
}

notify() {
    # $1 agent, $2 state, $3 tmux target, $4 working directory, $5 pane id.
    [[ $(tmux show-option -gqv @tmux-agent-notify 2>/dev/null) != off ]] || return 0
    local method cmd what urgency title body detail ttitle tbody
    method=$(ta_opt TMUX_AGENT_NOTIFY_METHOD @tmux-agent-notify-method auto)
    [[ $method != off ]] || return 0
    case $2 in
        blocked) what='needs input' urgency=critical ;;
        error) what='hit an error' urgency=critical ;;
        *) what=finished urgency=normal ;;
    esac
    title="$1 $what" body="${4##*/} · $3"
    if detail=$(ta_detail_read "$TA_STATE_DIR" "$5" 2>/dev/null); then
        body="$detail"$'\n'"$body"
    fi
    ttitle=$(ta_sanitize "$title") tbody=$(ta_sanitize "$body" 500)

    if [[ $method == auto ]]; then
        if notify_desktop_ok; then method=desktop; else method=terminal; fi
    fi
    if [[ $method == terminal ]]; then
        (
            [[ -z ${TA_LOCK_FD:-} ]] || exec {TA_LOCK_FD}>&-
            trap '' HUP
            notify_terminal "$5" "$ttitle" "$tbody"
        ) </dev/null >/dev/null 2>&1 &
    elif command -v notify-send >/dev/null 2>&1; then
        # notify-send renders markup in the body.
        # Quoted replacements: bash 5.2 reads a bare "&" there as the match.
        local etitle=${ttitle//'&'/'&amp;'} ebody=${body//'&'/'&amp;'}
        etitle=${etitle//'<'/'&lt;'} etitle=${etitle//'>'/'&gt;'}
        ebody=${ebody//'<'/'&lt;'} ebody=${ebody//'>'/'&gt;'}
        # Waits for the click in the background, off the scan lock (the
        # child would hold it until the notification closes) and off the
        # status job's output, which tmux reads to EOF.
        (
            [[ -z ${TA_LOCK_FD:-} ]] || exec {TA_LOCK_FD}>&-
            trap '' HUP
            notify_wait "$urgency" "$5" "$etitle" "$ebody"
        ) </dev/null >/dev/null 2>&1 &
    elif command -v osascript >/dev/null 2>&1; then
        # Text goes in as arguments, so quotes in paths need no escaping.
        osascript -e 'on run argv' \
            -e 'display notification (item 2 of argv) with title (item 1 of argv)' \
            -e 'end run' "$ttitle" "$tbody" >/dev/null 2>&1 || true
    fi

    cmd=$(ta_opt TMUX_AGENT_NOTIFY_COMMAND @tmux-agent-notify-command '')
    if [[ -n $cmd ]]; then
        # Data travels in the environment only, never in the command string.
        (
            [[ -z ${TA_LOCK_FD:-} ]] || exec {TA_LOCK_FD}>&-
            trap '' HUP
            export TA_TITLE=$ttitle TA_BODY=$tbody TA_STATE=$2 TA_AGENT=$1 \
                TA_PANE=$5 TA_PATH=$4 TA_URGENCY=$urgency
            exec bash -c "$cmd"
        ) </dev/null >/dev/null 2>&1 &
    fi
}

#!/usr/bin/env bash
# notify.sh -- desktop notifications for agent transitions. Sourced by
# bin/tmux-agent, whose SELF and TA_LOCK_FD it uses.
#
# shellcheck shell=bash

[[ -n "${_TA_NOTIFY_LOADED:-}" ]] && return 0
_TA_NOTIFY_LOADED=1

notify_idfile() {
    # $1 pane id ("<key>/<pane>" for a remote agent). The file holding the id
    # of the pane's open desktop notification, kept inside the state dir.
    printf '%s/notif-%s' "$TA_STATE_DIR" "${1//[^A-Za-z0-9%@._-]/_}"
}

notify_wait_once() {
    # $1 1 when the command prints the notification id first (-p), $2 id
    # file, then the notify-send command. Sets nw_id, nw_action and nw_rc in
    # the caller. The id is stored as soon as it is known and removed again
    # when the notification closes, unless a replacement stored its own.
    local withid=$1 file=$2 line first=1
    shift 2
    nw_id='' nw_action='' nw_rc=1
    while IFS= read -r line; do
        if [[ $line == rc:* ]]; then
            nw_rc=${line#rc:}
            continue
        fi
        [[ -n $line ]] || continue
        if ((withid && first)); then
            first=0
            if [[ $line =~ ^[0-9]+$ ]]; then
                nw_id=$line
                printf '%s\n' "$line" >"$file" 2>/dev/null || true
                continue
            fi
        fi
        first=0
        [[ -n $nw_action ]] || nw_action=$line
    done < <(
        "$@"
        # A leading newline: the command's last line may lack its own.
        printf '\nrc:%s\n' "$?"
    )
    if [[ -n $nw_id && -f $file && $(cat "$file" 2>/dev/null) == "$nw_id" ]]; then
        rm -f "$file"
    fi
}

notify_wait() {
    # $1 urgency, $2 pane id, $3 title, $4 body. Clicking the notification
    # (its default action) focuses the pane. --wait holds until the
    # notification closes; libnotify before 0.7.9 has no --wait, so fall back
    # to a plain notification. -p (0.8) gives the notification's id, kept so
    # that it can be replaced or closed (notify_dismiss).
    local file id='' secs
    local -a tmo=() rep=()
    local nw_id nw_action nw_rc
    file=$(notify_idfile "$2")
    secs=$(ta_opt TMUX_AGENT_NOTIFY_TIMEOUT @tmux-agent-notify-timeout '')
    [[ ! $secs =~ ^[0-9]+$ ]] || tmo+=(-t "$((secs * 1000))")
    mkdir -p "$TA_STATE_DIR" 2>/dev/null || true
    if [[ -f $file ]]; then
        read -r id <"$file" 2>/dev/null || true
        [[ ! $id =~ ^[0-9]+$ ]] || rep+=(-r "$id")
    fi
    notify_wait_once 1 "$file" notify-send -a tmux-agent -u "$1" ${tmo[@]+"${tmo[@]}"} ${rep[@]+"${rep[@]}"} \
        -p -A default=Show --wait "$3" "$4"
    if ((nw_rc != 0)); then
        # No -p or no --wait: the old way, then a plain notification.
        notify_wait_once 0 "$file" notify-send -a tmux-agent -u "$1" ${tmo[@]+"${tmo[@]}"} \
            -A default=Show --wait "$3" "$4"
        if ((nw_rc != 0)); then
            notify-send -a tmux-agent -u "$1" "$3" "$4" || true
            return 0
        fi
    fi
    if [[ $nw_action == default ]]; then
        "$SELF" focus --pane "$2"
    fi
}

notify_dismiss() {
    # $1 pane id. Close the pane's open desktop notification, if any: its
    # waiting notify-send then returns too. Off the caller's path: the D-Bus
    # call runs in the background and may be slow or fail, which is ignored.
    local file id=''
    file=$(notify_idfile "$1")
    [[ -f $file ]] || return 0
    read -r id <"$file" 2>/dev/null || true
    rm -f "$file"
    [[ $id =~ ^[0-9]+$ ]] || return 0
    (
        [[ -z ${TA_LOCK_FD:-} ]] || exec {TA_LOCK_FD}>&-
        trap '' HUP
        local -a t=()
        command -v timeout >/dev/null 2>&1 && t=(timeout 2)
        if command -v gdbus >/dev/null 2>&1; then
            ${t[@]+"${t[@]}"} gdbus call --session --dest org.freedesktop.Notifications \
                --object-path /org/freedesktop/Notifications \
                --method org.freedesktop.Notifications.CloseNotification "$id"
        elif command -v busctl >/dev/null 2>&1; then
            ${t[@]+"${t[@]}"} busctl --user call org.freedesktop.Notifications \
                /org/freedesktop/Notifications org.freedesktop.Notifications \
                CloseNotification u "$id"
        elif command -v dbus-send >/dev/null 2>&1; then
            ${t[@]+"${t[@]}"} dbus-send --session --dest=org.freedesktop.Notifications \
                /org/freedesktop/Notifications \
                org.freedesktop.Notifications.CloseNotification "uint32:$id"
        fi
    ) </dev/null >/dev/null 2>&1 &
}

notify_desktop_ok() {
    # Whether a desktop notification can reach the user: notify-send needs a
    # session bus or display (tmux's global environment knows it when this process, started
    # by a key binding or hook, lost it); osascript needs nothing.
    local v
    if command -v notify-send >/dev/null 2>&1; then
        [[ -z ${DBUS_SESSION_BUS_ADDRESS:-}${DISPLAY:-}${WAYLAND_DISPLAY:-} ]] || return 0
        for v in DBUS_SESSION_BUS_ADDRESS DISPLAY WAYLAND_DISPLAY; do
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

notify_deliver() {
    # $1 method, $2 agent, $3 state, $4 tmux target, $5 working directory,
    # $6 pane id, $7 detail ("" when none). Builds the text and dispatches.
    local method=$1 cmd what urgency=normal title body
    case $3 in
        blocked) what='needs input' ;;
        error) what='hit an error' ;;
        *) what=finished ;;
    esac
    # Critical is red and sticky in most daemons: opt-in, blocked and error only.
    if [[ $3 == blocked || $3 == error ]] &&
        [[ $(ta_opt TMUX_AGENT_NOTIFY_URGENCY @tmux-agent-notify-urgency normal) == critical ]]; then
        urgency=critical
    fi
    title="$2 $what" body="${5##*/} · $4"
    [[ -z $7 ]] || body="$7"$'\n'"$body"
    local ttitle tbody
    ttitle=$(ta_sanitize "$title") tbody=$(ta_sanitize "$body" 500)

    if [[ $method == terminal ]]; then
        (
            [[ -z ${TA_LOCK_FD:-} ]] || exec {TA_LOCK_FD}>&-
            trap '' HUP
            notify_terminal "$6" "$ttitle" "$tbody"
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
            notify_wait "$urgency" "$6" "$etitle" "$ebody"
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
            export TA_TITLE=$ttitle TA_BODY=$tbody TA_STATE=$3 TA_AGENT=$2 \
                TA_PANE=$6 TA_PATH=$5 TA_URGENCY=$urgency
            exec bash -c "$cmd"
        ) </dev/null >/dev/null 2>&1 &
    fi
}

notify() {
    # $1 agent, $2 state, $3 tmux target, $4 working directory, $5 pane id
    # ("<host key>/<pane>" for a remote agent).
    [[ $(tmux show-option -gqv @tmux-agent-notify 2>/dev/null) != off ]] || return 0
    local method detail=''
    method=$(ta_opt TMUX_AGENT_NOTIFY_METHOD @tmux-agent-notify-method auto)
    [[ $method != off ]] || return 0
    if [[ $method == auto ]]; then
        if notify_desktop_ok; then method=desktop; else method=terminal; fi
    fi
    if [[ $5 == */* ]]; then
        # A remote agent's detail lives on its host. Fetch it off the scan,
        # which must not wait on ssh.
        (
            [[ -z ${TA_LOCK_FD:-} ]] || exec {TA_LOCK_FD}>&-
            trap '' HUP
            if command -v timeout >/dev/null 2>&1; then
                detail=$(timeout 10 "$SELF" detail --pane "$5" 2>/dev/null) || detail=''
            else
                detail=$("$SELF" detail --pane "$5" 2>/dev/null) || detail=''
            fi
            notify_deliver "$method" "$1" "$2" "$3" "$4" "$5" "$detail"
            wait
        ) </dev/null >/dev/null 2>&1 &
        return 0
    fi
    detail=$(ta_detail_read "$TA_STATE_DIR" "$5" 2>/dev/null) || detail=''
    notify_deliver "$method" "$1" "$2" "$3" "$4" "$5" "$detail"
}

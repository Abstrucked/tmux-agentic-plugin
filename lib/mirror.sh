#!/usr/bin/env bash
# Remote pane views. Sourced by tmux-agent; all remote input goes through
# the Python control-mode client, never through a nested terminal client.
# shellcheck shell=bash

ta_mirror_identity() {
    local key pane marker
    if [[ $1 == %* ]]; then
        IFS='|' read -r key pane marker < <(tmux display-message -p -t "$1" \
            '#{@tmux-agent-remote}|#{@tmux-agent-remote-pane}|#{@tmux-agent-mirror}' 2>/dev/null || true) || true
        if [[ $marker == on && -n $key && $pane =~ ^%[0-9]+$ ]]; then
            printf '%s/%s' "$key" "$pane"
            return 0
        fi
    fi
    printf '%s' "$1"
}

ta_mirror_python() {
    command -v python3 >/dev/null 2>&1 &&
        python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null && return 0
    echo 'remote pane mirrors need local Python 3.9+; install python3 or set @tmux-agent-remote-view attach' >&2
    return 1
}

goto_remote_mirror() (
    local id=$1 key=${1%%/*} pane=${1#*/} sid='' wid='' local_pane='' s name marker
    local w host rp lp dead tries=0 lock fd label agent remote_pane
    local -a client=()
    [[ -z ${2:-} ]] || client=(-c "$2")
    [[ $id == */* && $pane =~ ^%[0-9]+$ ]] || {
        echo 'invalid remote pane id' >&2; return 1;
    }
    ta_remote_target "$key" || { echo "unknown remote host: $key" >&2; return 1; }
    ta_mirror_python || return 1
    mkdir -p "$TA_STATE_DIR"
    if command -v flock >/dev/null 2>&1; then
        exec {fd}>"$TA_STATE_DIR/mirror.lock"
        flock -w 5 "$fd" || { echo 'another mirror is opening; retry' >&2; return 1; }
    else
        lock=$TA_STATE_DIR/mirror.lock.d
        until mkdir "$lock" 2>/dev/null; do
            ((++tries <= 100)) || { echo 'another mirror is opening; retry' >&2; return 1; }
            [[ -z $(find "$lock" -maxdepth 0 -mmin +2 2>/dev/null) ]] || rmdir "$lock" 2>/dev/null || true
            sleep 0.05
        done
        trap 'rmdir "$lock" 2>/dev/null || true' EXIT
    fi
    while IFS='|' read -r s name marker; do
        [[ $name == remote-agents ]] || continue
        [[ $marker == on ]] || {
            echo 'session remote-agents already exists and is not owned by tmux-agent' >&2
            return 1
        }
        sid=$s
    done < <(tmux list-sessions -F '#{session_id}|#{session_name}|#{@tmux-agent-mirror-session}' 2>/dev/null || true)
    if [[ -n $sid ]]; then
        while IFS='|' read -r w host rp lp dead; do
            [[ $host == "$key" && $rp == "$pane" ]] || continue
            wid=$w local_pane=$lp
            if [[ $dead == 1 ]]; then
                tmux respawn-pane -t "$lp" "$SELF" mirror --pane "$id" || return 1
            fi
            break
        done < <(tmux list-panes -s -t "$sid" -F \
            '#{window_id}|#{@tmux-agent-remote}|#{@tmux-agent-remote-pane}|#{pane_id}|#{pane_dead}' 2>/dev/null || true)
    fi
    if [[ -z $wid ]]; then
        agent=agent
        if [[ -f $TA_STATE_DIR/remote-$key ]]; then
            while IFS='|' read -r _state name remote_pane _; do
                [[ $remote_pane != "$pane" ]] || { agent=$name; break; }
            done <"$TA_STATE_DIR/remote-$key"
        fi
        label=$(ta_sanitize "$agent@$TA_REMOTE_NAME-$pane" 60)
        if [[ -z $sid ]]; then
            IFS='|' read -r sid wid local_pane < <(tmux new-session -d -P \
                -F '#{session_id}|#{window_id}|#{pane_id}' -s remote-agents -n "$label" \
                "$SELF" mirror --pane "$id") || return 1
            [[ -n $sid && -n $wid && -n $local_pane ]] || return 1
            tmux set-option -t "$sid" @tmux-agent-mirror-session on
        else
            IFS='|' read -r wid local_pane < <(tmux new-window -d -P \
                -F '#{window_id}|#{pane_id}' -t "$sid:" -n "$label" \
                "$SELF" mirror --pane "$id") || return 1
            [[ -n $wid && -n $local_pane ]] || return 1
        fi
        tmux set-option -w -t "$wid" automatic-rename off
        tmux set-option -w -t "$wid" remain-on-exit on
        tmux set-option -w -t "$wid" @tmux-agent-remote "$key"
        tmux set-option -w -t "$wid" @tmux-agent-remote-pane "$pane"
        tmux set-option -p -t "$local_pane" @tmux-agent-remote "$key"
        tmux set-option -p -t "$local_pane" @tmux-agent-remote-pane "$pane"
        tmux set-option -p -t "$local_pane" @tmux-agent-mirror on
    fi
    tmux switch-client ${client[@]+"${client[@]}"} -t "$wid" 2>/dev/null || tmux select-window -t "$wid"
    (ta_remote_ssh "$key" seen "$pane" || true) </dev/null >/dev/null 2>&1 &
)

cmd_mirror() {
    local id='' pane key
    [[ ${1:-} != --pane ]] || id=${2:-}
    pane=${id#*/} key=${id%%/*}
    [[ $id == */* && $pane =~ ^%[0-9]+$ ]] || { echo 'mirror: invalid remote pane id' >&2; return 1; }
    # Keep diagnostics visible even if the bridge fails before its opener
    # finishes setting options. Mark before any process scan can see it.
    if [[ ${TMUX_PANE:-} =~ ^%[0-9]+$ ]]; then
        tmux set-option -w -t "$TMUX_PANE" remain-on-exit on
        tmux set-option -p -t "$TMUX_PANE" @tmux-agent-mirror on
        tmux set-option -p -t "$TMUX_PANE" @tmux-agent-remote "$key"
        tmux set-option -p -t "$TMUX_PANE" @tmux-agent-remote-pane "$pane"
    fi
    ta_mirror_python || return 1
    # Resolve afresh on every reconnect, since a ControlMaster can close.
    if ! ta_remote_target "$key"; then
        echo "mirror: unknown remote host: $key" >&2
        return 1
    fi
    exec python3 "$(dirname "$SELF")/../lib/remote-pane.py" --pane "$pane" \
        --launcher "$SELF" --identity "$id" \
        --status-file "$TA_STATE_DIR/mirror-${TMUX_PANE:-unknown}.status"
}

cmd_mirror_connect() {
    local pane='' sid
    [[ ${1:-} != --pane ]] || pane=${2:-}
    [[ $pane =~ ^%[0-9]+$ ]] || { echo 'mirror-connect: invalid pane id' >&2; return 2; }
    sid=$(tmux display-message -p -t "$pane" '#{session_id}' 2>/dev/null) || {
        echo TMUX_AGENT_MIRROR_CLOSED; return 1;
    }
    [[ $sid =~ ^\$[0-9]+$ ]] || { echo TMUX_AGENT_MIRROR_CLOSED; return 1; }
    printf 'TMUX_AGENT_MIRROR_V1\n'
    # Attaching by session ID avoids selecting the pane's window for other
    # clients. No resize, zoom, select-window or select-pane is performed.
    exec tmux -C attach-session -f ignore-size -t "$sid"
}

cmd_mirror_transport() {
    # Used by the Python helper on each reconnect. No 20s polling timeout.
    local id=${1:-} key pane
    key=${id%%/*} pane=${id#*/}
    [[ $id == */* && $pane =~ ^%[0-9]+$ ]] || return 2
    ta_remote_target "$key" || { echo "unknown remote host: $key" >&2; return 1; }
    exec "${TA_REMOTE_ARGV[@]}" -o BatchMode=yes -o ServerAliveInterval=5 \
        -o ServerAliveCountMax=3 -T "$TA_REMOTE_DEST" \
        "$(ta_remote_command mirror-connect --pane "$pane")"
}

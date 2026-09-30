#!/usr/bin/env bash
# remote.sh -- agents on other machines. Sourced by bin/tmux-agent, whose
# TA_STATE_DIR, SELF and notify() it uses.
#
# Each host runs tmux-agent itself. This side asks it over ssh
# ("status --porcelain") and caches the answer; nothing listens on a port
# and ssh keys are the only credentials. Hosts are the ssh aliases in
# @tmux-agent-remotes plus, with @tmux-agent-remote-discover on (the
# default), every live ssh ControlMaster connection, so a machine you are
# connected to shows up by itself. Files in $TA_STATE_DIR:
#   remotes            key|name|socket|destination|port, one line per host
#   remote-<key>       the host's last answer, one agent per line:
#                      state|agent|pane|target|window|age|path
#   remote-<key>.meta  ok|err|noplugin, then when it was fetched
#   remote.stamp       when the last fetch round started
# A host's key is user@hostname:port, so an alias and a live connection to
# the same machine count once.
#
# shellcheck shell=bash

[[ -n "${_TA_REMOTE_LOADED:-}" ]] && return 0
_TA_REMOTE_LOADED=1

# The far side's tmux-agent, wherever TPM put it (the paths install.sh
# checks); exits 127 when it is not installed there. Arguments follow.
# @tmux-agent-remote-command replaces it.
# shellcheck disable=SC2016 # expanded by the remote shell
declare -r TA_REMOTE_LOCATE='sh -c '\''for d in "${XDG_CONFIG_HOME:-$HOME/.config}/tmux/plugins" "$HOME/.tmux/plugins"; do f=$d/tmux-agentic-plugin/bin/tmux-agent; [ -x "$f" ] && exec "$f" "$@"; done; exit 127'\'' tmux-agent'

# Set by ta_remote_target: ssh with its options, the destination, and the
# host's display name.
# shellcheck disable=SC2034 # read by bin/tmux-agent
TA_REMOTE_ARGV=() TA_REMOTE_DEST='' TA_REMOTE_NAME=''

ta_opt() {
    # $1 environment variable, $2 tmux option, $3 default when both are
    # empty. A set variable wins, so scripts and tests need no tmux server.
    local value
    if [[ -n ${!1+x} ]]; then
        value=${!1}
    else
        value=$(tmux show-option -gqv "$2" 2>/dev/null || true)
    fi
    printf '%s' "${value:-$3}"
}

ta_remote_interval() {
    # Seconds between fetch rounds (@tmux-agent-remote-interval, default 10).
    local s
    s=$(ta_opt TMUX_AGENT_REMOTE_INTERVAL @tmux-agent-remote-interval 10)
    if [[ ! $s =~ ^[0-9]+$ ]] || ((s == 0)); then
        s=10
    fi
    echo "$s"
}

ta_remote_enabled() {
    # True when there are hosts to list or connections to discover.
    [[ -n $(ta_opt TMUX_AGENT_REMOTES @tmux-agent-remotes '') ]] ||
        [[ $(ta_opt TMUX_AGENT_REMOTE_DISCOVER @tmux-agent-remote-discover on) == on ]]
}

ta_is_self() {
    # $1 hostname. True for this machine, which the local scan covers.
    # uname, not hostname(1): Arch does not install that by default.
    local me
    me=$(uname -n 2>/dev/null || true)
    case " $me ${me%%.*} localhost 127.0.0.1 ::1 " in
    *" $1 "* | *" ${1%%.*} "*) return 0 ;;
    esac
    return 1
}

ta_remote_hosts() {
    # key|name|socket|destination|port for every host to ask: listed
    # aliases first, then live ControlMaster connections not already
    # listed. Socket names have to follow the ControlPath pattern
    # <prefix>%r@%h:%p (dotfiles: ~/.ssh/master-%r@%h:%p); the glob comes
    # from @tmux-agent-ssh-sockets.
    local alias k v host user port key glob prefix sock rest
    local -A have=()
    for alias in $(ta_opt TMUX_AGENT_REMOTES @tmux-agent-remotes ''); do
        host='' user='' port=''
        while read -r k v; do
            case $k in
            hostname) host=$v ;;
            user) user=$v ;;
            port) port=$v ;;
            esac
        done < <(ssh -G "$alias" 2>/dev/null || true)
        host=${host:-$alias} user=${user:-$(id -un)} port=${port:-22}
        key="$user@$host:$port"
        ta_is_self "$host" && continue
        [[ -z ${have[$key]:-} ]] || continue
        have[$key]=1
        printf '%s|%s||%s|\n' "$key" "$alias" "$alias"
    done

    [[ $(ta_opt TMUX_AGENT_REMOTE_DISCOVER @tmux-agent-remote-discover on) == on ]] || return 0
    glob=$(ta_opt TMUX_AGENT_SSH_SOCKETS @tmux-agent-ssh-sockets "$HOME/.ssh/master-*")
    glob=${glob/#\~/$HOME}
    prefix=${glob##*/}
    prefix=${prefix%%\**}
    # Unquoted on purpose: the option is a glob.
    # shellcheck disable=SC2086
    for sock in $glob; do
        [[ -S $sock ]] || continue
        rest=${sock##*/}
        rest=${rest#"$prefix"}
        [[ $rest =~ ^(.+)@(.+):([0-9]+)$ ]] || continue
        user=${BASH_REMATCH[1]} host=${BASH_REMATCH[2]} port=${BASH_REMATCH[3]}
        key="$user@$host:$port"
        ta_is_self "$host" && continue
        [[ -z ${have[$key]:-} ]] || continue
        # ControlPersist keeps a master a while after its last session.
        ssh -O check -S "$sock" "$user@$host" >/dev/null 2>&1 || continue
        have[$key]=1
        printf '%s|%s|%s|%s|%s\n' "$key" "$host" "$sock" "$user@$host" "$port"
    done
}

ta_remote_target() {
    # $1 key. Sets TA_REMOTE_ARGV (ssh and its options), TA_REMOTE_DEST and
    # TA_REMOTE_NAME from the host list; fails for an unknown key.
    local k name sock dest port
    [[ -f $TA_STATE_DIR/remotes ]] || return 1
    while IFS='|' read -r k name sock dest port; do
        [[ $k == "$1" ]] || continue
        TA_REMOTE_ARGV=(ssh -o ConnectTimeout=3)
        # Ride the live connection: no new login, and no second master.
        [[ -z $sock ]] || TA_REMOTE_ARGV+=(-S "$sock" -o ControlMaster=no)
        [[ -z $port ]] || TA_REMOTE_ARGV+=(-p "$port")
        # shellcheck disable=SC2034 # read by bin/tmux-agent
        TA_REMOTE_DEST=$dest TA_REMOTE_NAME=$name
        return 0
    done <"$TA_STATE_DIR/remotes"
    return 1
}

ta_remote_command() {
    # $@ arguments for the far tmux-agent, as one remote shell command.
    local cmd arg
    cmd=$(ta_opt TMUX_AGENT_REMOTE_COMMAND @tmux-agent-remote-command "$TA_REMOTE_LOCATE")
    for arg; do
        cmd+=" $(printf '%q' "$arg")"
    done
    printf '%s' "$cmd"
}

ta_remote_ssh() {
    # $1 key, then the far tmux-agent's arguments. Batch mode: fails
    # instead of prompting, so a status job never hangs on a password.
    ta_remote_target "$1" || return 255
    shift
    local -a limit=()
    command -v timeout >/dev/null 2>&1 && limit=(timeout 20)
    ${limit[@]+"${limit[@]}"} "${TA_REMOTE_ARGV[@]}" -o BatchMode=yes -T \
        "$TA_REMOTE_DEST" "$(ta_remote_command "$@")"
}

ta_remote_fresh() {
    # $1 key. True when its last fetch worked and is recent enough to trust:
    # an open connection does not mean the data is current.
    local st epoch meta=$TA_STATE_DIR/remote-$1.meta
    [[ -f $meta ]] || return 1
    IFS='|' read -r st epoch <"$meta" || return 1
    [[ $st == ok && $epoch =~ ^[0-9]+$ ]] || return 1
    (($(date +%s) - epoch <= 6 * $(ta_remote_interval)))
}

ta_remote_in_view() {
    # $1 key. True when a focused client shows that host's window.
    local name
    while read -r name; do
        [[ -n $name ]] || continue
        [[ $(tmux display-message -p -c "$name" '#{@tmux-agent-remote}' 2>/dev/null) == "$1" ]] &&
            return 0
    done < <(tmux list-clients -F '#{?#{m:*focused*,#{client_flags}},#{client_name},}' 2>/dev/null)
    return 1
}

ta_remote_notify() {
    # $1 key, $2 host name, $3 previous answer, $4 new answer. Notify about
    # agents that just became blocked or ready, as the local scan does.
    [[ -z ${TMUX_AGENT_QUIET:-} ]] || return 0
    local st ag pane tgt path prev
    local -A before=()
    while IFS='|' read -r st _ pane _; do
        [[ -n $pane ]] && before[$pane]=$st
    done <"$3"
    while IFS='|' read -r st ag pane tgt _ _ path; do
        prev=${before[$pane]:-}
        [[ -n $prev && $st != "$prev" ]] || continue
        [[ $st == blocked || $st == ready ]] || continue
        ta_remote_in_view "$1" && continue
        notify "$ag@$2" "$st" "$tgt" "$path" "$1/$pane"
    done <"$4"
}

ta_remote_fetch() {
    # $1 key, $2 host name. Ask the host for its agents and cache the answer.
    local out=$TA_STATE_DIR/remote-$1 rc=0 st=ok
    ta_remote_ssh "$1" status --porcelain >"$out.tmp" 2>/dev/null || rc=$?
    case $rc in
    0) ;;
    127) st=noplugin ;;
    *) st=err ;;
    esac
    if [[ $st == ok ]]; then
        [[ ! -f $out ]] || ta_remote_notify "$1" "$2" "$out" "$out.tmp"
        mv "$out.tmp" "$out"
    else
        rm -f "$out.tmp"
    fi
    printf '%s|%s\n' "$st" "$(date +%s)" >"$out.meta"
}

ta_remote_round() {
    # One fetch round: list the hosts, ask them all at once, then forget
    # hosts that are gone.
    local hosts=$TA_STATE_DIR/remotes key name f base
    local -A listed=()
    ta_remote_hosts >"$hosts.tmp"
    mv "$hosts.tmp" "$hosts"
    while IFS='|' read -r key name _; do
        [[ -n $key ]] || continue
        listed[$key]=1
        ta_remote_fetch "$key" "$name" &
    done <"$hosts"
    wait
    for f in "$TA_STATE_DIR"/remote-*; do
        [[ -e $f ]] || continue
        base=${f#"$TA_STATE_DIR"/remote-}
        base=${base%.meta}
        base=${base%.tmp}
        [[ -n ${listed[$base]:-} ]] || rm -f "$f"
    done
}

ta_remote_clear() {
    # Forget every host: remotes were turned off.
    rm -f "$TA_STATE_DIR"/remote-* "$TA_STATE_DIR/remotes" "$TA_STATE_DIR/remote.stamp"
}

ta_remote_tick() {
    # Called from the status bar: start a fetch round in the background
    # once the last one is older than the interval. The status bar never
    # waits on ssh, and remote-refresh's lock stops rounds piling up behind
    # a slow host.
    local stamp=$TA_STATE_DIR/remote.stamp last=0 now_s
    if ! ta_remote_enabled; then
        ta_remote_clear
        return 0
    fi
    now_s=$(date +%s)
    [[ -f $stamp ]] && read -r last <"$stamp"
    [[ $last =~ ^[0-9]+$ ]] || last=0
    ((now_s - last >= $(ta_remote_interval))) || return 0
    printf '%s\n' "$now_s" >"$stamp"
    (
        [[ -z ${TA_LOCK_FD:-} ]] || exec {TA_LOCK_FD}>&-
        trap '' HUP
        exec "$SELF" remote-refresh
    ) </dev/null >/dev/null 2>&1 &
}

ta_remote_agents() {
    # Cached agents of every fresh host, as
    # "state|agent|name|target|age|key/pane|path".
    local k name f st ag pane tgt age path fetched now_s
    [[ -f $TA_STATE_DIR/remotes ]] || return 0
    now_s=$(date +%s)
    while IFS='|' read -r k name _; do
        f=$TA_STATE_DIR/remote-$k
        if [[ ! -f $f ]] || ! ta_remote_fresh "$k"; then
            continue
        fi
        IFS='|' read -r _ fetched <"$f.meta"
        while IFS='|' read -r st ag pane tgt _ age path; do
            [[ -n $pane && $age =~ ^[0-9]+$ ]] || continue
            printf '%s|%s|%s|%s|%s|%s|%s\n' \
                "$st" "$ag" "$name" "$tgt" "$((age + now_s - fetched))" "$k/$pane" "$path"
        done <"$f"
    done <"$TA_STATE_DIR/remotes"
}

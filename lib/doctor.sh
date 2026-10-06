#!/usr/bin/env bash
# doctor.sh -- `doctor` (install health) and `explain` (why a pane shows the
# state it does). Sourced by bin/tmux-agent, whose globals and functions
# (SELF, TA_STATE_DIR, refresh, now, age_str, cmd_remotes...) it uses.
#
# shellcheck shell=bash
# shellcheck disable=SC2154 # SELF, TA_* come from bin/tmux-agent

[[ -n "${_TA_DOCTOR_LOADED:-}" ]] && return 0
_TA_DOCTOR_LOADED=1

DOC_FAIL=0

doc_line() {
    # $1 OK|WARN|FAIL|INFO, $2 check, $3 info.
    [[ $1 != FAIL ]] || DOC_FAIL=1
    printf '%-4s %-14s %s\n' "$1" "$2" "$3"
}

doc_ver_ge() {
    # $1 text holding a version ("tmux 3.3a", "0.44 (brew)"), $2 major.minor.
    # True when its first major.minor is at least $2.
    [[ $1 =~ ([0-9]+)\.([0-9]+) ]] || return 1
    local maj=${BASH_REMATCH[1]} min=${BASH_REMATCH[2]}
    [[ $2 =~ ^([0-9]+)\.([0-9]+)$ ]] || return 1
    ((10#$maj > 10#${BASH_REMATCH[1]} || (10#$maj == 10#${BASH_REMATCH[1]} && 10#$min >= 10#${BASH_REMATCH[2]})))
}

doc_events() {
    # $1 variable name in lib/install-hooks (CLAUDE_EVENTS...). Prints the
    # event names, one per line: matcher (=...) and timeout (@...) dropped.
    local raw spec
    raw=$(sed -n "s/^declare -r $1='\\([^']*\\)'.*/\\1/p" "$(dirname "$SELF")/../lib/install-hooks" 2>/dev/null) || return 0
    for spec in $raw; do
        spec=${spec%%=*}
        printf '%s\n' "${spec%%@*}"
    done
}

doc_realpath() { readlink -f "$1" 2>/dev/null || printf '%s' "$1"; }

doc_hooks() {
    # $1 agent (claude|codex), $2 JSON hooks file, $3 install-hooks variable.
    local agent=$1 file=$2 var=$3 ev cmd bin ours total=0 found=0
    local -a missing=() stale=()
    ours=$(doc_realpath "$SELF")
    if ! command -v jq >/dev/null 2>&1; then
        doc_line WARN "$agent hooks" "cannot check without jq"
        return 0
    fi
    if [[ ! -s $file ]]; then
        doc_line WARN "$agent hooks" "not installed ($file): heuristics only; run install-hooks $agent"
        return 0
    fi
    while IFS= read -r ev; do
        [[ -n $ev ]] || continue
        total=$((total + 1))
        local hit=0 other=''
        while IFS= read -r cmd; do
            [[ $cmd == *" hook $agent $ev" ]] || continue
            bin=${cmd% hook "$agent" "$ev"}
            bin=${bin#\"} bin=${bin%\"}
            if [[ $(doc_realpath "$bin") == "$ours" ]]; then
                hit=1
            else
                other=$bin
            fi
        done < <(jq -r --arg e "$ev" '.hooks[$e][]?.hooks[]?.command // empty' "$file" 2>/dev/null || true)
        if ((hit)); then
            found=$((found + 1))
        elif [[ -n $other ]]; then
            stale+=("$ev -> $other")
        else
            missing+=("$ev")
        fi
    done < <(doc_events "$var")
    if ((${#stale[@]})); then
        doc_line WARN "$agent hooks" "point at another tmux-agent (stale install): ${stale[0]}$( ((${#stale[@]} > 1)) && echo " (+$((${#stale[@]} - 1)) more)"); re-run install-hooks $agent"
    fi
    if ((${#missing[@]})); then
        doc_line WARN "$agent hooks" "$found/$total events installed here, missing: ${missing[*]}; re-run install-hooks $agent"
    elif ((${#stale[@]} == 0)); then
        doc_line OK "$agent hooks" "all $total events installed ($file)"
    fi
}

doc_claude() {
    local file=$HOME/.claude/settings.json
    if [[ ! -s $file ]] || ! grep -q 'tmux-agent' "$file" 2>/dev/null; then
        if grep -qs 'tmux-agentic' "$HOME/.claude/plugins/installed_plugins.json" "$HOME/.claude/plugins/known_marketplaces.json"; then
            doc_line OK "claude hooks" "provided by the Claude Code plugin"
        else
            doc_line WARN "claude hooks" "not installed ($file): heuristics only; run install-hooks claude"
        fi
        return 0
    fi
    doc_hooks claude "$file" CLAUDE_EVENTS
}

doc_codex() {
    local file=$HOME/.codex/hooks.json config=$HOME/.codex/config.toml
    doc_hooks codex "$file" CODEX_EVENTS
    if awk '/^\[/{s=$0} s=="[features]" && /^[[:space:]]*hooks[[:space:]]*=[[:space:]]*false/{f=1} END{exit !f}' \
        "$config" 2>/dev/null; then
        doc_line WARN "codex config" "[features] hooks = false in $config: hooks are disabled"
    fi
    doc_line INFO "codex trust" "Codex runs new hooks only after you trust them: open /hooks in Codex"
}

doc_opencode() {
    local link=$HOME/.config/opencode/plugins/tmux-agent target want
    if [[ ! -L $link ]]; then
        doc_line INFO "opencode" "plugin not installed (run install-hooks opencode)"
        return 0
    fi
    target=$(doc_realpath "$link")
    want=$(doc_realpath "$(dirname "$SELF")/../opencode")
    if [[ $target == "$want" ]]; then
        doc_line OK "opencode" "plugin links to $want"
    else
        doc_line WARN "opencode" "plugin links to $target, not this install ($want)"
    fi
}

doc_osc() {
    # $1 forced OSC type, $2 terminal name: the kind notify_terminal uses.
    case ${1:-$2} in
    99 | *kitty*) echo 99 ;;
    777 | foot* | *vte* | *konsole* | *rxvt*) echo 777 ;;
    *) echo 9 ;;
    esac
}

doc_notify() {
    local on method resolved cmd forced tty term n=0
    on=$(tmux show-option -gqv @tmux-agent-notify 2>/dev/null || true)
    method=$(ta_opt TMUX_AGENT_NOTIFY_METHOD @tmux-agent-notify-method auto)
    resolved=$method
    if [[ $method == auto ]]; then
        if notify_desktop_ok; then resolved=desktop; else resolved=terminal; fi
        resolved="auto -> $resolved"
    fi
    if [[ $on == off ]]; then
        doc_line INFO "notify" "off (@tmux-agent-notify off)"
    else
        doc_line OK "notify" "on, method $resolved"
    fi
    if [[ $method == auto || $method == desktop ]] && ! notify_desktop_ok; then
        doc_line WARN "notify" "no desktop notifier reachable (notify-send with a bus or display, or osascript)"
    fi
    cmd=$(ta_opt TMUX_AGENT_NOTIFY_COMMAND @tmux-agent-notify-command '')
    if [[ -n $cmd ]]; then
        doc_line INFO "notify cmd" "set"
    else
        doc_line INFO "notify cmd" "not set"
    fi
    forced=$(ta_opt TMUX_AGENT_OSC @tmux-agent-osc '')
    while IFS='|' read -r tty term; do
        [[ -n $tty ]] || continue
        n=$((n + 1))
        doc_line INFO "notify client" "$tty ($term): terminal mode would send OSC $(doc_osc "$forced" "$term")"
    done < <(tmux list-clients -F '#{client_tty}|#{client_termname}' 2>/dev/null || true)
    ((n)) || doc_line INFO "notify client" "no attached client"
}

doc_remotes() {
    # Cached state only: cmd_remotes may fetch, doctor must not touch the network.
    local k name n=0 bad=() st meta
    if [[ ! -f $TA_STATE_DIR/remotes ]]; then
        doc_line INFO "remotes" "none known yet (no fetch round has run)"
        return 0
    fi
    while IFS='|' read -r k name _; do
        [[ -n $k ]] || continue
        n=$((n + 1))
        st=pending
        meta=$TA_STATE_DIR/remote-$k.meta
        if [[ -f $meta ]]; then
            IFS='|' read -r st _ <"$meta" || true
        fi
        case $st in err | noplugin) bad+=("$name ($st)") ;; esac
    done <"$TA_STATE_DIR/remotes"
    if ((${#bad[@]})); then
        doc_line WARN "remotes" "$n host(s); problems: ${bad[*]}"
    else
        doc_line OK "remotes" "$n host(s)"
    fi
}

doc_tools() {
    local v fz
    if ((BASH_VERSINFO[0] >= 4)); then
        doc_line OK bash "$BASH_VERSION"
    else
        doc_line FAIL bash "$BASH_VERSION: tmux-agent needs bash 4 or newer"
    fi
    if ! v=$(tmux -V 2>/dev/null); then
        doc_line FAIL tmux "not found"
    elif ! doc_ver_ge "$v" 3.2; then
        doc_line FAIL tmux "$v: needs 3.2 or newer"
    elif tmux list-sessions >/dev/null 2>&1; then
        doc_line OK tmux "$v, server reachable"
    else
        doc_line FAIL tmux "$v, but no server is reachable"
    fi
    if ! fz=$(fzf --version 2>/dev/null); then
        doc_line WARN fzf "not found: no picker or mobile menu"
    else
        fz=${fz%% *}
        if ! doc_ver_ge "$fz" 0.40; then
            doc_line WARN fzf "$fz: older than 0.40, the picker header errors"
        elif ! doc_ver_ge "$fz" 0.43; then
            doc_line WARN fzf "$fz: no picker auto-reload before 0.43"
        else
            doc_line OK fzf "$fz"
        fi
    fi
    if command -v jq >/dev/null 2>&1; then
        doc_line OK jq "$(jq --version 2>/dev/null)"
    else
        doc_line WARN jq "not found: no hook details, no status --json"
    fi
    if command -v curl >/dev/null 2>&1; then
        doc_line OK curl "found"
    else
        doc_line WARN curl "not found: no picker auto-reload"
    fi
}

doc_state() {
    local mode stamp
    doc_line OK plugin "$(doc_realpath "$(dirname "$SELF")/..")"
    doc_line OK "server key" "$(ta_server_key)"
    if [[ ! -d $TA_STATE_DIR ]]; then
        doc_line WARN "state dir" "$TA_STATE_DIR does not exist yet"
        return 0
    fi
    mode=$(stat -c %a "$TA_STATE_DIR" 2>/dev/null || stat -f %Lp "$TA_STATE_DIR" 2>/dev/null || echo '?')
    if [[ ! -O $TA_STATE_DIR ]]; then
        doc_line WARN "state dir" "$TA_STATE_DIR is not owned by you"
    elif [[ $mode != 700 ]]; then
        doc_line WARN "state dir" "$TA_STATE_DIR has mode $mode, expected 700"
    else
        doc_line OK "state dir" "$TA_STATE_DIR (mode 700)"
    fi
    if [[ -f $TA_STATE_DIR/stamp ]] && stamp=$(<"$TA_STATE_DIR/stamp") && [[ $stamp =~ ^[0-9]+$ ]]; then
        doc_line INFO "last scan" "$(age_str "$(($(now) - stamp))") ago"
    else
        doc_line INFO "last scan" "never"
    fi
}

cmd_doctor() {
    DOC_FAIL=0
    doc_tools
    doc_state
    doc_claude
    doc_codex
    doc_opencode
    doc_notify
    doc_remotes
    return "$DOC_FAIL"
}

doc_authoritative() {
    # $1 report state, $2 report epoch, $3 now, $4 agent detected (0/1).
    if declare -F report_authoritative >/dev/null; then
        report_authoritative "$@"
        return
    fi
    [[ -n $1 ]] || return 1
    (($3 - $2 < TA_REPORT_TTL)) || { [[ $4 == 1 && $1 != working ]]; }
}

doc_age() {
    # $1 epoch: "<n>s ago" style, or "n/a".
    if [[ ! $1 =~ ^[0-9]+$ ]] || ((10#$1 == 0)); then
        printf 'n/a'
        return 0
    fi
    printf '%s ago' "$(age_str "$(($(now) - $1))")"
}

cmd_explain() {
    local pane=''
    while (($#)); do
        case $1 in
        --pane)
            pane=${2:-}
            shift 2 || shift
            ;;
        *) shift ;;
        esac
    done
    [[ -n $pane ]] || {
        echo "usage: tmux-agent explain --pane <id>" >&2
        return 2
    }
    if [[ $pane == */* ]]; then
        ta_remote_ssh "${pane%%/*}" explain --pane "${pane#*/}"
        return
    fi

    local id pid target path found=0 detect='' agent='' apid='' now_s
    now_s=$(now)
    while IFS='|' read -r id pid _ target path; do
        [[ $id == "$pane" ]] || continue
        found=1
        break
    done < <(tmux list-panes -a -F "$TA_PANE_FMT" 2>/dev/null || true)
    if ((!found)); then
        echo "pane $pane: not found" >&2
        return 1
    fi
    echo "pane       $pane  $target  $path"

    ta_ps_snapshot
    detect=$(ta_detect_agent "$pid" || true)
    if [[ -n $detect ]]; then
        read -r agent apid <<<"$detect"
        echo "agent      $agent (pid $apid)"
    else
        echo "agent      none detected"
    fi

    local rstate='' repoch=0 report_auth=0 f
    f=$TA_STATE_DIR/report-$pane
    if [[ -f $f ]]; then
        IFS='|' read -r rstate repoch <"$f" || true
        [[ $repoch =~ ^[0-9]+$ ]] || repoch=0
        local has_agent=0 auth_word=no
        [[ -z $detect ]] || has_agent=1
        if doc_authoritative "$rstate" "$repoch" "$now_s" "$has_agent"; then
            report_auth=1 auth_word=yes
        fi
        echo "report     state=${rstate:-?} epoch=$repoch ($(doc_age "$repoch")) authoritative=$auth_word ttl=${TA_REPORT_TTL}s"
    else
        echo "report     none"
    fi

    local d depoch
    if d=$(ta_detail_read "$TA_STATE_DIR" "$pane"); then
        depoch=$(ta_detail_epoch "$TA_STATE_DIR" "$pane" || echo 0)
        echo "detail     $d (epoch $depoch, $(doc_age "$depoch"))"
    else
        echo "detail     none"
    fi

    local prev='' pcpu=0 changed=$now_s phash='' scanned=0
    f=$TA_STATE_DIR/state-$pane
    if [[ -f $f ]]; then
        IFS='|' read -r prev _ pcpu changed scanned _ _ _ phash <"$f" || true
        [[ $pcpu =~ ^[0-9]+$ ]] || pcpu=0
        [[ $changed =~ ^[0-9]+$ ]] || changed=$now_s
        echo "cache      state=$prev changed $(doc_age "$changed"), last scan $(doc_age "$scanned")"
    else
        echo "cache      no state file"
    fi

    local seen_at=0 seen=0
    [[ -f $TA_STATE_DIR/seen-$pane ]] && seen_at=$(<"$TA_STATE_DIR/seen-$pane")
    [[ $seen_at =~ ^[0-9]+$ ]] || seen_at=0
    ((seen_at >= changed && seen_at > 0)) && seen=1
    if ((seen_at)); then
        local seen_word=no
        ((!seen)) || seen_word=yes
        echo "seen       $(doc_age "$seen_at"); since last change: $seen_word"
    else
        echo "seen       never"
    fi

    local tail hash cpu=0 delta output_changed=0 line
    tail=$(tmux capture-pane -p -J -S -30 -t "$pane" 2>/dev/null | tail_lines 12 || true)
    read -r hash _ <<<"$(cksum <<<"$tail")"
    [[ -z $apid ]] || cpu=$(ta_proc_cpu "$apid" || echo 0)
    delta=$((cpu - pcpu))
    [[ -n $phash && $hash != "$phash" ]] && output_changed=1
    local changed_word=no
    ((!output_changed)) || changed_word=yes
    echo "heuristics last lines of the pane:"
    while IFS= read -r line; do
        printf '             | %s\n' "$line"
    done < <(printf '%s\n' "$tail" | tail -n 5)
    echo "           cpu ticks now=$cpu stored=$pcpu delta=$delta (working at >= $TA_CPU_TICKS_WORKING with output change); output changed=$changed_word"

    local heur heur_unseen final source
    heur=$(ta_classify "$tail" "$delta" "$prev" "$seen" "$output_changed")
    heur_unseen=$(ta_classify "$tail" "$delta" "$prev" 0 "$output_changed")
    echo "           ta_classify -> $heur"

    if ((report_auth)) && [[ $rstate == working ]] && ta_match_interrupted "$tail"; then
        report_auth=0
        echo "           the pane shows an interrupted turn: the working report is dropped"
    fi
    if ((report_auth)); then
        final=$rstate source='hook report'
        if [[ $final == ready || $final == error ]] && ((seen_at >= repoch && seen_at > 0)); then
            final=idle source='seen → idle'
        fi
    else
        final=$heur source=heuristics
        [[ $heur == idle && $heur_unseen == ready ]] && source='seen → idle'
    fi
    echo "state      $final"
    echo "source     $source"
}

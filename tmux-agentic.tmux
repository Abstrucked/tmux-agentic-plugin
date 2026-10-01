#!/usr/bin/env bash
# tmux-agentic-plugin -- TPM entry point. Binds the agent picker and the
# jump-to-urgent key, puts the agent strip in the status bar and marks agent
# panes as seen on focus.
#
# Options (set before TPM runs):
#   @tmux-agent-key             picker key after the prefix (default a, off: none)
#   @tmux-agent-urgent-key      key after the prefix that jumps to the next
#                               blocked, then ready agent (default A, off: none)
#   @tmux-agent-popup-size      picker popup width and height (default 80%)
#   @tmux-agent-strip-position  interpolate (default): replace #{agent_status}
#                               in status-left/status-right; centre: centre
#                               of the status bar; off: no strip
#   @tmux-agent-strip-max       agents shown by name before counting (default 4)
#   @tmux-agent-notify          desktop notifications, on (default) or off
#   @tmux-agent-raise-command   raises the terminal on a notification click;
#                               detected by default (lib/raise), off: none
#   @tmux-agent-remotes         ssh aliases whose agents to show too
#   @tmux-agent-remote-discover also ask hosts with a live ssh ControlMaster
#                               connection, on (default) or off
#   @tmux-agent-remote-tailscale also ask every online Linux/macOS peer of the
#                               tailnet (needs tailscale and jq), on (default)
#                               or off
#   @tmux-agent-remote-interval seconds between remote fetches (default 10)
#   @tmux-agent-ssh-sockets     ControlMaster socket glob (~/.ssh/master-*)
#   @tmux-agent-remote-command  runs tmux-agent on a remote host (default:
#                               looks in TPM's plugin directories)
# The remote options are read on every refresh, so they apply without a
# reload.
#
# Agent hooks are opt-in: run bin/tmux-agent install-hooks once.
set -euo pipefail

CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
declare -r BIN="$CURRENT_DIR/bin/tmux-agent"

option() {
    # $1 option name, $2 default when unset or empty.
    local value
    value=$(tmux show-option -gqv "$1")
    printf '%s' "${value:-$2}"
}

interpolate() {
    # $1 strip command. Replace #{agent_status} in status-left/right.
    local opt value
    for opt in status-left status-right; do
        value=$(tmux show-option -gqv "$opt")
        [[ $value == *'#{agent_status}'* ]] || continue
        tmux set-option -gq "$opt" "${value//'#{agent_status}'/"$1"}"
    done
}

focus_hook() {
    # Focusing an agent pane marks its finished state as seen (ready ->
    # idle). Appended so the user's own hooks stay; skipped on reloads.
    tmux show-hooks -g pane-focus-in 2>/dev/null | grep -qF "$BIN" && return 0
    tmux set-hook -ga pane-focus-in "run-shell \"'$BIN' seen #{pane_id}\""
}

main() {
    if ((BASH_VERSINFO[0] < 4)); then
        tmux display-message "tmux-agentic-plugin needs bash 4+ (found $BASH_VERSION)"
        return 0
    fi
    local key urgent_key size position max
    key=$(option @tmux-agent-key a)
    urgent_key=$(option @tmux-agent-urgent-key A)
    size=$(option @tmux-agent-popup-size 80%)
    position=$(option @tmux-agent-strip-position interpolate)
    # Only pass a limit that was set, so TMUX_AGENT_STRIP_MAX still applies.
    max=$(tmux show-option -gqv @tmux-agent-strip-max)

    if [[ $key != off ]]; then
        tmux bind-key "$key" display-popup -E -w "$size" -h "$size" "'$BIN' pick"
    fi
    if [[ $urgent_key != off ]]; then
        # A message instead of tmux's "returned 1" when nothing needs you.
        tmux bind-key "$urgent_key" run-shell \
            "'$BIN' attach --urgent --from '#{pane_id}' 2>/dev/null || tmux display-message 'no agent needs you'"
    fi
    focus_hook
    case $position in
    centre | center) "$BIN" place-strip ${max:+--max "$max"} ;;
    off) ;;
    *) interpolate "#('$BIN' strip${max:+ --max $max})" ;;
    esac
}

main

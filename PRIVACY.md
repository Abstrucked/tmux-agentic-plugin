# Privacy

tmux-agentic-plugin collects no data and has no telemetry. Nothing is sent to
the author or to any third party, and the code contains no HTTP client or
analytics of any kind.

- **Local state only.** Agent state lives in small files under
  `$TMUX_AGENT_STATE_DIR`, by default `${TMPDIR:-/tmp}/tmux-agent-<uid>`
  (`bin/tmux-agent`). Remove that directory to clear it.
- **Hooks.** The Claude Code, Codex and OpenCode hooks only run the local
  `tmux-agent` script, which records the agent's state for its tmux pane.
- **Notifications** are local desktop notifications through `notify-send` or
  `osascript`. Turn them off with `set -g @tmux-agent-notify off`.
- **Remote agents (optional).** To show agents on other machines, the plugin
  runs `ssh` to hosts you list in `@tmux-agent-remotes`, to hosts you already
  have a live ssh ControlMaster connection to, and, unless you set
  `@tmux-agent-remote-tailscale off`, to online peers of your tailnet. Peers
  are found with the local `tailscale status --json` command. These
  connections use your own ssh keys and run `tmux-agent` on the remote host
  to read its agent states. Turn each part off with
  `@tmux-agent-remote-discover off` and `@tmux-agent-remote-tailscale off`.

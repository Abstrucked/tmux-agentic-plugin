# Privacy

tmux-agentic-plugin collects no data and has no telemetry. Nothing is sent to
the author or to any third party, and the code contains no HTTP client or
analytics of any kind.

- **Local state only.** Agent state lives in small files under
  `$TMUX_AGENT_STATE_DIR`, by default a private (0700) directory per tmux
  server: `$XDG_RUNTIME_DIR/tmux-agent-<uid>/<server>`, else
  `${TMPDIR:-/tmp}/tmux-agent-<uid>/<server>` (`bin/tmux-agent`). Remove
  that directory to clear it. Earlier versions used one shared
  `/tmp/tmux-agent-<uid>` that other local users could read; you can remove it.
- **Hooks.** The Claude Code, Codex and OpenCode hooks only run the local
  `tmux-agent` script, which records the agent's state for its tmux pane.
  With jq installed it also stores a one-line detail per pane in the state
  directory, taken from the agent's own hook data: the permission request,
  the first line of your prompt, the first line of the final answer, or the
  error. It is deleted when the session ends or the pane closes.
- **Notifications** are local desktop notifications through `notify-send` or
  `osascript`, or escape sequences written to your own terminal (over ssh,
  they travel on that connection). The terminal kind is used when you set
  `@tmux-agent-notify-method terminal`, and by the default `auto` when no
  desktop is reachable, such as tmux on a remote box. Turn them off with `set -g @tmux-agent-notify off`.
- **Notify command (optional).** `@tmux-agent-notify-command` is empty
  unless you set it. When set, the plugin runs it on each notification with
  the title, the detail line, the agent and the directory in its environment,
  and it sends whatever your command sends, wherever it sends it.
- **Remote agents (optional).** To show agents on other machines, the plugin
  runs `ssh` to hosts you list in `@tmux-agent-remotes`, to hosts you already
  have a live ssh ControlMaster connection to, and, unless you set
  `@tmux-agent-remote-tailscale off`, to online peers of your tailnet. Peers
  are found with the local `tailscale status --json` command. These
  connections use your own ssh keys and run `tmux-agent` on the remote host
  to read its agent states. Turn each part off with
  `@tmux-agent-remote-discover off` and `@tmux-agent-remote-tailscale off`.

# Changelog

## 0.2.0 (2026-10-06)

- Installable as a Claude Code plugin that registers the Claude Code hooks:
  `/plugin marketplace add Abstrucked/tmux-agentic-plugin`, then
  `/plugin install tmux-agentic-plugin@tmux-agentic`. An alternative to
  `install-hooks claude`; the tmux plugin is still installed separately.
- Metadata-only Codex plugin manifest. Codex hooks still come from
  `install-hooks codex`.
- `PRIVACY.md` states what the plugin collects (nothing) and what it sends.
- CI runs the HOL Guard repository scan and publishes signed provenance, with
  test dependencies installed from hash-pinned `requirements-dev.txt`.
- The strip puts a space between a state mark and an agent count (`● 5`).

- Tailscale peers are asked for their agents too (`@tmux-agent-remote-tailscale`,
  needs `tailscale` and `jq`). Hosts without the plugin are retried every
  5 minutes and show as `noplugin` in `remotes`; one host is asked per peer.
- A `tmux-agent watch` over ssh keeps each remote host's agents current within
  a second (`@tmux-agent-remote-watch`); hosts that lack it are polled every
  interval, and unreachable hosts are backed off.

- Agents on other machines show up next to local ones, as `claude@devbox`,
  in the strip, the picker and `prefix + A`, and notify when they get
  blocked or finish. Hosts are the ssh aliases in `@tmux-agent-remotes`,
  plus any machine you have a live ssh ControlMaster connection to. Each
  host is asked over ssh; nothing listens on a port. Jumping to a remote
  agent opens a window with a nested tmux client on that host, and later
  jumps reuse it.
- `status --porcelain` lists this host's agents for other hosts to fetch;
  `status --remote` adds remote agents; `remotes` shows each host's last
  fetch.

- Clicking a notification jumps to the agent's pane and raises its terminal
  window, switching to its workspace or tag. Hyprland, sway, awesome and X11
  window managers (through `xdotool` or `wmctrl`) are detected;
  `@tmux-agent-raise-command` takes a command of your own, or `off`.
- `focus --pane <id>` runs that jump from the command line.

- `prefix + A` jumps to the next agent that needs you: blocked first, then
  finished and unseen. Pressing it again cycles through them. Rebind with
  `@tmux-agent-urgent-key`, or turn it off with `off`.
- `attach --urgent` limits the jump to blocked and ready agents, and
  `attach --from <pane>` moves to the candidate after that pane.

## 0.1.0 (2026-09-30)

First release as a standalone TPM plugin, extracted from
[abstrucked's dotfiles](https://github.com/Abstrucked/abstrucked-arch) with its history.

- TPM entry point `tmux-agentic.tmux` with `@tmux-agent-*` options: picker
  key and popup size, strip position (`#{agent_status}` interpolation or
  centred), strip name limit, notifications.
- Paths resolve from the plugin checkout; nothing is installed to `~/.local`.
- `install-hooks --remove` takes the agent hooks out again.
- macOS support: `ps` CPU time without procfs, a `mkdir` lock without
  `flock`, `osascript` notifications.
- `place-strip` replaces an earlier strip, so a moved plugin or a new limit
  applies on config reload.

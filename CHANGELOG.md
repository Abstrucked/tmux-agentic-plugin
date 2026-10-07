# Changelog

## Unreleased

- Remote agent jumps now open live pane mirrors in the local `remote-agents`
  session, preserving the remote pane's layout, size and process. Mirrors use
  a persistent SSH control-mode bridge, retry disconnects without replaying
  input, and support panning and history scrolling. This default needs Python
  3.9+ locally and the updated plugin on both hosts. Set
  `@tmux-agent-remote-view attach` for the previous nested-tmux behavior or
  while upgrading remote hosts; it has no Python requirement. tmux 3.2+
  remains the minimum for either view.
- Mirror panes retain their remote agent identity for seen tracking and urgent
  cycling, and are excluded from local agent detection. Looking at one mirror
  suppresses notifications only for that agent, rather than the whole host.

## 0.3.0 (2026-10-07)

- Desktop notifications for blocked and error agents use `normal` urgency, so
  they take your notification daemon's theme and timeout instead of the
  red, never-expiring `critical` style. `@tmux-agent-notify-urgency critical`
  restores it; `@tmux-agent-notify-timeout` sets the seconds. They now close
  when you look at the agent (focus, jump, answer, pane gone) and a new one
  replaces the open one, via `notify-send -p/-r` and a D-Bus close.
- Notifications and the picker say why an agent stopped: the command awaiting
  permission, the question, the first line of the final answer or the error.
  Hooks record it (needs jq); `status --json` and `detail --pane` show it.
- New `error` state (✖) for a turn that ended on an API error, from Claude
  Code's `StopFailure` hook. Re-run `install-hooks claude` to get it; the
  Claude Code plugin gets it on update.
- Picker actions: ctrl-y approve (only while still blocked), ctrl-n deny or
  interrupt, ctrl-e reply, ctrl-s mark seen, ctrl-d git diff preview, ctrl-l
  reload.
- Terminal notifications (OSC 9, 777 or kitty 99, by `TERM` or
  `@tmux-agent-osc`) that work over ssh and without a display.
  `@tmux-agent-notify-method` picks `auto`, `desktop`, `terminal` or `off`.
- `@tmux-agent-notify-command` runs your command on every notification, with
  the details in `TA_*` environment variables, for phone push through ntfy,
  Pushover or Telegram.
- `mobile` is a tap-friendly agent list and action menu for a phone terminal
  (Termius over ssh). `send --pane <id> --key|--text ...` types into an
  agent pane, a remote one over ssh, from a short allowlist of keys.
- `diff --pane <id>` shows the git diff of an agent's directory.
- Faster scans: one `ps` per scan and fewer pane captures.
- An interrupted Claude Code turn no longer shows as working for 5 minutes.
- Approve is trustworthy: `send --expect-state` forces a fresh scan and lets
  a newer hook report override the cache, and the picker refuses when a new
  permission request replaced the one you saw.
- `StopFailure` details come from Claude Code's `error` and `error_details`.
- `prefix + A` and `attach --urgent` include agents in error: blocked, then
  error, then ready.
- OpenCode: a pane showing several sessions follows all of them, and a failed
  run reports error instead of ready.
- Codex: `install-hooks codex` adds `Interrupt` (an interrupted turn reads
  ready), keeps `SessionEnd` and `Interrupt` within Codex's 3 s hook limit,
  and reminds you to trust the hooks in `/hooks`. Hooks are on by default, so
  it warns only when `config.toml` turns them off.
- `auto` notifications count a D-Bus session bus as a reachable desktop.
- Remote agents can be marked seen and show `detail`, and `wait` works on
  them (`<host-key>/<pane>` ids).
- State is per tmux server in a private (0700) directory, preferring
  `$XDG_RUNTIME_DIR`. It held prompt and command details in a shared,
  world-readable `/tmp/tmux-agent-<uid>`, which you can now delete.
- `mobile`'s listen port needs the same random key as the picker's, so other
  local users can't drive it, and it cleans up when interrupted.
- Much faster scans: agent detection no longer runs a grep per process name.
- `tmux-agent doctor` checks the install, and `explain --pane <id>` shows why
  a pane has its state.

Upgrading: re-run `install-hooks codex`, then trust the hooks in `/hooks` in
Codex.

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

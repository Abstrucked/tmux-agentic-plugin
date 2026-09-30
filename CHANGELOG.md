# Changelog

## Unreleased

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

# Changelog

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

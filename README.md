# tmux-agentic-plugin

See at a glance what your coding agents are doing across every tmux session,
and jump straight to the one that needs you.

![Agent strip in the tmux status bar: a blocked claude named, then counts of ready and idle agents](docs/strip.png)

It detects Claude Code, Codex, OpenCode, Gemini, Aider, Amp, Copilot and other
agent CLIs running in any pane, and tracks each one's state:

| Mark | State   | Meaning                                 |
|------|---------|-----------------------------------------|
| ◆    | blocked | needs input or approval (red)           |
| ●    | working | actively running (yellow)               |
| ◉    | ready   | finished, you have not looked yet (blue)|
| ○    | idle    | finished and seen (green, name dimmed)  |

- **Status bar strip**: one `mark name` per agent, most urgent first. Past
  four agents it switches to counts per state (`◆ codex  ●5  ○3`), and keeps
  blocked agents named while there are at most two.
- **Picker** (`prefix + a`): an fzf popup over every agent pane with a live
  preview. Enter switches to that pane, in any session.
- **Jump** (`prefix + A`): straight to the next blocked agent, then to
  finished ones you have not looked at. Press again to cycle through them.
- **Desktop notifications** when an agent you are not looking at gets
  blocked or finishes.
- **Exact states through agent hooks** (optional): Claude Code, Codex and
  OpenCode report their own lifecycle. Without hooks, state is inferred
  from pane output and CPU use.

![Agent picker: every agent pane across sessions with its state, and a live preview of the blocked one](docs/picker.png)

## Requirements

- tmux 3.2+ (`display-popup`)
- bash 4+ (on macOS: `brew install bash`)
- [fzf](https://github.com/junegunn/fzf) for the picker
- jq for `install-hooks` and `status --json`
- Linux or macOS. Notifications use `notify-send` or `osascript`.

## Install

### With [TPM](https://github.com/tmux-plugins/tpm)

```tmux
set -g @plugin 'Abstrucked/tmux-agentic-plugin'
set -g status-right '#{agent_status} %H:%M'   # where the strip goes
set -g focus-events on                        # lets focusing a pane mark it seen
```

Then press `prefix + I`.

### Manually

```sh
git clone https://github.com/Abstrucked/tmux-agentic-plugin ~/.tmux/plugins/tmux-agentic-plugin
```

```tmux
run-shell ~/.tmux/plugins/tmux-agentic-plugin/tmux-agentic.tmux
```

### Agent hooks (recommended)

Hooks make states exact, not inferred. Run this once, and again if you move
the plugin:

```sh
~/.tmux/plugins/tmux-agentic-plugin/bin/tmux-agent install-hooks            # all three
~/.tmux/plugins/tmux-agentic-plugin/bin/tmux-agent install-hooks claude     # or pick
```

| Agent       | What it changes                                                   |
|-------------|-------------------------------------------------------------------|
| Claude Code | merges hook entries into `~/.claude/settings.json`                |
| Codex       | merges hook entries into `~/.codex/hooks.json`; needs `hooks = true` under `[features]` in `~/.codex/config.toml` |
| OpenCode    | links `~/.config/opencode/plugins/tmux-agent` to this plugin      |

Re-running replaces earlier tmux-agent entries and leaves your other hooks
alone. `install-hooks --remove` takes them out again.

TPM installs to `~/.config/tmux/plugins/` instead of `~/.tmux/plugins/` when
your config lives in `~/.config/tmux`. Adjust the paths above to match.

## Options

| Option                        | Default       | Effect |
|-------------------------------|---------------|--------|
| `@tmux-agent-key`             | `a`           | picker key after the prefix; `off` for none |
| `@tmux-agent-urgent-key`      | `A`           | jump key after the prefix; `off` for none |
| `@tmux-agent-popup-size`      | `80%`         | picker popup width and height |
| `@tmux-agent-strip-position`  | `interpolate` | `interpolate`: replace `#{agent_status}` in `status-left`/`status-right`; `centre`: put the strip in the middle of the status bar; `off`: no strip |
| `@tmux-agent-strip-max`       | `4`           | agents shown by name before switching to counts |
| `@tmux-agent-notify`          | `on`          | desktop notifications; `off` to silence |

Set options before the `@plugin` line runs, that is, above TPM's `run` line.

## CLI

`bin/tmux-agent` is also usable on its own:

```
tmux-agent status [--json]     list agent panes and states
tmux-agent attach --next       jump to the most urgent agent pane
tmux-agent attach --urgent [--from %3]   next blocked/ready pane, after %3
tmux-agent wait --pane %3 --state ready [--timeout 600]
tmux-agent window-dot @1       rollup state dot, for window-status-format
tmux-agent pane-label %3       agent + state, for pane-border-format
```

Run `tmux-agent` with no arguments for the full list.

## Uninstall

1. `bin/tmux-agent install-hooks --remove`, if you installed hooks.
2. Remove the `@plugin` line and `#{agent_status}`, then press
   `prefix + alt + u` (TPM clean).

## Development

```sh
shellcheck bin/tmux-agent lib/engine.sh lib/install-hooks tmux-agentic.tmux
pytest tests
```

## License

MIT

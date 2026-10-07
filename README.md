# tmux-agentic-plugin

[![HOL Guard](https://img.shields.io/endpoint?url=https%3A%2F%2Fhol.org%2Fapi%2Fregistry%2Fbadges%2Fguard%2FAbstrucked%2Ftmux-agentic-plugin&style=flat-square)](https://hol.org/registry/plugins/abstrucked%2Ftmux-agentic-plugin)

See at a glance what your coding agents are doing across tmux sessions and
SSH hosts, and jump straight to the one that needs you.

![Agent strip in the tmux status bar: a blocked claude named, then counts of ready and idle agents](docs/strip.png)

It detects Claude Code, Codex, OpenCode, Gemini, Aider, Amp, Copilot and other
agent CLIs running in any pane, and tracks each one's state:

| Mark | State   | Meaning                                 |
|------|---------|-----------------------------------------|
| ◆    | blocked | needs input or approval (red)           |
| ✖    | error   | last turn ended on an API error (magenta) |
| ●    | working | actively running (yellow)               |
| ◉    | ready   | finished, you have not looked yet (blue)|
| ○    | idle    | finished and seen (green, name dimmed)  |

`error` is a turn that ended on an API error (rate limit, overload, billing),
reported by Claude Code's `StopFailure` hook. Like ready, it becomes idle once
you look at it.

- **Status bar strip**: one `mark name` per agent, most urgent first. Past
  four agents it switches to counts per state (`◆ codex  ● 5  ○ 3`), and keeps
  blocked agents named while there are at most two.
- **Picker** (`prefix + a`): an fzf popup over every agent pane with a live
  preview. Enter switches to that pane, in any session. It can also approve,
  deny, reply to and diff an agent without leaving the popup (see
  [Picker keys](#picker-keys)).
- **Jump** (`prefix + A`): straight to the next blocked agent, then ones in
  error, then finished ones you have not looked at. Press again to cycle
  through them.
- **Remote pane mirrors**: open an agent on an SSH host in the local
  `remote-agents` session, with one window per agent. Type into the agent
  without changing its remote layout or nesting another tmux session.
- **Notifications** when an agent you are not looking at gets blocked,
  hits an error or finishes: desktop, in your terminal, or through a command
  of your own such as a phone push. Click a desktop one to jump to that pane,
  with its terminal window raised on its workspace.
- **Why it stopped**: notifications and the picker show what the hook
  reported: the command awaiting permission, the question, the first line of
  the final answer, or the error.
- **Exact states through agent hooks** (optional): Claude Code, Codex and
  OpenCode report their own lifecycle. Without hooks, state is inferred
  from pane output and CPU use.

![Agent picker: every agent pane across sessions with its state, and a live preview of the blocked one](docs/picker.png)

## Requirements

- tmux 3.2+ (`display-popup`)
- bash 4+ (on macOS: `brew install bash`)
- Python 3.9+ on the local machine for the default remote pane mirror view
- [fzf](https://github.com/junegunn/fzf) for the picker. Its actions need any
  recent fzf; the error line in the picker header needs 0.40+; the
  auto-refreshing list needs 0.43+ and curl (otherwise press ctrl-l)
- jq for `install-hooks` and `status --json`. It also lets hooks record why
  an agent stopped (optional; without it everything else works)
- Linux or macOS. Desktop notifications use `notify-send` or `osascript`;
  clicking them works with `notify-send` (see
  [Clicking notifications](#clicking-notifications)). Terminal notifications
  need nothing but a terminal that supports them (see
  [Notifications](#notifications)).

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
| Codex       | merges hook entries into `~/.codex/hooks.json`; warns only if `[features] hooks = false` in `~/.codex/config.toml` |
| OpenCode    | links `~/.config/opencode/plugins/tmux-agent` to this plugin      |

Re-running replaces earlier tmux-agent entries and leaves your other hooks
alone. `install-hooks --remove` takes them out again.

Codex enables hooks by default. After `install-hooks codex`, Codex asks you
to review and trust the new hooks: open `/hooks` in Codex. Codex also
reports `Interrupt`, so an interrupted turn reads ready. If you installed
Codex hooks earlier, re-run `install-hooks codex` and trust them again.

OpenCode panes that show several sessions follow all of them: blocked if any
asks, error if any failed, working while any runs, ready when all are done.
A failed run shows as error.

Claude Code hooks now include `StopFailure`, which reports the `error` state.
If you installed hooks earlier, re-run `install-hooks claude` after updating.
The Claude Code plugin below gets it automatically.

TPM installs to `~/.config/tmux/plugins/` instead of `~/.tmux/plugins/` when
your config lives in `~/.config/tmux`. Adjust the paths above to match.

#### Claude Code hooks as a Claude Code plugin

As an alternative to `install-hooks claude`, inside Claude Code run:

```
/plugin marketplace add Abstrucked/tmux-agentic-plugin
/plugin install tmux-agentic-plugin@tmux-agentic
```

This only registers the Claude Code hooks. You still need the tmux plugin
installed with TPM or manually, for the strip and the picker. Use either this
or `install-hooks claude`; running both is harmless, since repeated hook
events with the same state are ignored. Codex and OpenCode hooks still come
from `install-hooks`.

### Picker keys

| Key      | Action |
|----------|--------|
| enter    | switch to the pane |
| ctrl-y   | approve: sends the agent's approve key (claude `1`, codex `y`); refused if the agent moved on, or if a new request replaced the one you saw |
| ctrl-n   | deny, or interrupt a working agent: sends Esc (claude, codex, opencode) |
| ctrl-e   | type a reply and send it with Enter |
| ctrl-s   | mark the agent seen |
| ctrl-d   | toggle the preview between the pane and a git diff of its directory |
| ctrl-l   | reload the list |

A refused or failed action shows its reason in the header (fzf 0.40+).

### Notifications

`@tmux-agent-notify-method` picks where they go:

| Method    | Effect |
|-----------|--------|
| `auto`    | desktop when a notification daemon and display are reachable (or on macOS), otherwise terminal |
| `desktop` | `notify-send` or `osascript` |
| `terminal` | ask every attached terminal to show it |
| `off`     | none, including the notify command |

`@tmux-agent-notify off` silences everything too.

**Desktop notifications** use `normal` urgency, so their colours come from your
notification daemon's theme (to restyle them, match `app-name tmux-agent` in
your dunst, mako, swaync or awesome rules). Set `@tmux-agent-notify-urgency
critical` to send blocked and error agents as `critical`: most daemons then
show them in red and keep them until dismissed. `@tmux-agent-notify-timeout`
(seconds) overrides how long they stay; empty uses the daemon's default.
A notification closes by itself when you look at the agent (focusing its
pane, jumping to it, or answering it so its state moves on), and a new one for
the same agent replaces the open one. This needs `notify-send` 0.8+ and
`gdbus`, `busctl` or `dbus-send`; the macOS and terminal methods and the notify
command cannot be closed.

**Terminal notifications** work over ssh and on macOS, with no display. The
plugin writes an escape sequence straight to each tmux client's terminal, so
no tmux passthrough setting is needed. The sequence is chosen from the
client's `TERM`: OSC 9 (iTerm2, Ghostty, WezTerm and the default), OSC 777
(foot, Konsole and other VTE terminals, rxvt) or kitty's OSC 99. Force one
with `@tmux-agent-osc` set to `9`, `777` or `99`.

**Notify command.** `@tmux-agent-notify-command` runs a command of yours in
addition to the method, through `bash -c`. The notification is passed only in
the environment, never in the command string:

| Variable     | Value |
|--------------|-------|
| `TA_TITLE`   | e.g. `claude needs input` |
| `TA_BODY`    | the detail line, then directory and tmux target; may hold text the agent wrote |
| `TA_STATE`   | `blocked`, `error` or `ready` |
| `TA_AGENT`   | agent name |
| `TA_PANE`    | pane id |
| `TA_PATH`    | the agent's working directory |
| `TA_URGENCY` | `critical` (blocked, error) or `normal` |

**Phone push.** Put the token in a small script, not in `tmux.conf`:

```sh
#!/usr/bin/env bash
# ~/.local/bin/agent-push (chmod 700); secrets in ~/.config/agent-push.env (chmod 600)
set -eu
. ~/.config/agent-push.env     # NTFY_TOPIC=..., PUSHOVER_TOKEN=..., PUSHOVER_USER=..., TG_TOKEN=..., TG_CHAT=...
prio=default; [[ $TA_URGENCY == critical ]] && prio=high

# ntfy
curl -s -m 10 -H "Title: $TA_TITLE" -H "Priority: $prio" --data-raw "$TA_BODY" "https://ntfy.sh/$NTFY_TOPIC"

# Pushover (priority 1 is high)
curl -s -m 10 --form-string "token=$PUSHOVER_TOKEN" --form-string "user=$PUSHOVER_USER" \
    --form-string "title=$TA_TITLE" --form-string "message=$TA_BODY" \
    --form-string "priority=$([[ $TA_URGENCY == critical ]] && echo 1 || echo 0)" \
    https://api.pushover.net/1/messages.json

# Telegram
curl -s -m 10 --data-urlencode "chat_id=$TG_CHAT" --data-urlencode "text=$TA_TITLE: $TA_BODY" \
    "https://api.telegram.org/bot$TG_TOKEN/sendMessage"
```

Keep one of the three blocks. Then:

```tmux
set -g @tmux-agent-notify-command '~/.local/bin/agent-push'
```

The curl flags matter. `TA_BODY` can hold text the agent wrote, and curl's
`-d "$X"` and `-F "k=$X"` read a file when the value starts with `@` (`<` for
`-F`), so an agent whose last message is `@~/.ssh/id_ed25519` would have that
file uploaded. `--data-raw` and `--form-string` never do, and a `text=` prefix
keeps `--data-urlencode` safe.

### Remote agents

Agents running in tmux on other machines appear next to local ones, named
`agent@host`: in the strip, in the picker (with a live preview) and for
`prefix + A`. When one gets blocked or finishes, you get a notification.
Marking seen, `detail` and `wait` work for them too, with `<host-key>/<pane>`
ids (`tmux-agent status --remote` shows them).

```tmux
set -g @tmux-agent-remotes 'devbox gpu-box'   # ssh aliases to always ask
```

On top of that list, any machine you have a live ssh ControlMaster
connection to is picked up by itself and dropped once the connection
closes. This needs a `ControlPath` of the form `<dir>/<prefix>%r@%h:%p`,
for example:

```ssh-config
Host *
    ControlMaster auto
    ControlPath ~/.ssh/master-%r@%h:%p
    ControlPersist 10m
```

How it works: each host runs this plugin itself. Every
`@tmux-agent-remote-interval` seconds, the status bar starts a background
`ssh host tmux-agent status --porcelain` for each host, reusing the
ControlMaster connection when there is one. Nothing listens on a port, and
your ssh keys are the only credentials. A host that can't be reached, or
whose answer is more than six intervals old, drops out; the local strip
never waits on ssh.

Jumping to a remote agent opens its live pane mirror in a local session named
`remote-agents`, with one reusable window per agent. The mirror sends terminal
snapshots over a persistent SSH control-mode connection, so the remote pane's
layout, size and running process stay as they are. The first jump creates the
window; later jumps reuse it. If the connection drops, the bridge retries
after 1, 2, 5 and 10 seconds without replaying input. Closing a mirror only
stops its bridge; it does not stop the remote pane or agent.

In the mirror, `Ctrl-]` followed by an arrow pans the view, `Ctrl-]` then
PageUp/PageDown scrolls history, and `Ctrl-]` then `0` returns to cursor-follow
mode. Press `Ctrl-]` twice to send a literal `Ctrl-]` to the remote pane. The local
`tmux-agent mirror --pane <host-key>/<pane>` and remote
`tmux-agent mirror-connect --pane <pane>` commands implement this bridge.

The default `@tmux-agent-remote-view mirror` needs Python 3.9+ locally and the
updated plugin on both hosts. To use the previous nested-tmux attach behavior,
set `@tmux-agent-remote-view attach`; it does not need Python and is useful
while remote hosts are still on an older plugin. Both modes retain the tmux
3.2+ minimum.

When upgrading, update the tmux plugin on the local machine and each remote
host, then reload each tmux config. Run `tmux-agent doctor` locally to check
Python and the selected remote view. A host with an older plugin can still
be detected, but cannot serve a mirror; use `attach` until it is updated.

Requirements on each remote host: tmux, bash 4+ and this plugin, installed
by TPM in the usual place. Otherwise, point `@tmux-agent-remote-command` at
its `bin/tmux-agent`. `tailscale ssh` opens no ControlMaster connection, so
list such hosts in `@tmux-agent-remotes`; plain `ssh` over the tailnet
works. `tmux-agent remotes` shows each host and how its last fetch went.

### From a phone

`tmux-agent mobile` is a tap-friendly list of every agent this host can see
(its own and the remote ones) with a live preview of the selected pane.
Tap an agent for a menu of big rows: Enter, `y`, `n`, `1`-`3`, Esc,
Ctrl-C, arrows, "Type a reply..." and "Open full session". Each key goes
to the agent's pane (`tmux-agent send`), remote ones over ssh, and the
preview shows what happened.

With Termius: install Tailscale on the phone, add the host by its tailnet
name (Tailscale SSH needs no keys), and set its startup command to
`~/.config/tmux/plugins/tmux-agentic-plugin/bin/tmux-agent mobile` (use
`~/.tmux/plugins/...` if that is where TPM put it). Turn on mouse support
so taps select rows. Any host works as the entry point, since each one
lists the others it can reach. Needs fzf; the list refreshes itself when
curl is installed too, otherwise Ctrl-R.

`send` accepts only agent panes and a short list of keys (Enter, Escape,
Tab, Space, BSpace, the arrows, Ctrl-C, `y n Y N` and digits); `--text`
is typed literally, never run.

### Clicking notifications

Clicking a notification switches a tmux client to that agent's session,
window and pane, then raises the terminal window the client runs in,
switching to its workspace or tag. Nothing to set up on most desktops.

The click needs a notification daemon that runs the default action on click
and libnotify 0.7.9+ (`notify-send --wait`). swaync, mako, awesome, GNOME and
KDE do out of the box. dunst closes the notification on a left click by
default; add this to `dunstrc`:

```ini
[global]
    mouse_left_click = do_action, close_current
```

Raising the window is detected from the tmux client's environment:

| Desktop                                  | How                                                          |
|------------------------------------------|--------------------------------------------------------------|
| Hyprland                                 | `hyprctl` (Lua configs through `eval`, older ones `dispatch`); needs jq |
| sway                                     | `swaymsg [pid=…] focus`                                      |
| awesome                                  | `awesome-client`, jumping to the client's tag                |
| other X11 window managers (i3, bspwm, xfwm, KDE/GNOME on X11) | `xdotool`, else `wmctrl`, whichever is installed |
| GNOME/KDE on Wayland, macOS              | not raised; the tmux jump still happens                      |

For anything else, set `@tmux-agent-raise-command` to your own command. It
gets the tmux client's PID as its last argument; the terminal owning the
window is one of its ancestors.

## Options

| Option                        | Default       | Effect |
|-------------------------------|---------------|--------|
| `@tmux-agent-key`             | `a`           | picker key after the prefix; `off` for none |
| `@tmux-agent-urgent-key`      | `A`           | jump key after the prefix; `off` for none |
| `@tmux-agent-popup-size`      | `80%`         | picker popup width and height |
| `@tmux-agent-strip-position`  | `interpolate` | `interpolate`: replace `#{agent_status}` in `status-left`/`status-right`; `centre`: put the strip in the middle of the status bar; `off`: no strip |
| `@tmux-agent-strip-max`       | `4`           | agents shown by name before switching to counts |
| `@tmux-agent-notify`          | `on`          | notifications; `off` to silence all |
| `@tmux-agent-notify-method`   | `auto`        | `auto`, `desktop`, `terminal` or `off` (see [Notifications](#notifications)) |
| `@tmux-agent-notify-urgency`  | `normal`      | desktop urgency for blocked and error agents; `critical` is red and sticky in most daemons |
| `@tmux-agent-notify-timeout`  | empty         | seconds a desktop notification stays; empty is the daemon's default |
| `@tmux-agent-osc`             | from `TERM`   | force the terminal notification sequence: `9`, `777` or `99` |
| `@tmux-agent-notify-command`  | empty         | command run on every notification, with `TA_*` variables; for phone push |
| `@tmux-agent-raise-command`   | detected      | raises the terminal window when a notification is clicked; a command (gets the client PID) or `off` |
| `@tmux-agent-remotes`         | empty         | ssh aliases whose agents to show (see [Remote agents](#remote-agents)) |
| `@tmux-agent-remote-view`     | `mirror`      | remote pane view: `mirror`, or legacy nested-tmux `attach` |
| `@tmux-agent-remote-discover` | `on`          | also ask hosts with a live ssh ControlMaster connection; `off` for the list only |
| `@tmux-agent-remote-tailscale` | `on`         | also ask every online Linux/macOS tailnet peer (needs `tailscale` and `jq`); hosts without tmux-agent are retried every 5 minutes and show as `noplugin` in `tmux-agent remotes` |
| `@tmux-agent-remote-watch`    | `on`          | keep a `tmux-agent watch` running over ssh to each host that has one, so changes show within a second; hosts without it (older plugin) are polled every interval |
| `@tmux-agent-remote-interval` | `10`          | seconds between fetches from remote hosts |
| `@tmux-agent-ssh-sockets`     | `~/.ssh/master-*` | glob matching your ControlMaster sockets |
| `@tmux-agent-remote-command`  | TPM paths     | command that runs `tmux-agent` on a remote host |

Set options before the `@plugin` line runs, that is, above TPM's `run` line.

## CLI

`bin/tmux-agent` is also usable on its own:

```
tmux-agent status [--json] [--remote]   list agent panes and states;
                               --remote adds agents on other hosts;
                               --json includes each agent's detail
tmux-agent status --porcelain  this host's agents, for other hosts to fetch
tmux-agent remotes             remote hosts and their last fetch
tmux-agent attach --next       jump to the most urgent agent pane
tmux-agent attach --urgent [--from %3]   next blocked, error, then ready pane,
                               after %3
tmux-agent focus --pane %3     switch a client to %3 and raise its terminal
tmux-agent send --pane %3 (--key K | --text T)... [--expect-state blocked]
                               type into an agent pane; exits 3 without
                               typing unless it is in that state
tmux-agent mobile              tap-friendly agent list for a phone terminal
tmux-agent detail --pane %3    why the agent stopped, as its hook reported
tmux-agent doctor              check the install; exits 1 on any FAIL
tmux-agent explain --pane %3   why %3 shows its state
tmux-agent diff --pane %3      git diff of the pane's directory
tmux-agent wait --pane %3 --state ready [--timeout 600]
tmux-agent window-dot @1       rollup state dot, for window-status-format
tmux-agent pane-label %3       agent + state, for pane-border-format
```

Run `tmux-agent` with no arguments for the full list.

## Troubleshooting

Run `tmux-agent doctor`. It checks the tools and their versions, the state
directory, the Claude Code, Codex and OpenCode hooks (missing or stale
entries, Codex's `config.toml` and trust), the notification setup and the
remote hosts. Each line is `OK`, `WARN`, `FAIL` or `INFO`; it exits 1 if any
is `FAIL`.

When a pane shows the wrong state, run `tmux-agent explain --pane %3` (ids
from `status`; `<host-key>/%3` for a remote one). It prints the agent found,
the hook report and its age, the stored detail, the cached state, when you
last saw it, and what the heuristics (pane output and CPU) conclude, then the
final state and whether it came from the hook report or the heuristics.

## State location

State is kept per tmux server in a private directory (mode 0700, owned by
you): `$XDG_RUNTIME_DIR/tmux-agent-<uid>/<server>`, or
`${TMPDIR:-/tmp}/tmux-agent-<uid>/<server>` without `XDG_RUNTIME_DIR`. If
that parent is not a private directory of yours, it falls back to
`${XDG_CACHE_HOME:-~/.cache}/tmux-agent/<server>`. `TMUX_AGENT_STATE_DIR`
overrides all of this. Earlier versions shared one `/tmp/tmux-agent-<uid>`
between servers, readable by other users; you can delete it.

## Uninstall

1. `bin/tmux-agent install-hooks --remove`, if you installed hooks.
2. Remove the `@plugin` line and `#{agent_status}`, then press
   `prefix + alt + u` (TPM clean).

## Development

```sh
shellcheck bin/tmux-agent lib/engine.sh lib/remote.sh lib/mirror.sh lib/notify.sh lib/pick.sh lib/doctor.sh lib/install-hooks lib/raise tmux-agentic.tmux
pytest tests
```

CI runs these checks on Linux and macOS. HOL Guard runs separately on pull
requests, pushes to `main`, weekly and manual dispatches, using the pinned
workflow in [.github/workflows/guarded-repository.yml](.github/workflows/guarded-repository.yml).
Its scanner uses `strict-security` and fails on high or critical findings.
SARIF upload, signed provenance and public verification registration run in
GitHub Actions; a local scanner run checks the code without refreshing the
published badge.

## Privacy

The plugin has no telemetry and sends nothing to the author or any third
party. Remote agents are fetched over your own ssh, from the hosts you list,
live ControlMaster connections and, unless `@tmux-agent-remote-tailscale` is
`off`, your tailnet peers. See [PRIVACY.md](PRIVACY.md).

## License

MIT

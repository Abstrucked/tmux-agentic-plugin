# Security policy

## Reporting a vulnerability

Please report security issues privately through GitHub's
[private vulnerability reporting](https://github.com/Abstrucked/tmux-agentic-plugin/security/advisories/new)
instead of opening a public issue. Expect a first reply within a week.

## What this plugin touches

- `tmux-agent install-hooks` edits the hook settings of the agents you ask
  it to (`~/.claude/settings.json`, Codex hooks, OpenCode config) and
  `install-hooks --remove` undoes it. Nothing is changed until you run it.
- Remote agents are queried over your own ssh connections; the plugin
  listens on no port and stores no credentials.

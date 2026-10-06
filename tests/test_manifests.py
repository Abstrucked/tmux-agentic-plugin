"""Run with: pytest tests.

Checks that the Claude Code / Codex plugin manifests agree with each other
and with lib/install-hooks.
"""

import json
import re
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(rel):
    return json.loads((ROOT / rel).read_text())


def claude_events():
    text = (ROOT / "lib" / "install-hooks").read_text()
    raw = re.search(r"CLAUDE_EVENTS='([^']*)'", text).group(1)
    events = {}
    for spec in raw.split():
        event, _, matcher = spec.partition("=")
        events[event] = matcher or None
    return events


class ManifestTests(unittest.TestCase):
    def test_hooks_match_install_hooks(self):
        hooks = load("hooks/hooks.json")["hooks"]
        expected = claude_events()
        self.assertEqual(set(hooks), set(expected))
        for event, matcher in expected.items():
            (entry,) = hooks[event]
            self.assertEqual(entry.get("matcher"), matcher, event)
            (hook,) = entry["hooks"]
            self.assertEqual(hook["type"], "command")
            self.assertEqual(
                hook["command"],
                f'"${{CLAUDE_PLUGIN_ROOT}}/bin/tmux-agent" hook claude {event}',
            )

    def test_versions_agree(self):
        market = load(".claude-plugin/marketplace.json")["plugins"][0]
        versions = {
            load(".claude-plugin/plugin.json")["version"],
            load(".codex-plugin/plugin.json")["version"],
            market["version"],
        }
        self.assertEqual(len(versions), 1, versions)
        self.assertRegex(versions.pop(), r"^\d+\.\d+\.\d+$")

    def test_codex_interface_paths_exist(self):
        iface = load(".codex-plugin/plugin.json")["interface"]
        paths = [iface["composerIcon"], iface["logo"], *iface["screenshots"]]
        for rel in paths:
            self.assertTrue(rel.startswith("./"), rel)
            self.assertTrue((ROOT / rel).is_file(), rel)


if __name__ == "__main__":
    unittest.main()

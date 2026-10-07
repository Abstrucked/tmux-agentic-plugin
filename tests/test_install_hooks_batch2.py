import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "bin" / "tmux-agent"

FOREIGN = {"hooks": {"SessionStart": [{"hooks": [
    {"type": "command", "command": "/x/herdr-agent-state.sh"}]}]}}


class CodexInstallHooksTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ta-home-")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.hooks = self.home / ".codex" / "hooks.json"
        self.hooks.parent.mkdir()

    def run_cli(self, *args):
        r = subprocess.run([str(CLI), "install-hooks", *args],
                           capture_output=True, text=True,
                           env={**os.environ, "HOME": str(self.home)})
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def data(self):
        return json.loads(self.hooks.read_text())["hooks"]

    def test_timeouts_per_event(self):
        self.run_cli("codex")
        hooks = self.data()
        self.assertIn("Interrupt", hooks)
        for event, entries in hooks.items():
            want = 3 if event in ("SessionEnd", "Interrupt") else 5
            self.assertEqual([h["timeout"] for e in entries for h in e["hooks"]],
                             [want], event)

    def test_foreign_hook_survives_and_rerun_is_idempotent(self):
        self.hooks.write_text(json.dumps(FOREIGN))
        self.run_cli("codex")
        first = self.hooks.read_text()
        self.run_cli("codex")
        self.assertEqual(self.hooks.read_text(), first)
        cmds = [h["command"] for e in self.data()["SessionStart"] for h in e["hooks"]]
        self.assertEqual(len(cmds), 2)
        self.assertEqual(cmds[0], "/x/herdr-agent-state.sh")

    def test_remove_drops_only_ours(self):
        self.hooks.write_text(json.dumps(FOREIGN))
        self.run_cli("codex")
        r = self.run_cli("--remove", "codex")
        self.assertEqual(json.loads(self.hooks.read_text()), FOREIGN)
        self.assertNotIn("trust", r.stdout + r.stderr)

    def test_disabled_warning_only_with_hooks_false(self):
        config = self.home / ".codex" / "config.toml"
        self.assertNotIn("disabled", self.run_cli("codex").stderr)
        config.write_text("[features]\nhooks = true\n")
        self.assertNotIn("disabled", self.run_cli("codex").stderr)
        config.write_text("[other]\nhooks = false\n")
        self.assertNotIn("disabled", self.run_cli("codex").stderr)
        config.write_text("[features]\nhooks = false\n")
        self.assertIn("hooks are disabled in", self.run_cli("codex").stderr)

    def test_trust_reminder_on_install(self):
        r = self.run_cli("codex")
        self.assertIn("open /hooks in Codex", r.stdout)

    def test_claude_output_unchanged(self):
        self.run_cli("claude")
        data = json.loads((self.home / ".claude" / "settings.json").read_text())["hooks"]
        self.assertEqual(list(data), [
            "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
            "PermissionRequest", "Notification", "Stop", "StopFailure", "SessionEnd"])
        for event, entries in data.items():
            self.assertEqual(len(entries), 1)
            hook = entries[0]["hooks"][0]
            self.assertEqual(hook["timeout"], 5)
            self.assertTrue(hook["command"].endswith(f"tmux-agent hook claude {event}"))
        self.assertEqual(data["Notification"][0]["matcher"],
                         "permission_prompt|elicitation_dialog")
        self.assertEqual([e for e, v in data.items() if "matcher" in v[0]],
                         ["Notification"])

    def test_symlinked_hooks_file_stays_symlink(self):
        target = self.home / "real.json"
        target.write_text("{}")
        self.hooks.symlink_to(target)
        self.run_cli("codex")
        self.assertTrue(self.hooks.is_symlink())
        self.assertIn("Interrupt", json.loads(target.read_text())["hooks"])


if __name__ == "__main__":
    unittest.main()

"""Engine helpers added in batch 2: hook mappings, server key, detail epoch."""

import tempfile
import unittest
from pathlib import Path

from test_tmux_agent import bash, fake_tmux


def state(event):
    return bash('ta_hook_state "$1"', event).stdout.strip()


class HookStateTests(unittest.TestCase):
    def test_failed_is_error(self):
        self.assertEqual(state("session.execution.failed"), "error")

    def test_interrupt_is_ready(self):
        self.assertEqual(state("Interrupt"), "ready")

    def test_unchanged(self):
        for ev, want in [("Stop", "ready"), ("StopFailure", "error"),
                         ("session.execution.succeeded", "ready"),
                         ("session.execution.interrupted", "ready"),
                         ("PreToolUse", "working"), ("SessionStart", "idle"),
                         ("PermissionRequest", "blocked"), ("SessionEnd", "end"),
                         ("Bogus", "")]:
            self.assertEqual(state(ev), want, ev)


class ServerKeyTests(unittest.TestCase):
    def key(self, tmux, env=None):
        r = bash('ta_server_key', env={"TMUX": tmux, **(env or {})})
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_default_socket(self):
        self.assertEqual(self.key("/tmp/tmux-1000/default,123,0"), "default")

    def test_named_socket(self):
        self.assertEqual(self.key("/tmp/tmux-1000/work,123,0"), "work")

    def test_odd_chars_sanitized(self):
        self.assertEqual(self.key("/tmp/tmux 1000/my sock$x,1,0"), "my_sock_x")

    def test_unset_uses_tmux_display_message(self):
        with tempfile.TemporaryDirectory() as d:
            fake_tmux(d, {"display-message -p #{socket_path}": "/tmp/tmux-1000/alt\n"})
            r = bash('unset TMUX; ta_server_key', env={"PATH": f"{d}:/usr/bin:/bin"})
            self.assertEqual(r.stdout.strip(), "alt")

    def test_no_tmux_is_default(self):
        with tempfile.TemporaryDirectory() as d:
            r = bash('unset TMUX; PATH="$1"; ta_server_key', d)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), "default")


class DetailEpochTests(unittest.TestCase):
    def epoch(self, content):
        with tempfile.TemporaryDirectory() as d:
            if content is not None:
                (Path(d) / "detail-%1").write_text(content)
            return bash('ta_detail_epoch "$1" %1', d)

    def test_present(self):
        r = self.epoch("1700000000|Stop|hello\n")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "1700000000"))

    def test_missing_and_empty(self):
        for c in (None, ""):
            r = self.epoch(c)
            self.assertEqual((r.returncode, r.stdout), (1, ""))

    def test_garbage(self):
        for c in ("abc|Stop|x\n", "|Stop|x\n", "12x|a|b\n"):
            r = self.epoch(c)
            self.assertEqual((r.returncode, r.stdout), (1, ""), c)


if __name__ == "__main__":
    unittest.main()

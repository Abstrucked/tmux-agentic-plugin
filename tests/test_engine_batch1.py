"""Engine helpers added in batch 1: error state, ps snapshot, sanitising."""

import os
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from test_tmux_agent import bash

ALLOWED = {"Enter", "Escape", "Tab", "Space", "BSpace", "Up", "Down", "Left",
           "Right", "C-c"}


def key_ok(key):
    return key in ALLOWED or re.fullmatch(r"[ynYN0-9]", key) is not None


class ErrorStateTests(unittest.TestCase):
    def test_states_order(self):
        self.assertEqual(bash('echo "$TA_STATES"').stdout.strip(),
                         "blocked error working ready idle")

    def test_rank(self):
        r = bash('for s in $TA_STATES bogus; do ta_state_rank $s; done')
        self.assertEqual(r.stdout.split(), ["0", "1", "2", "3", "4", "5"])

    def test_color_ansi_char(self):
        r = bash('ta_state_color error; ta_state_ansi error; echo; ta_state_char error')
        self.assertEqual(r.stdout, "magenta\n\033[35m\n✖\n")

    def test_unchanged_mappings(self):
        r = bash('ta_state_color blocked; ta_state_char blocked; ta_state_char idle')
        self.assertEqual(r.stdout.split(), ["red", "◆", "○"])

    def test_rollup_prefers_blocked_then_error(self):
        self.assertEqual(bash('ta_rollup working error ready').stdout.strip(), "error")
        self.assertEqual(bash('ta_rollup error blocked').stdout.strip(), "blocked")

    def test_stop_failure(self):
        self.assertEqual(bash('ta_hook_state StopFailure').stdout.strip(), "error")
        self.assertEqual(bash('ta_hook_state Stop').stdout.strip(), "ready")


class SnapshotTests(unittest.TestCase):
    def tearDown(self):
        for proc in getattr(self, "procs", []):
            proc.kill()
            proc.wait()

    def spawn(self, script):
        proc = subprocess.Popen(["bash", "-c", script])
        self.procs = getattr(self, "procs", [])
        self.procs.append(proc)
        time.sleep(0.2)
        return proc

    def test_detect_after_snapshot(self):
        proc = self.spawn('bash -c "exec -a codex sleep 300"; sleep 300')
        r = bash('ta_ps_snapshot; ta_detect_agent "$1"', str(proc.pid))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split()[0], "codex")

    def test_snapshot_from_inside_function(self):
        proc = self.spawn("exec -a claude sleep 300")
        r = bash('f() { ta_ps_snapshot; }; f; ta_detect_agent "$1"', str(proc.pid))
        self.assertEqual(r.stdout.split(), ["claude", str(proc.pid)])

    def test_one_ps_call_for_many_panes(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "ps.log"
            ps = Path(d) / "ps"
            ps.write_text(f'#!/bin/sh\necho x >>"{log}"\n'
                          'echo "10 1 bash bash"\necho "11 10 claude claude"\n')
            ps.chmod(0o755)
            env = {"PATH": f"{d}:{os.environ['PATH']}"}
            r = bash('ta_ps_snapshot; ta_detect_agent 10; ta_detect_agent 11; '
                     'ta_detect_agent 99 || true', env=env)
            self.assertEqual(r.stdout.split(), ["claude", "11", "claude", "11"], r.stderr)
            self.assertEqual(len(log.read_text().splitlines()), 1)


class InterruptedTests(unittest.TestCase):
    def match(self, text):
        return bash('ta_match_interrupted "$1"', text).returncode == 0

    def test_positive(self):
        self.assertTrue(self.match("  ⎿  Interrupted by user"))
        self.assertTrue(self.match("Interrupted · What should Claude do instead?"))

    def test_negative(self):
        self.assertFalse(self.match("interrupted by user"))
        self.assertFalse(self.match("KeyboardInterrupt"))
        self.assertFalse(self.match("Interrupted."))
        self.assertFalse(self.match("all good"))


class KeyTests(unittest.TestCase):
    def test_approve(self):
        self.assertEqual(bash('ta_approve_key claude').stdout, "1\n")
        self.assertEqual(bash('ta_approve_key codex').stdout, "y\n")
        for agent in ("opencode", "gemini", ""):
            r = bash('ta_approve_key "$1"', agent)
            self.assertEqual((r.returncode, r.stdout), (1, ""))

    def test_deny(self):
        for agent in ("claude", "codex", "opencode"):
            self.assertEqual(bash('ta_deny_key "$1"', agent).stdout, "Escape\n")
        r = bash('ta_deny_key gemini')
        self.assertEqual((r.returncode, r.stdout), (1, ""))

    def test_keys_pass_allowlist(self):
        for fn in ("ta_approve_key", "ta_deny_key"):
            for agent in ("claude", "codex", "opencode", "gemini"):
                r = bash(f'{fn} "$1" || true', agent)
                if r.stdout.strip():
                    self.assertTrue(key_ok(r.stdout.strip()), (fn, agent))


class SanitizeTests(unittest.TestCase):
    def san(self, text, *max_):
        r = bash('ta_sanitize "$@"', text, *max_)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.endswith("\n"))
        return r.stdout[:-1]

    def test_escape_and_bel(self):
        out = self.san("a\x1b[31mred\x07b\x7f")
        self.assertNotRegex(out, r"[\x00-\x1f\x7f]")
        self.assertEqual(out, "a[31mredb")

    def test_whitespace_controls(self):
        self.assertEqual(self.san("a\nb\tc\r\nd"), "a b c d")

    def test_pipe(self):
        self.assertEqual(self.san("a|b"), "a¦b")

    def test_collapse_and_trim(self):
        self.assertEqual(self.san("  a   b \n\n c  "), "a b c")

    def test_truncate(self):
        out = self.san("x" * 50, "10")
        self.assertEqual(out, "x" * 9 + "…")
        self.assertEqual(self.san("short", "10"), "short")
        self.assertEqual(len(self.san("y" * 500)), 200)

    def test_utf8(self):
        self.assertEqual(self.san("é日本😀 ok"), "é日本😀 ok")
        out = self.san("日本語😀é" * 5, "6")
        self.assertEqual(out, "日本語😀é" + "…")

    def test_empty(self):
        self.assertEqual(self.san(""), "")
        self.assertEqual(self.san(" \n\t "), "")


class DetailReadTests(unittest.TestCase):
    def test_present(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "detail-%3").write_text("1700000000|Notification|needs your OK\n")
            r = bash('ta_detail_read "$1" %3', d)
            self.assertEqual(r.stdout, "needs your OK\n")

    def test_missing_or_empty(self):
        with tempfile.TemporaryDirectory() as d:
            r = bash('ta_detail_read "$1" %3', d)
            self.assertEqual((r.returncode, r.stdout), (1, ""))
            Path(d, "detail-%3").write_text("")
            r = bash('ta_detail_read "$1" %3', d)
            self.assertEqual((r.returncode, r.stdout), (1, ""))


if __name__ == "__main__":
    unittest.main()

"""Run with: pytest tests/test_doctor.py

`doctor` and `explain` of lib/doctor.sh through bin/tmux-agent, with HOME,
the state dir, tmux and ssh replaced by fixtures.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from test_tmux_agent import CLI, ROOT, bash, fake_ssh, fake_tmux

DOCTOR = ROOT / "lib" / "doctor.sh"


def events(var):
    raw = re.search(rf"{var}='([^']*)'", (ROOT / "lib" / "install-hooks").read_text()).group(1)
    return [spec.partition("=")[0].partition("@")[0] for spec in raw.split()]


def hooks_json(bin_path, agent, skip=()):
    return {"hooks": {ev: [{"hooks": [{"type": "command", "timeout": 5,
                                       "command": f"{bin_path} hook {agent} {ev}"}]}]
                      for ev in events("CLAUDE_EVENTS" if agent == "claude" else "CODEX_EVENTS")
                      if ev not in skip}}


class DoctorBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.home = self.dir / "home"
        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".codex").mkdir()
        self.state = self.dir / "state"
        self.state.mkdir(mode=0o700)
        self.fake = self.dir / "bin"
        self.fake.mkdir()
        self.replies = {}
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "TMUX_AGENT_STATE_DIR": str(self.state),
            "TMUX_AGENT_QUIET": "1",
            "TMUX_AGENT_NOTIFY_METHOD": "terminal",
            "PATH": f"{self.fake}:{os.environ['PATH']}",
        }
        self.ssh_log = fake_ssh(self.fake)

    def cli(self, *args, **env):
        self.tmux_log = fake_tmux(self.fake, self.replies)
        return subprocess.run([str(CLI), *args], capture_output=True, text=True,
                              env={**self.env, **env}, timeout=30)

    def write(self, rel, data):
        p = self.home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data if isinstance(data, str) else json.dumps(data))
        return p


class DoctorTests(DoctorBase):
    def setUp(self):
        super().setUp()
        self.replies = {"-V": "tmux 3.4"}
        self.bin = os.path.realpath(CLI)

    def line(self, out, check):
        return next((ln for ln in out.splitlines() if re.match(rf"\w+\s+{re.escape(check)}\s", ln)), "")

    def python(self, version):
        executable = self.fake / "python3"
        executable.write_text(f"#!/bin/sh\nprintf '%s\\n' 'Python {version}'\n")
        executable.chmod(0o755)
        return executable

    def path_without_python(self):
        path = self.dir / "minimal-bin"
        path.mkdir()
        # Keep the commands doctor needs while ensuring command -v cannot
        # discover the host's Python installation.
        for name in ("bash", "env", "readlink", "dirname", "sed", "grep",
                     "awk", "stat", "date", "id"):
            source = shutil.which(name)
            if source:
                (path / name).symlink_to(source)
        (path / "tmux").symlink_to(self.fake / "tmux")
        (path / "ssh").symlink_to(self.fake / "ssh")
        return str(path)

    def test_version_compare(self):
        r = bash(f'. "{DOCTOR}"; '
                 'doc_ver_ge "tmux 3.3a" 3.2 && echo a; doc_ver_ge "tmux next-3.4" 3.2 && echo b; '
                 'doc_ver_ge "tmux 3.0" 3.2 || echo c; doc_ver_ge 0.9 0.40 || echo d; '
                 'doc_ver_ge 0.44.1 0.43 && echo e; doc_ver_ge "tmux 10.1" 3.2 && echo f; '
                 'doc_ver_ge junk 1.0 || echo g')
        self.assertEqual(r.stdout.split(), list("abcdefg"), r.stderr)

    def test_full_claude_hooks_ok(self):
        self.write(".claude/settings.json", hooks_json(self.bin, "claude"))
        r = self.cli("doctor")
        self.assertTrue(self.line(r.stdout, "claude hooks").startswith("OK"), r.stdout + r.stderr)

    def test_missing_stopfailure_warns(self):
        self.write(".claude/settings.json", hooks_json(self.bin, "claude", skip=("StopFailure",)))
        r = self.cli("doctor")
        ln = self.line(r.stdout, "claude hooks")
        self.assertTrue(ln.startswith("WARN") and "StopFailure" in ln and "install-hooks claude" in ln, r.stdout)
        self.assertEqual(r.returncode, 0)

    def test_other_path_is_stale(self):
        self.write(".claude/settings.json", hooks_json("/opt/old/bin/tmux-agent", "claude"))
        r = self.cli("doctor")
        ln = self.line(r.stdout, "claude hooks")
        self.assertTrue(ln.startswith("WARN") and "/opt/old/bin/tmux-agent" in ln, r.stdout)

    def test_no_hooks_warns_heuristics(self):
        r = self.cli("doctor")
        ln = self.line(r.stdout, "claude hooks")
        self.assertTrue(ln.startswith("WARN") and "heuristics" in ln, r.stdout)

    def test_codex_hooks_ok_and_trust_reminder(self):
        self.write(".codex/hooks.json", hooks_json(self.bin, "codex"))
        r = self.cli("doctor")
        self.assertTrue(self.line(r.stdout, "codex hooks").startswith("OK"), r.stdout)
        self.assertIn("/hooks", self.line(r.stdout, "codex trust"))

    def test_codex_hooks_disabled_warns(self):
        self.write(".codex/hooks.json", hooks_json(self.bin, "codex"))
        self.write(".codex/config.toml", "[features]\nhooks = false\n")
        r = self.cli("doctor")
        self.assertTrue(self.line(r.stdout, "codex config").startswith("WARN"), r.stdout)

    def test_opencode_symlink(self):
        plugins = self.home / ".config" / "opencode" / "plugins"
        plugins.mkdir(parents=True)
        (plugins / "tmux-agent").symlink_to(ROOT / "opencode")
        self.assertTrue(self.line(self.cli("doctor").stdout, "opencode").startswith("OK"))
        (plugins / "tmux-agent").unlink()
        (plugins / "tmux-agent").symlink_to(self.dir)
        self.assertTrue(self.line(self.cli("doctor").stdout, "opencode").startswith("WARN"))

    def test_old_tmux_fails_with_exit_1(self):
        self.replies = {"-V": "tmux 3.0"}
        r = self.cli("doctor")
        self.assertTrue(self.line(r.stdout, "tmux").startswith("FAIL"), r.stdout)
        self.assertEqual(r.returncode, 1)

    def test_healthy_exit_zero(self):
        self.assertEqual(self.cli("doctor").returncode, 0)

    def test_mirror_view_accepts_python_39_or_newer(self):
        self.python("3.11.6")
        r = self.cli("doctor", TMUX_AGENT_REMOTE_VIEW="mirror")
        self.assertTrue(self.line(r.stdout, "remote view").startswith("OK"), r.stdout)
        self.assertIn("Python 3.11.6", self.line(r.stdout, "python3"))

    def test_mirror_view_fails_when_python_is_missing(self):
        r = self.cli("doctor", TMUX_AGENT_REMOTE_VIEW="mirror", PATH=self.path_without_python())
        self.assertIn("not found", self.line(r.stdout, "python3"))
        self.assertTrue(self.line(r.stdout, "python3").startswith("FAIL"), r.stdout)
        self.assertEqual(r.returncode, 1, r.stdout)

    def test_mirror_view_fails_with_old_python(self):
        self.python("3.8.18")
        r = self.cli("doctor", TMUX_AGENT_REMOTE_VIEW="mirror")
        self.assertTrue(self.line(r.stdout, "python3").startswith("FAIL"), r.stdout)
        self.assertIn("3.9 or newer", self.line(r.stdout, "python3"))
        self.assertEqual(r.returncode, 1, r.stdout)

    def test_attach_view_does_not_require_python(self):
        r = self.cli("doctor", TMUX_AGENT_REMOTE_VIEW="attach", PATH=self.path_without_python())
        self.assertIn("Python is not required", self.line(r.stdout, "remote view"))
        self.assertEqual(self.line(r.stdout, "python3"), "")
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_unknown_remote_view_fails_clearly(self):
        r = self.cli("doctor", TMUX_AGENT_REMOTE_VIEW="window")
        self.assertTrue(self.line(r.stdout, "remote view").startswith("FAIL"), r.stdout)
        self.assertIn("expected mirror or attach", self.line(r.stdout, "remote view"))
        self.assertEqual(r.returncode, 1, r.stdout)

    def test_notify_does_not_leak_command(self):
        r = self.cli("doctor", TMUX_AGENT_NOTIFY_COMMAND="echo SECRETCMD")
        self.assertIn("set", self.line(r.stdout, "notify cmd"))
        self.assertNotIn("SECRETCMD", r.stdout)

    def test_client_osc_kind(self):
        self.replies["list-clients -F #{client_tty}|#{client_termname}"] = "/dev/pts/9|xterm-kitty\n"
        r = self.cli("doctor")
        self.assertIn("OSC 99", self.line(r.stdout, "notify client"))

    def test_remote_problems_from_cache_without_ssh(self):
        (self.state / "remotes").write_text("k1|devbox|\nk2|other|\n")
        (self.state / "remote-k1.meta").write_text(f"noplugin|{int(time.time())}\n")
        (self.state / "remote-k2.meta").write_text(f"ok|{int(time.time())}\n")
        r = self.cli("doctor")
        ln = self.line(r.stdout, "remotes")
        self.assertTrue(ln.startswith("WARN") and "devbox (noplugin)" in ln and "other" not in ln, r.stdout)
        self.assertFalse(self.ssh_log.exists())

    def test_state_dir_mode_warns(self):
        self.state.chmod(0o755)
        r = self.cli("doctor")
        self.assertTrue(self.line(r.stdout, "state dir").startswith("WARN"), r.stdout)


class ExplainTests(DoctorBase):
    PANE = "%7"

    def setUp(self):
        super().setUp()
        # An "agent": a process whose argv names claude.
        self.proc = subprocess.Popen(["bash", "-c", "exec -a claude sleep 60"])
        self.addCleanup(self.proc.wait)
        self.addCleanup(self.proc.kill)
        time.sleep(0.2)
        self.now = int(time.time())
        fmt = "#{pane_id}|#{pane_pid}|#{window_id}|#{session_name}:#{window_index}.#{pane_index}|#{pane_current_path}"
        self.replies = {
            f"list-panes -a -F {fmt}": f"{self.PANE}|{self.proc.pid}|@1|main:1.1|/src/app\n",
            f"capture-pane -p -J -S -30 -t {self.PANE}": "building...\nall quiet here\n",
        }

    def report(self, state, age=10):
        (self.state / f"report-{self.PANE}").write_text(f"{state}|{self.now - age}\n")

    def explain(self, pane=None):
        return self.cli("explain", "--pane", pane or self.PANE)

    def test_authoritative_report(self):
        self.report("blocked")
        r = self.explain()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("source     hook report", r.stdout)
        self.assertIn("authoritative=yes", r.stdout)
        self.assertRegex(r.stdout, r"agent\s+claude \(pid \d+\)")
        self.assertRegex(r.stdout, r"state\s+blocked")
        self.assertIn("main:1.1  /src/app", r.stdout)

    def test_stale_working_report_falls_back(self):
        self.report("working", age=1000)
        r = self.explain()
        self.assertIn("authoritative=no", r.stdout)
        self.assertIn("source     heuristics", r.stdout)

    def test_no_report_uses_heuristics(self):
        r = self.explain()
        self.assertIn("report     none", r.stdout)
        self.assertIn("source     heuristics", r.stdout)
        self.assertIn("ta_classify ->", r.stdout)
        self.assertIn("all quiet here", r.stdout)

    def test_seen_after_ready_is_idle(self):
        self.report("ready", age=30)
        (self.state / f"seen-{self.PANE}").write_text(f"{self.now - 5}\n")
        r = self.explain()
        self.assertIn("source     seen → idle", r.stdout)
        self.assertRegex(r.stdout, r"state\s+idle")

    def test_detail_and_cache_shown(self):
        self.report("error")
        (self.state / f"detail-{self.PANE}").write_text(f"{self.now - 20}|StopFailure|rate_limit: 429\n")
        (self.state / f"state-{self.PANE}").write_text(
            f"error|claude|5|{self.now - 20}|{self.now - 2}|main:1.1|@1|/src/app|123\n")
        r = self.explain()
        self.assertIn("rate_limit: 429", r.stdout)
        self.assertIn("state=error", r.stdout)
        self.assertIn("cpu ticks", r.stdout)

    def test_explain_does_not_touch_state(self):
        self.report("ready")
        before = sorted(p.name for p in self.state.iterdir())
        self.explain()
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), before)

    def test_unknown_pane(self):
        r = self.explain("%99")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not found", r.stderr)

    def test_remote_id_goes_over_ssh(self):
        (self.state / "remotes").write_text("me@devbox.example:22|devbox|\n")
        (self.fake / "answer-devbox.example").write_text("state      working\n")
        self.cli_env = {"TMUX_AGENT_REMOTES": "devbox"}
        r = self.cli("explain", "--pane", "me@devbox.example:22/%3", **self.cli_env)
        calls = self.ssh_log.read_text() if self.ssh_log.exists() else ""
        self.assertIn("explain --pane", calls, r.stderr)
        self.assertIn("%3", calls)


if __name__ == "__main__":
    unittest.main()

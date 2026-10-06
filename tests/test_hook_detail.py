"""Hook detail files, the error state, scan shortcuts, send --expect-state
and the detail and diff subcommands of bin/tmux-agent."""

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from test_tmux_agent import CLI, fake_tmux

HAVE_JQ = shutil.which("jq") is not None


class Sandbox(unittest.TestCase):
    """A state dir plus a fake bin dir (tmux, ps) in front of PATH."""

    def setUp(self):
        # Hooks leave a background rescan that may still write into the
        # state dir while it is removed.
        self._tmp = tempfile.TemporaryDirectory(prefix="ta-detail-", ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.state = self.dir / "state"
        self.state.mkdir()
        self.fake = self.dir / "bin"
        self.fake.mkdir()
        # The hook's background rescan sweeps files of panes tmux does not
        # list: keep %999 alive so it leaves its detail alone.
        fake_tmux(self.fake, {
            "list-panes -a -F #{pane_id}|#{pane_pid}|#{window_id}|"
            "#{session_name}:#{window_index}.#{pane_index}|#{pane_current_path}":
            "%999|4199999|@9|main:1.1|/project\n"})
        self.env = {**os.environ, "TMUX_AGENT_QUIET": "1",
                    "TMUX_AGENT_STATE_DIR": str(self.state),
                    "PATH": f"{self.fake}:{os.environ['PATH']}"}

    def cli(self, *args, input=None, **env):
        return subprocess.run([str(CLI), *args], capture_output=True, text=True,
                              input=input, env={**self.env, **env}, timeout=30)

    def state_file(self, pane, state, agent="claude", path="/project", tail_hash="1"):
        now = int(time.time())
        (self.state / f"state-{pane}").write_text(
            f"{state}|{agent}|0|{now}|{now}|main:1.1|@1|{path}|{tail_hash}\n")
        (self.state / "stamp").write_text(f"{now}\n")

    def report(self, pane, state, age=0):
        (self.state / f"report-{pane}").write_text(f"{state}|{int(time.time()) - age}\n")

    def detail(self, pane="%999"):
        f = self.state / f"detail-{pane}"
        return f.read_text() if f.exists() else None

    def text_of(self, pane="%999"):
        d = self.detail(pane)
        return d.rstrip("\n").split("|", 2)[2] if d else None


class HookDetailTests(Sandbox):
    def hook(self, event, payload, pane="%999", **kw):
        if not isinstance(payload, str):
            payload = json.dumps(payload)
        return self.cli("hook", "claude", event, input=payload, TMUX_PANE=pane, **kw)

    def setUp(self):
        super().setUp()
        if not HAVE_JQ:
            self.skipTest("jq not installed")

    def test_events_write_their_detail(self):
        cases = (
            ("PermissionRequest", {"tool_name": "Bash", "tool_input": {"command": "rm -rf x", "description": "d"}},
             "Bash: rm -rf x"),
            ("PermissionRequest", {"tool_name": "Edit", "tool_input": {"file_path": "/a/b.py"}},
             "Edit: /a/b.py"),
            ("PermissionRequest", {"tool_name": "WebFetch", "tool_input": {"url": "http://x"}},
             "WebFetch: http://x"),
            ("PermissionRequest", {"tool_name": "Grep", "tool_input": {"pattern": "foo"}},
             "Grep: foo"),
            ("PermissionRequest", {"tool_name": "Task", "tool_input": {"description": "dig"}},
             "Task: dig"),
            ("PermissionRequest", {"tool_name": "Mystery", "tool_input": {"x": 1}}, "Mystery"),
            ("PermissionRequest", {"tool_name": "Weird", "tool_input": {"command": 42}}, "Weird: 42"),
            ("Notification", {"message": "Claude needs your permission"},
             "Claude needs your permission"),
            ("Stop", {"last_assistant_message": "\n\n  First line\nsecond"}, "First line"),
            ("StopFailure", {"error_type": "rate_limit", "error_message": "slow down"},
             "rate_limit: slow down"),
            ("UserPromptSubmit", {"prompt": "\nfix the bug\nplease"}, "fix the bug"),
            ("UserPromptSubmit", {"user_prompt": "old key"}, "old key"),
        )
        for event, payload, want in cases:
            with self.subTest(event=event, want=want):
                (self.state / "detail-%999").unlink(missing_ok=True)
                r = self.hook(event, payload)
                self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
                d = self.detail()
                self.assertEqual(d.count("\n"), 1)
                epoch, ev, text = d.rstrip("\n").split("|", 2)
                self.assertEqual((ev, text), (event, want))
                self.assertLessEqual(abs(int(epoch) - time.time()), 5)

    def test_text_is_sanitized(self):
        r = self.hook("Notification", {"message": "a\x1b[31mred\x07 b|c\nd\te"})
        self.assertEqual(r.returncode, 0, r.stderr)
        text = self.text_of()
        self.assertEqual(self.detail().count("\n"), 1)
        for bad in ("\x1b", "\x07", "\t"):
            self.assertNotIn(bad, text)
        self.assertEqual(len(self.detail().rstrip("\n").split("|")), 3)
        self.assertIn("red", text)

    def test_empty_extraction_keeps_the_old_detail(self):
        self.hook("Notification", {"message": "first"})
        before = self.detail()
        self.hook("Notification", {"message": ""})
        self.hook("Stop", {"nothing": "here"})
        self.assertEqual(self.detail(), before)

    def test_bad_json_is_harmless(self):
        r = self.hook("Notification", "{not json")
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        self.assertIsNone(self.detail())
        self.assertTrue((self.state / "report-%999").exists())

    def test_tool_events_are_not_parsed(self):
        r = self.hook("PreToolUse", {"message": "x", "tool_name": "Bash"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(self.detail())

    def test_big_payload_is_capped_and_drained(self):
        big = json.dumps({"message": "hi", "pad": "x" * (2 * 1024 * 1024)})
        start = time.monotonic()
        r = self.hook("Stop", big)
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        self.assertTrue((self.state / "report-%999").exists())

    def test_notification_does_not_overwrite_a_fresh_permission_request(self):
        self.hook("PermissionRequest", {"tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.hook("Notification", {"message": "Claude needs your permission"})
        self.assertEqual(self.text_of(), "Bash: ls")
        # Past 30 s the Notification is the newer news.
        _, ev, text = self.detail().rstrip("\n").split("|", 2)
        (self.state / "detail-%999").write_text(f"{int(time.time()) - 60}|{ev}|{text}\n")
        self.hook("Notification", {"message": "Claude needs your permission"})
        self.assertEqual(self.text_of(), "Claude needs your permission")

    def test_session_end_removes_detail(self):
        self.hook("Notification", {"message": "x"})
        self.assertIsNotNone(self.detail())
        r = self.hook("SessionEnd", {})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(self.detail())

    def test_stop_failure_reports_error(self):
        r = self.hook("StopFailure", {"error_type": "overloaded", "error_message": "busy"})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        self.assertEqual((self.state / "report-%999").read_text().split("|")[0], "error")

    def test_detail_is_written_even_when_the_state_is_unchanged(self):
        self.hook("Notification", {"message": "one"})
        (self.state / "detail-%999").unlink()
        self.hook("Notification", {"message": "two"})
        self.assertEqual(self.text_of(), "two")

    def test_outside_tmux_still_drains_and_writes_nothing(self):
        r = self.hook("Notification", {"message": "x"}, pane="")
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_without_jq_nothing_is_written(self):
        nojq = self.dir / "nojq"
        nojq.mkdir()
        for tool in ("bash", "env", "cat", "head", "date", "mkdir", "id", "rm", "mv",
                     "tr", "dirname", "readlink", "sh", "ps", "grep"):
            path = shutil.which(tool)
            if path:
                (nojq / tool).symlink_to(path)
        (nojq / "tmux").write_text("#!/bin/sh\nexit 0\n")
        (nojq / "tmux").chmod(0o755)
        r = subprocess.run(
            [str(CLI), "hook", "claude", "Notification"], input='{"message": "x"}',
            capture_output=True, text=True,
            env={"PATH": str(nojq), "TMUX_AGENT_QUIET": "1",
                 "TMUX_AGENT_STATE_DIR": str(self.state), "TMUX_PANE": "%999"})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        self.assertIsNone(self.detail())
        self.assertTrue((self.state / "report-%999").exists())


class ScanTests(Sandbox):
    PANES = (("%1", 4100001), ("%2", 4100002), ("%3", 4100003))

    def setUp(self):
        super().setUp()
        panes = "".join(f"{p}|{pid}|@{p[1:]}|main:1.{p[1:]}|/project\n" for p, pid in self.PANES)
        self.captures = self.dir / "captures"
        self.captures.mkdir()
        self.ps_log = self.dir / "ps.log"
        self.log = self.dir / "tmux.log"
        (self.fake / "tmux").write_text(f"""#!/bin/sh
printf '%s\\n' "$*" >>{shlex.quote(str(self.log))}
case "$1" in
list-panes) printf '%s' {shlex.quote(panes)} ;;
capture-pane)
    for a; do p=$a; done
    [ -f "{self.captures}/$p" ] && cat "{self.captures}/$p"
    ;;
esac
exit 0
""")
        # Panes' own pids are the shells.
        table = "".join(f"{pid} 1 bash bash\n{pid + 1000} {pid} claude claude\n"
                        for _, pid in self.PANES)
        (self.fake / "ps").write_text(f"""#!/bin/sh
printf '%s\\n' "$*" >>{shlex.quote(str(self.ps_log))}
case "$1" in -eo) printf '%s' {shlex.quote(table)} ;; *) exit 1 ;; esac
""")
        for tool in ("tmux", "ps"):
            (self.fake / tool).chmod(0o755)

    def states(self):
        out = {}
        for f in self.state.glob("state-*"):
            out[f.name[len("state-"):]] = f.read_text().split("|")[0]
        return out

    def test_ps_runs_once_per_forced_refresh(self):
        r = self.cli("refresh", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(set(self.states()), {"%1", "%2", "%3"})
        calls = [c for c in self.ps_log.read_text().splitlines() if c.startswith("-eo")]
        self.assertEqual(len(calls), 1, calls)

    def test_interrupted_tail_drops_a_working_report(self):
        self.state_file("%1", "working", tail_hash="1")
        self.report("%1", "working")
        (self.captures / "%1").write_text("some output\n  ⎿  Interrupted by user\n")
        r = self.cli("refresh", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.states()["%1"], "ready")
        self.assertFalse((self.state / "report-%1").exists())

    def test_working_report_holds_without_interrupt(self):
        self.state_file("%1", "idle", tail_hash="1")
        self.report("%1", "working")
        (self.captures / "%1").write_text("compiling\n")
        self.cli("refresh", "1")
        self.assertEqual(self.states()["%1"], "working")
        self.assertTrue((self.state / "report-%1").exists())

    def test_settled_report_skips_the_capture_and_keeps_the_hash(self):
        for pane, state in (("%1", "blocked"), ("%2", "ready"), ("%3", "error")):
            self.state_file(pane, "idle", tail_hash="777")
            self.report(pane, state)
        self.report("%2", "ready")
        r = self.cli("refresh", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.states(), {"%1": "blocked", "%2": "ready", "%3": "error"})
        self.assertNotIn("capture-pane", self.log.read_text())
        for pane in ("%1", "%2", "%3"):
            self.assertTrue((self.state / f"state-{pane}").read_text().rstrip().endswith("|777"))

    def test_working_report_still_captures(self):
        self.state_file("%1", "working", tail_hash="777")
        self.report("%1", "working")
        self.cli("refresh", "1")
        self.assertIn("capture-pane", self.log.read_text())

    def test_seen_error_becomes_idle(self):
        self.state_file("%1", "error")
        self.report("%1", "error", age=10)
        (self.state / "seen-%1").write_text(f"{int(time.time())}\n")
        self.cli("refresh", "1")
        self.assertEqual(self.states()["%1"], "idle")

    def test_stale_detail_files_are_swept(self):
        (self.state / "detail-%77").write_text("1|Stop|x\n")
        (self.state / "detail-%1").write_text("1|Stop|x\n")
        self.cli("refresh", "1")
        self.assertFalse((self.state / "detail-%77").exists())
        self.assertTrue((self.state / "detail-%1").exists())


class ErrorStateTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.state_file("%1", "idle", agent="codex")
        self.state_file("%2", "error", agent="claude")
        self.state_file("%3", "blocked", agent="opencode")
        (self.state / "stamp").write_text(f"{int(time.time())}\n")
        fake_tmux(self.fake)

    def test_strip_names_the_error_agent_in_rank_order(self):
        r = self.cli("strip")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout
        self.assertIn("#[fg=magenta]✖ #[fg=white]claude#[default]", out)
        self.assertLess(out.index("opencode"), out.index("claude"))
        self.assertLess(out.index("claude"), out.index("codex"))

    def test_strip_counts_the_error_state(self):
        r = self.cli("strip", "--max", "1")
        self.assertIn("#[fg=magenta]✖ #[fg=white]1#[default]", r.stdout)

    def test_status_lists_the_error_agent(self):
        r = self.cli("status", "--json")
        rows = [json.loads(line) for line in r.stdout.splitlines()]
        self.assertEqual([x["state"] for x in rows], ["blocked", "error", "idle"])
        r = self.cli("status")
        self.assertIn("✖", r.stdout)

    def test_porcelain_lists_the_error_agent(self):
        r = self.cli("status", "--porcelain")
        self.assertEqual([l.split("|")[0] for l in r.stdout.splitlines()],
                         ["blocked", "error", "idle"])

    def test_attach_next_takes_blocked_first(self):
        r = self.cli("attach", "--next")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("switch-client -t %3", (self.fake / "tmux.log").read_text())


class StatusDetailTests(Sandbox):
    @unittest.skipUnless(HAVE_JQ, "jq not installed")
    def test_status_json_has_detail(self):
        fake_tmux(self.fake)
        self.state_file("%1", "blocked")
        self.state_file("%2", "idle")
        (self.state / "detail-%1").write_text("5|PermissionRequest|Bash: ls\n")
        rows = {x["pane"]: x for x in
                map(json.loads, self.cli("status", "--json").stdout.splitlines())}
        self.assertEqual(rows["%1"]["detail"], "Bash: ls")
        self.assertEqual(rows["%2"]["detail"], "")


class SendExpectStateTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.log = fake_tmux(self.fake)
        self.state_file("%1", "ready")

    def sent(self):
        return [c for c in self.log.read_text().splitlines() if c.startswith("send-keys")] \
            if self.log.exists() else []

    def test_refuses_when_the_state_differs(self):
        for args in (("--expect-state", "blocked", "--pane", "%1", "--key", "y"),
                     ("--pane", "%1", "--key", "y", "--expect-state", "blocked")):
            with self.subTest(args=args):
                r = self.cli("send", *args)
                self.assertEqual(r.returncode, 3, r.stderr)
                self.assertIn("send: pane changed: now ready", r.stderr)
        self.assertEqual(self.sent(), [])

    def test_sends_when_the_state_matches(self):
        r = self.cli("send", "--pane", "%1", "--expect-state", "ready", "--key", "y")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.sent(), ["send-keys -t %1 y"])


class DetailDiffTests(Sandbox):
    def test_detail_prints_the_text(self):
        (self.state / "detail-%1").write_text("5|Stop|all done\n")
        r = self.cli("detail", "--pane", "%1")
        self.assertEqual((r.returncode, r.stdout), (0, "all done\n"))
        r = self.cli("detail", "--pane", "%2")
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_detail_for_remote_ids_is_empty(self):
        r = self.cli("detail", "--pane", "u@h:22/%1")
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def git(self, repo, *args):
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                        *args], check=True, capture_output=True)

    def test_diff_shows_changes_and_untracked_files(self):
        repo = self.dir / "repo"
        repo.mkdir()
        self.git(repo, "init", "-q")
        (repo / "a.txt").write_text("one\n")
        self.git(repo, "add", "a.txt")
        self.git(repo, "commit", "-qm", "init")
        (repo / "a.txt").write_text("one\ntwo\n")
        (repo / "new.txt").write_text("x\n")
        fake_tmux(self.fake)
        self.state_file("%1", "idle", path=str(repo))
        r = self.cli("diff", "--pane", "%1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("a.txt | 1 +", r.stdout)
        self.assertIn("\x1b[", r.stdout)
        self.assertIn("+two", re.sub(r"\x1b\[[0-9;]*m", "", r.stdout))
        self.assertIn("?? new.txt", r.stdout)

    def test_diff_does_not_run_commands_from_the_repo_config(self):
        repo = self.dir / "evil"
        repo.mkdir()
        self.git(repo, "init", "-q")
        marker = self.dir / "marker"
        hook = self.dir / "hook.sh"
        hook.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n")
        hook.chmod(0o755)
        (repo / "a.txt").write_text("one\n")
        (repo / ".gitattributes").write_text("*.txt diff=evil\n")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-qm", "init")
        (repo / "a.txt").write_text("one\ntwo\n")
        for key in ("core.fsmonitor", "diff.external", "diff.evil.textconv"):
            self.git(repo, "config", key, str(hook))
        fake_tmux(self.fake)
        self.state_file("%1", "idle", path=str(repo))
        r = self.cli("diff", "--pane", "%1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("a.txt", r.stdout)
        self.assertFalse(marker.exists())

    def test_diff_outside_a_repo(self):
        plain = self.dir / "plain"
        plain.mkdir()
        self.state_file("%1", "idle", path=str(plain))
        r = self.cli("diff", "--pane", "%1")
        self.assertEqual((r.returncode, r.stdout), (0, f"not a git repository: {plain}\n"))


class UsageTests(Sandbox):
    def test_usage_lists_the_new_commands(self):
        r = self.cli("bogus")
        for word in ("detail --pane", "diff --pane", "--expect-state"):
            self.assertIn(word, r.stdout)


if __name__ == "__main__":
    unittest.main()

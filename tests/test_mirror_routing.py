"""Routing regressions for remote tmux pane mirrors."""

from pathlib import Path
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest

from test_tmux_agent import CLI, ROOT, RemoteTestSupport, fake_tmux, wait_for


class MirrorRoutingTests(RemoteTestSupport, unittest.TestCase):
    """Exercise mirror routing through the CLI and a fake tmux/ssh pair."""

    SESSION_FMT = "#{session_id}|#{session_name}|#{@tmux-agent-mirror-session}"
    PANE_FMT = "#{window_id}|#{@tmux-agent-remote}|#{@tmux-agent-remote-pane}|#{pane_id}|#{pane_dead}"

    def setUp(self):
        super().setUp()
        self.env["TMUX_AGENT_REMOTE_VIEW"] = "mirror"

    def cache(self, key, host, rows):
        """Install/update one host's cache without discarding other hosts."""
        remotes = self.state / "remotes"
        lines = remotes.read_text().splitlines() if remotes.exists() else []
        replacement = f"{key}|{host}||{host}|"
        for index, line in enumerate(lines):
            if line.split("|", 1)[0] == key:
                lines[index] = replacement
                break
        else:
            lines.append(replacement)
        remotes.write_text("\n".join(lines) + "\n")
        (self.state / f"remote-{key}").write_text(rows)
        (self.state / f"remote-{key}.meta").write_text(f"ok|{int(time.time())}\n")

    def stateful_tmux(self, key):
        """Fake the owned session changing after the first process creates it."""
        log = self.fake / "tmux.log"
        marker = self.dir / "mirror-session-created"
        query_sessions = f"list-sessions -F {self.SESSION_FMT}"
        query_panes = f"list-panes -s -t $9 -F {self.PANE_FMT}"
        script = self.fake / "tmux"
        script.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >>{shlex.quote(str(log))}\n"
            "case \"$*\" in\n"
            f"{shlex.quote(query_sessions)}) "
            f"[ ! -f {shlex.quote(str(marker))} ] || printf '%s\\n' '$9|remote-agents|on' ;;\n"
            f"{shlex.quote(query_panes)}) "
            f"[ ! -f {shlex.quote(str(marker))} ] || printf '%s\\n' '@10|{key}|%3|%20|0' ;;\n"
            "new-session*) sleep 0.2; "
            f"touch {shlex.quote(str(marker))}; printf '%s\\n' '$9|@10|%20' ;;\n"
            "esac\n"
        )
        script.chmod(0o755)
        return log

    def test_default_remote_view_creates_owned_mirror_session_and_routes_identity(self):
        self.cache(self.KEY, "devbox", "blocked|claude|%3|main:1.2|@2|0|/src\n")
        self.tmux_replies = {
            f"list-sessions -F {self.SESSION_FMT}": "",
            "new-session -d -P -F #{session_id}|#{window_id}|#{pane_id} -s remote-agents -n claude@devbox-%3 "
            f"{CLI} mirror --pane {self.KEY}/%3": "$9|@10|%20\n",
        }
        # This setting is the shipped default; remove the explicit fixture override.
        self.env.pop("TMUX_AGENT_REMOTE_VIEW")
        r = self.cli("attach", "--next")
        self.assertEqual(r.returncode, 0, f"{r.stderr}\n{self.tmux_calls()}\n{self.ssh_calls()}")
        calls = self.tmux_calls()
        self.assertIn("set-option -t $9 @tmux-agent-mirror-session on", calls)
        self.assertIn(f"set-option -w -t @10 @tmux-agent-remote {self.KEY}", calls)
        self.assertIn("set-option -w -t @10 @tmux-agent-remote-pane %3", calls)
        self.assertIn(f"set-option -p -t %20 @tmux-agent-remote {self.KEY}", calls)
        self.assertIn("set-option -p -t %20 @tmux-agent-remote-pane %3", calls)
        self.assertIn("set-option -p -t %20 @tmux-agent-mirror on", calls)
        self.assertIn("switch-client -t @10", calls)
        self.assertFalse(any("tmux attach-session" in c for c in calls))

    def test_same_remote_agent_reuses_its_window(self):
        self.cache(self.KEY, "devbox", "blocked|claude|%3|main:1.2|@2|0|/src\n")
        self.tmux_replies = {
            f"list-sessions -F {self.SESSION_FMT}": "$9|remote-agents|on\n",
            f"list-panes -s -t $9 -F {self.PANE_FMT}": f"@10|{self.KEY}|%3|%20|0\n",
        }
        r = self.cli("attach", "--next")
        self.assertEqual(r.returncode, 0, f"{r.stderr}\n{self.tmux_calls()}\n{self.ssh_calls()}")
        calls = self.tmux_calls()
        self.assertFalse(any(c.startswith(("new-session", "new-window")) for c in calls))
        self.assertIn("switch-client -t @10", calls)

    def test_cross_host_pane_id_collision_gets_distinct_mirrors(self):
        other = "me@build.example:22"
        self.env["TMUX_AGENT_REMOTES"] = "devbox build"
        self.cache(self.KEY, "devbox", "blocked|claude|%3|main:1.2|@2|0|/src\n")
        self.cache(other, "build", "blocked|codex|%3|main:1.2|@2|0|/src\n")
        self.assertEqual(len((self.state / "remotes").read_text().splitlines()), 2)
        # The existing first mirror and the next host share a remote pane ID.
        self.tmux_replies = {
            f"list-sessions -F {self.SESSION_FMT}": "$9|remote-agents|on\n",
            f"list-panes -s -t $9 -F {self.PANE_FMT}": f"@10|{self.KEY}|%3|%20|0\n",
            "new-window -d -P -F #{window_id}|#{pane_id} -t $9: -n codex@build-%3 "
            f"{CLI} mirror --pane {other}/%3": "@11|%21\n",
        }
        r = self.cli("attach", "--urgent", "--from", f"{self.KEY}/%3")
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.tmux_calls()
        self.assertTrue(any(c.startswith("new-window") and "codex@build-%3" in c for c in calls), calls)
        self.assertNotIn(f"set-option -w -t @11 @tmux-agent-remote {self.KEY}", calls)
        self.assertIn(f"set-option -w -t @11 @tmux-agent-remote {other}", calls)
        self.assertIn("set-option -w -t @11 @tmux-agent-remote-pane %3", calls)

    def test_two_agents_on_one_host_get_distinct_mirror_windows(self):
        self.cache(self.KEY, "devbox",
                   "blocked|claude|%3|main:1.2|@2|0|/src\nblocked|codex|%4|main:1.3|@2|0|/src\n")
        self.tmux_replies = {
            f"list-sessions -F {self.SESSION_FMT}": "$9|remote-agents|on\n",
            f"list-panes -s -t $9 -F {self.PANE_FMT}": f"@10|{self.KEY}|%3|%20|0\n",
            "new-window -d -P -F #{window_id}|#{pane_id} -t $9: -n codex@devbox-%4 "
            f"{CLI} mirror --pane {self.KEY}/%4": "@11|%21\n",
        }
        r = self.cli("attach", "--urgent", "--from", f"{self.KEY}/%3")
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.tmux_calls()
        self.assertIn(f"set-option -w -t @11 @tmux-agent-remote {self.KEY}", calls)
        self.assertIn("set-option -w -t @11 @tmux-agent-remote-pane %4", calls)
        self.assertIn("set-option -p -t %21 @tmux-agent-remote-pane %4", calls)

    def test_dead_mirror_pane_is_respawned_for_the_same_remote_identity(self):
        self.cache(self.KEY, "devbox", "blocked|claude|%3|main:1.2|@2|0|/src\n")
        self.tmux_replies = {
            f"list-sessions -F {self.SESSION_FMT}": "$9|remote-agents|on\n",
            f"list-panes -s -t $9 -F {self.PANE_FMT}": f"@10|{self.KEY}|%3|%20|1\n",
        }
        r = self.cli("attach", "--next")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"respawn-pane -t %20 {CLI} mirror --pane {self.KEY}/%3",
                      self.tmux_calls())

    def test_concurrent_open_creates_only_one_mirror_window(self):
        self.cache(self.KEY, "devbox", "blocked|claude|%3|main:1.2|@2|0|/src\n")
        log = self.stateful_tmux(self.KEY)
        procs = [subprocess.Popen([str(CLI), "attach", "--next"], cwd=ROOT,
                                  env=self.env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL)
                 for _ in range(2)]
        results = [proc.communicate(timeout=15) + (proc.returncode,) for proc in procs]
        self.assertEqual([rc for _, _, rc in results], [0, 0], results)
        calls = log.read_text().splitlines()
        self.assertEqual(sum(c.startswith("new-session") for c in calls), 1, calls)
        self.assertFalse(any(c.startswith("new-window") for c in calls), calls)

    def test_unowned_remote_agents_session_is_never_reused(self):
        self.cache(self.KEY, "devbox", "blocked|claude|%3|main:1.2|@2|0|/src\n")
        self.tmux_replies = {
            f"list-sessions -F {self.SESSION_FMT}": "$9|remote-agents|\n",
        }
        r = self.cli("attach", "--next")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not owned by tmux-agent", r.stderr)
        self.assertFalse(any(c.startswith(("new-session", "new-window")) for c in self.tmux_calls()))

    def test_mirror_pane_is_skipped_by_local_detection(self):
        (self.fake / "ps").write_text(
            "#!/bin/sh\nprintf '123 1 bash bash\\n456 123 claude claude\\n'\n"
        )
        (self.fake / "ps").chmod(0o755)
        self.tmux_replies = {
            "list-panes -a -F #{pane_id}|#{@tmux-agent-mirror}": "%20|on\n",
            "list-panes -a -F #{pane_id}|#{pane_pid}|#{window_id}|#{session_name}:#{window_index}.#{pane_index}|#{pane_current_path}":
                "%20|123|@10|remote-agents:1.1|/src\n",
        }
        (self.state / "state-%20").write_text("blocked|claude|0|1|1|remote-agents:1.1|@10|/src|\n")
        (self.state / "seen-%20").write_text("1\n")
        (self.state / "report-%20").write_text("blocked|1\n")
        r = self.cli("refresh", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        for stale in ("state-%20", "seen-%20", "report-%20"):
            self.assertFalse((self.state / stale).exists(), stale)

    def test_seen_on_local_mirror_resolves_to_remote_composite_id(self):
        self.cache(self.KEY, "devbox", "blocked|claude|%3|main:1.2|@2|0|/src\n")
        self.tmux_replies = {
            "display-message -p -t %20 #{@tmux-agent-remote}|#{@tmux-agent-remote-pane}|#{@tmux-agent-mirror}":
                f"{self.KEY}|%3|on",
        }
        r = self.cli("seen", "%20")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.state / "seen-%20").exists())
        self.assertTrue(any(c.endswith("tmux-agent seen %3") for c in self.ssh_calls()), self.ssh_calls())

    def test_urgent_cycle_from_mirror_pane_uses_remote_identity(self):
        self.cache(self.KEY, "devbox", "blocked|claude|%3|main:1.2|@2|0|/src\nready|codex|%4|main:1.3|@2|0|/src\n")
        self.tmux_replies = {
            "display-message -p -t %20 #{@tmux-agent-remote}|#{@tmux-agent-remote-pane}|#{@tmux-agent-mirror}":
                f"{self.KEY}|%3|on",
            f"list-sessions -F {self.SESSION_FMT}": "",
            f"new-session -d -P -F #{{session_id}}|#{{window_id}}|#{{pane_id}} -s remote-agents -n codex@devbox-%4 {CLI} mirror --pane {self.KEY}/%4":
                "$9|@10|%20\n",
        }
        r = self.cli("attach", "--urgent", "--from", "%20")
        self.assertEqual(r.returncode, 0, f"{r.stderr}\n{self.tmux_calls()}")
        self.assertIn("mirror --pane %s/%%4" % self.KEY, "\n".join(self.tmux_calls()))

    def notification_body_for_active_marker(self, marker):
        self.cache(self.KEY, "devbox", "working|claude|%3|main:1.2|@2|0|/src\nworking|codex|%4|main:1.3|@2|0|/src\n")
        self.answer("devbox", "blocked|claude|%3|main:1.2|@2|0|/src\nblocked|codex|%4|main:1.3|@2|0|/src\n")
        notifications = self.dir / "notifications"
        (self.fake / "notify-send").write_text(
            f'#!/bin/sh\nprintf "%s\\n" "$*" >>{shlex.quote(str(notifications))}\n'
            f'printf "__NOTIFY_CALL_END__\\n" >>{shlex.quote(str(notifications))}\n'
            'printf default\n'
        )
        (self.fake / "notify-send").chmod(0o755)
        self.tmux_replies = {
            "list-clients -F #{?#{m:*focused*,#{client_flags}},#{client_name},}": "client-1\n",
            "display-message -p -c client-1 #{@tmux-agent-remote}": self.KEY,
            "display-message -p -c client-1 #{@tmux-agent-remote-pane}": "%3",
            "display-message -p -c client-1 #{@tmux-agent-mirror}": marker,
        }
        r = self.cli("remote-refresh", TMUX_AGENT_QUIET="",
                     TMUX_AGENT_NOTIFY_METHOD="desktop")
        self.assertEqual(r.returncode, 0, r.stderr)
        expected_notifications = 1 if marker == "on" else 2
        self.assertTrue(
            wait_for(lambda: notifications.exists()
                     and notifications.read_text().count("__NOTIFY_CALL_END__")
                     >= expected_notifications),
            self.tmux_calls(),
        )
        return notifications.read_text()

    def test_remote_notification_suppresses_only_the_mirrored_pane(self):
        body = self.notification_body_for_active_marker("on")
        self.assertIn("codex@devbox", body)
        self.assertNotIn("claude@devbox", body)

    def test_unmarked_active_pane_does_not_suppress_remote_notification(self):
        # A shell-split pane in a mirror window has the window identity but
        # not the mirror pane marker, so the selected agent is still notified.
        body = self.notification_body_for_active_marker("")
        self.assertIn("claude@devbox", body)
        self.assertIn("codex@devbox", body)

    def test_non_mirror_active_pane_does_not_suppress_remote_notification(self):
        body = self.notification_body_for_active_marker("off")
        self.assertIn("claude@devbox", body)
        self.assertIn("codex@devbox", body)

    def test_bad_host_or_pane_id_and_missing_python_fail_clearly(self):
        r = self.cli("mirror", "--pane", "unknown/%3")
        self.assertEqual(r.returncode, 1)
        self.assertIn("unknown remote host", r.stderr)
        r = self.cli("mirror", "--pane", f"{self.KEY}/not-a-pane")
        self.assertEqual(r.returncode, 1)
        self.assertIn("invalid remote pane id", r.stderr)

        # Put an unavailable-version shim first on PATH to exercise the same
        # actionable error path as a machine without Python 3.9+.
        (self.fake / "python3").write_text("#!/bin/sh\nexit 1\n")
        (self.fake / "python3").chmod(0o755)
        r = self.cli("mirror", "--pane", f"{self.KEY}/%3")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Python 3.9+", r.stderr)


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class MirrorFitTests(unittest.TestCase):
    """mirror-fit against a real tmux on a private socket and a fake bridge."""

    BRIDGE = (
        "import signal, sys, time\n"
        "log = sys.argv[1]\n"
        "def note(name):\n"
        "    def handler(*_):\n"
        "        with open(log, 'a') as f:\n"
        "            f.write(name + '\\n')\n"
        "    return handler\n"
        "signal.signal(signal.SIGUSR1, note('USR1'))\n"
        "signal.signal(signal.SIGUSR2, note('USR2'))\n"
        "open(log + '.ready', 'w').close()\n"
        "while True:\n"
        "    time.sleep(0.05)\n"
    )

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ta-fit-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.state = self.dir / "state"
        self.state.mkdir()
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.signals = self.dir / "signals"
        sockdir = Path(tempfile.mkdtemp(prefix="ta-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, sockdir, ignore_errors=True)
        self.sock = sockdir / "sock"
        real = shutil.which("tmux")
        wrapper = self.bin / "tmux"
        wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(real)} -S {shlex.quote(str(self.sock))} "$@"\n')
        wrapper.chmod(0o755)
        self.env = {
            **{k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")},
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "TMUX_AGENT_STATE_DIR": str(self.state),
            "TMUX_AGENT_QUIET": "1",
        }
        self.addCleanup(self.tmux, "kill-server")
        (self.dir / "remote-pane.py").write_text(self.BRIDGE)

    def tmux(self, *args):
        return subprocess.run(["tmux", *args], capture_output=True, text=True, env=self.env, stdin=subprocess.DEVNULL)

    def new_pane(self, *command, mirror=True):
        r = self.tmux("new-session", "-d", "-P", "-F", "#{pane_id}", "-x", "80", "-y", "24",
                      *command)
        self.assertEqual(r.returncode, 0, r.stderr)
        pane = r.stdout.strip()
        if mirror:
            self.tmux("set-option", "-p", "-t", pane, "@tmux-agent-mirror", "on")
        return pane

    def bridge_pane(self, **kw):
        pane = self.new_pane("python3", str(self.dir / "remote-pane.py"), str(self.signals), **kw)
        wait_for(lambda: Path(f"{self.signals}.ready").exists())
        return pane

    def fit(self, *args):
        return subprocess.run([str(CLI), "mirror-fit", *args], capture_output=True, text=True,
                              env=self.env, timeout=20, stdin=subprocess.DEVNULL)

    def received(self, count):
        wait_for(lambda: self.signals.exists() and len(self.signals.read_text().split()) >= count)
        return self.signals.read_text().split()

    def assert_no_signal(self):
        time.sleep(0.3)
        self.assertFalse(self.signals.exists())

    def test_on_sends_usr1_and_off_sends_usr2(self):
        pane = self.bridge_pane()
        self.assertEqual(self.fit("on", "-t", pane).returncode, 0)
        self.assertEqual(self.received(1), ["USR1"])
        self.assertEqual(self.fit("off", "-t", pane).returncode, 0)
        self.assertEqual(self.received(2), ["USR1", "USR2"])

    def test_toggle_follows_the_status_file(self):
        pane = self.bridge_pane()
        status = self.state / f"mirror-{pane}.status"
        status.write_text('{"fit": false, "connected": true}\n')
        self.assertEqual(self.fit("toggle", "-t", pane).returncode, 0)
        self.assertEqual(self.received(1), ["USR1"])
        status.write_text('{"fit": true, "connected": true}\n')
        self.assertEqual(self.fit("-t", pane).returncode, 0)
        self.assertEqual(self.received(2), ["USR1", "USR2"])

    def test_toggle_without_a_status_file_turns_fit_on(self):
        pane = self.bridge_pane()
        self.assertEqual(self.fit("toggle", "-t", pane).returncode, 0)
        self.assertEqual(self.received(1), ["USR1"])

    def test_default_pane_is_tmux_pane(self):
        pane = self.bridge_pane()
        r = subprocess.run([str(CLI), "mirror-fit", "on"], capture_output=True, text=True,
                           env={**self.env, "TMUX_PANE": pane}, timeout=20, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.received(1), ["USR1"])

    def test_unmarked_pane_is_refused(self):
        pane = self.bridge_pane(mirror=False)
        r = self.fit("on", "-t", pane)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a remote pane mirror", r.stderr)
        self.assert_no_signal()

    def test_marked_pane_running_something_else_is_refused(self):
        pane = self.new_pane("sleep", "300")
        r = self.fit("on", "-t", pane)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a remote pane mirror", r.stderr)

    def test_bad_action_or_pane_id_is_a_usage_error(self):
        pane = self.bridge_pane()
        self.assertEqual(self.fit("sideways", "-t", pane).returncode, 2)
        self.assertEqual(self.fit("on", "-t", "main").returncode, 2)
        self.assert_no_signal()


class MirrorStatusSweepTests(unittest.TestCase):
    """A scan deletes mirror status files of panes that no longer exist."""

    def test_scan_removes_only_stale_mirror_status_files(self):
        with tempfile.TemporaryDirectory(prefix="ta-sweep-") as directory:
            state = Path(directory)
            fake_tmux(directory, {
                "list-panes -a -F #{pane_id}|#{@tmux-agent-mirror}": "%12|on\n%1|\n",
                "list-panes -a -F #{pane_id}|#{pane_pid}|#{window_id}|"
                "#{session_name}:#{window_index}.#{pane_index}|#{pane_current_path}":
                    "%12|1|@1|remote-agents:1.1|/x\n%1|1|@2|work:1.1|/x\n",
            })
            for name in ("mirror-%99.status", "mirror-%12.status",
                         "mirror-unknown.status", "mirror.lock"):
                (state / name).write_text("x\n")
            env = {k: v for k, v in os.environ.items() if k != "TMUX_AGENT_QUIET"}
            env.update({"TMUX_AGENT_STATE_DIR": directory,
                        "PATH": f"{directory}:{os.environ['PATH']}"})
            r = subprocess.run([str(CLI), "refresh", "1"], capture_output=True, text=True,
                               env=env, stdin=subprocess.DEVNULL, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse((state / "mirror-%99.status").exists())
            for kept in ("mirror-%12.status", "mirror-unknown.status", "mirror.lock"):
                self.assertTrue((state / kept).exists(), kept)

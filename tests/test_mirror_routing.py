"""Routing regressions for remote tmux pane mirrors."""

from pathlib import Path
import shlex
import subprocess
import time
import unittest

from test_tmux_agent import CLI, ROOT, RemoteTestSupport, wait_for


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
                                  stderr=subprocess.PIPE, text=True)
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

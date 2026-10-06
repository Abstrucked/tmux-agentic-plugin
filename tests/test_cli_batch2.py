"""Batch 2 CLI behaviour: private per-server state dirs, the guarded send,
StopFailure details, attach --urgent, remote seen/detail/wait and mobile's
keyed listen port (bin/tmux-agent)."""

import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tmux_agent import CLI, HookTests, fake_ssh, fake_tmux  # noqa: E402

PANES = "list-panes -a -F #{pane_id}|#{pane_pid}|#{window_id}|#{session_name}:#{window_index}.#{pane_index}|#{pane_current_path}"


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ta-cli2-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.state = self.dir / "state"
        self.state.mkdir()
        self.fake = self.dir / "bin"
        self.fake.mkdir()
        self.now = int(time.time())
        self.env = {
            **os.environ,
            "TMUX_AGENT_QUIET": "1",
            "TMUX_AGENT_STATE_DIR": str(self.state),
            "PATH": f"{self.fake}:{os.environ['PATH']}",
        }

    def run_cli(self, *args, env=None, **kw):
        return subprocess.run([str(CLI), *args], capture_output=True, text=True,
                              env=env or self.env, timeout=30, **kw)

    def write_state(self, pane, state, agent="claude"):
        (self.state / f"state-{pane}").write_text(
            f"{state}|{agent}|0|{self.now}|{self.now}|main:1.1|@1|/project|1\n")


class StateDirTests(Base):
    def setUp(self):
        super().setUp()
        fake_tmux(self.fake)
        self.env.pop("TMUX_AGENT_STATE_DIR")
        self.env.pop("XDG_RUNTIME_DIR", None)
        self.tmp = self.dir / "tmp"
        self.tmp.mkdir()
        self.env["TMPDIR"] = str(self.tmp)

    def refresh(self, sock):
        r = self.run_cli("refresh", "1", env={**self.env, "TMUX": f"{sock},1,0"})
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def test_two_tmux_servers_get_two_state_dirs(self):
        self.refresh("/run/tmux/one")
        self.refresh("/run/tmux/two")
        parent = self.tmp / f"tmux-agent-{os.getuid()}"
        self.assertEqual(sorted(p.name for p in parent.iterdir()), ["one", "two"])
        self.assertTrue((parent / "one" / "stamp").exists())

    def test_xdg_runtime_dir_is_preferred_when_it_is_a_directory(self):
        xdg = self.dir / "xdg"
        xdg.mkdir()
        self.env["XDG_RUNTIME_DIR"] = str(xdg)
        self.refresh("/run/tmux/one")
        self.assertTrue((xdg / f"tmux-agent-{os.getuid()}" / "one" / "stamp").exists())
        self.assertEqual(list(self.tmp.iterdir()), [])
        # Not a directory: back to TMPDIR.
        self.env["XDG_RUNTIME_DIR"] = str(self.dir / "missing")
        self.refresh("/run/tmux/one")
        self.assertTrue((self.tmp / f"tmux-agent-{os.getuid()}" / "one").is_dir())

    def test_dirs_are_private_even_when_an_older_one_was_not(self):
        parent = self.tmp / f"tmux-agent-{os.getuid()}"
        parent.mkdir(mode=0o755)
        parent.chmod(0o755)
        old = parent / "stamp"
        old.write_text("1\n")
        self.refresh("/run/tmux/one")
        for d in (parent, parent / "one"):
            self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700, d)

    def test_a_symlinked_parent_is_refused_with_a_stable_cache_fallback(self):
        victim = self.dir / "victim"
        victim.mkdir()
        (self.tmp / f"tmux-agent-{os.getuid()}").symlink_to(victim)
        cache = self.dir / "cache"
        self.env["XDG_CACHE_HOME"] = str(cache)
        for _ in range(2):
            r = self.refresh("/run/tmux/one")
            self.assertEqual(r.stderr.count("tmux-agent:"), 1, r.stderr)
            self.assertIn("not a private directory", r.stderr)
        self.assertEqual(list(victim.iterdir()), [])
        state = cache / "tmux-agent" / "one"
        self.assertTrue((state / "stamp").exists())
        for d in (cache / "tmux-agent", state):
            self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700, d)
        self.assertEqual([p.name for p in self.tmp.iterdir()], [f"tmux-agent-{os.getuid()}"])

    def test_unusable_cache_dir_falls_back_to_a_temp_dir(self):
        victim = self.dir / "victim"
        victim.mkdir()
        (self.tmp / f"tmux-agent-{os.getuid()}").symlink_to(victim)
        cache = self.dir / "cache"
        cache.mkdir()
        (cache / "tmux-agent").symlink_to(victim)
        self.env["XDG_CACHE_HOME"] = str(cache)
        r = self.refresh("/run/tmux/one")
        self.assertEqual(list(victim.iterdir()), [])
        made = [p for p in self.tmp.iterdir() if not p.is_symlink()]
        self.assertEqual(len(made), 1, r.stderr)
        self.assertTrue((made[0] / "stamp").exists())

    def test_explicit_state_dir_still_wins(self):
        explicit = self.dir / "explicit"
        self.run_cli("refresh", "1", env={**self.env, "TMUX_AGENT_STATE_DIR": str(explicit)})
        self.assertTrue((explicit / "stamp").exists())
        self.assertEqual(list(self.tmp.iterdir()), [])


class GuardedSendTests(Base):
    def setUp(self):
        super().setUp()
        # One live pane whose process is no known agent: reports decide.
        self.tmux_log = fake_tmux(self.fake, {PANES: "%1|1|@1|main:1.1|/project\n"})
        (self.state / "stamp").write_text(f"{self.now}\n")

    def sent(self):
        if not self.tmux_log.exists():
            return []
        return [c for c in self.tmux_log.read_text().splitlines() if c.startswith("send-keys")]

    def send(self, *args):
        return self.run_cli("send", "--pane", "%1", *args, "--key", "y")

    def test_newer_ready_report_beats_the_cached_blocked_state(self):
        self.write_state("%1", "blocked")
        (self.state / "report-%1").write_text(f"ready|{self.now}\n")
        r = self.send("--expect-state", "blocked")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("pane changed: now ready", r.stderr)
        self.assertEqual(self.sent(), [])

    def test_blocked_report_lets_the_send_through(self):
        self.write_state("%1", "idle")
        (self.state / "report-%1").write_text(f"blocked|{self.now}\n")
        r = self.send("--expect-state", "blocked")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.sent(), ["send-keys -t %1 y"])

    def blocked_with_detail(self, epoch):
        self.write_state("%1", "blocked")
        (self.state / "report-%1").write_text(f"blocked|{self.now}\n")
        (self.state / "detail-%1").write_text(f"{epoch}|PermissionRequest|Bash: ls\n")

    def test_epoch_mismatch_means_a_new_request(self):
        self.blocked_with_detail(self.now)
        r = self.send("--expect-state", "blocked", "--expect-epoch", str(self.now - 5))
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("send: pane changed: new request", r.stderr)
        self.assertEqual(self.sent(), [])

    def test_missing_detail_does_not_match_an_epoch(self):
        self.write_state("%1", "blocked")
        (self.state / "report-%1").write_text(f"blocked|{self.now}\n")
        r = self.send("--expect-state", "blocked", "--expect-epoch", str(self.now))
        self.assertEqual(r.returncode, 3, r.stderr)

    def test_matching_epoch_sends(self):
        self.blocked_with_detail(self.now)
        r = self.send("--expect-state", "blocked", "--expect-epoch", str(self.now))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.sent(), ["send-keys -t %1 y"])

    def test_report_authoritative_rule(self):
        def rule(*a):
            script = (f"eval \"$(sed -n '/^report_authoritative() {{/,/^}}/p' '{CLI}')\"; "
                      f"TA_REPORT_TTL=300; report_authoritative {' '.join(a)}")
            return subprocess.run(["bash", "-c", script], capture_output=True).returncode
        n = self.now
        self.assertEqual(rule("ready", str(n), str(n), "0"), 0)
        self.assertEqual(rule("''", str(n), str(n), "1"), 1)
        self.assertEqual(rule("working", str(n - 999), str(n), "1"), 1)
        self.assertEqual(rule("ready", str(n - 999), str(n), "1"), 0)
        self.assertEqual(rule("ready", str(n - 999), str(n), "0"), 1)


class HookDetailTests(Base):
    def hook(self, event, payload):
        settle_dir = self.state
        self.addCleanup(HookTests.settle, str(settle_dir))
        return subprocess.run(
            [str(CLI), "hook", "claude", event], input=payload, capture_output=True,
            text=True, env={**self.env, "TMUX_PANE": "%999"})

    def detail(self):
        return self.run_cli("detail", "--pane", "%999").stdout.strip()

    def test_official_stop_failure_payload(self):
        fake_tmux(self.fake)
        r = self.hook("StopFailure",
                      '{"error":"rate_limit","error_details":"429 Too Many Requests",'
                      '"last_assistant_message":"API Error: Rate limit reached"}')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.detail(), "rate_limit: 429 Too Many Requests")

    def test_message_only_falls_back_to_the_last_assistant_message(self):
        fake_tmux(self.fake)
        self.hook("StopFailure", '{"last_assistant_message":"\\n\\n API Error: Rate limit reached\\nmore"}')
        self.assertEqual(self.detail(), "API Error: Rate limit reached")

    def test_older_aliases_still_work(self):
        fake_tmux(self.fake)
        self.hook("StopFailure", '{"error_type":"overloaded","error_message":"busy"}')
        self.assertEqual(self.detail(), "overloaded: busy")


class AttachUrgentTests(Base):
    def attach(self, states, *args):
        for p in list(self.state.glob("state-*")):
            p.unlink()
        (self.state / "stamp").write_text(f"{self.now}\n")
        for pane, st in states.items():
            self.write_state(pane, st)
        log = fake_tmux(self.fake)
        log.unlink(missing_ok=True)
        r = self.run_cli("attach", "--urgent", *args)
        calls = log.read_text().splitlines() if log.exists() else []
        return r, [c.split()[-1] for c in calls if c.startswith("switch-client")]

    def test_error_only_agent_is_picked(self):
        r, jumps = self.attach({"%1": "working", "%2": "error", "%3": "idle"})
        self.assertEqual((r.returncode, jumps), (0, ["%2"]))

    def test_order_is_blocked_then_error_then_ready(self):
        mix = {"%1": "ready", "%2": "error", "%3": "blocked", "%4": "working"}
        for current, expected in (("", "%3"), ("%3", "%2"), ("%2", "%1"), ("%1", "%3")):
            with self.subTest(current=current):
                args = ("--from", current) if current else ()
                self.assertEqual(self.attach(mix, *args)[1], [expected])


class RemoteParityTests(Base):
    KEY = "me@devbox.example:22"

    def setUp(self):
        super().setUp()
        self.sockets = Path(tempfile.mkdtemp(prefix="ta-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.sockets, ignore_errors=True)
        self.ssh_log = fake_ssh(self.fake)
        fake_tmux(self.fake)
        (self.state / "stamp").write_text(f"{self.now}\n")
        self.env.update({
            "TMUX_AGENT_REMOTES": "devbox",
            "TMUX_AGENT_REMOTE_DISCOVER": "off",
            "TMUX_AGENT_REMOTE_TAILSCALE": "off",
            "TMUX_AGENT_REMOTE_WATCH": "off",
            "TMUX_AGENT_SSH_SOCKETS": f"{self.sockets}/master-*",
        })
        self.assertEqual(self.run_cli("remote-refresh").returncode, 0)
        self.ssh_log.unlink(missing_ok=True)

    def calls(self):
        return self.ssh_log.read_text().splitlines() if self.ssh_log.exists() else []

    def test_seen_is_forwarded(self):
        r = self.run_cli("seen", f"{self.KEY}/%3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("tmux-agent seen %3", self.calls()[-1])
        self.assertEqual(list(self.state.glob("seen-*")), [])

    def test_detail_is_forwarded_and_printed(self):
        (self.fake / "answer-devbox").write_text("Bash: rm -rf build\n")
        r = self.run_cli("detail", "--pane", f"{self.KEY}/%3")
        self.assertEqual((r.returncode, r.stdout), (0, "Bash: rm -rf build\n"), r.stderr)
        self.assertIn("tmux-agent detail --pane %3", self.calls()[-1])

    def test_wait_is_streamed_with_state_and_timeout(self):
        r = self.run_cli("wait", "--pane", f"{self.KEY}/%3", "--state", "ready", "--timeout", "7")
        self.assertEqual(r.returncode, 0, r.stderr)
        call = self.calls()[-1]
        self.assertIn("tmux-agent wait --pane %3 --state ready --timeout 7", call)
        self.assertIn("ServerAliveInterval", call)

    def test_wait_keeps_the_remote_exit_status(self):
        (self.fake / "answer-devbox.rc").write_text("1")
        r = self.run_cli("wait", "--pane", f"{self.KEY}/%3", "--state", "ready", "--timeout", "1")
        self.assertEqual(r.returncode, 1)

    def test_send_forwards_the_guards(self):
        r = self.run_cli("send", "--pane", f"{self.KEY}/%3", "--expect-state", "blocked",
                         "--expect-epoch", "123", "--key", "y")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("send --pane %3 --expect-state blocked --expect-epoch 123 --key y",
                      self.calls()[-1])


class ServerTargetingTests(Base):
    @unittest.skipUnless(shutil.which("tmux"), "needs tmux")
    def test_a_private_server_gets_its_own_state_dir(self):
        name = f"ta-test-{os.getpid()}"
        sockdir = Path(tempfile.mkdtemp(prefix="ta-srv-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, sockdir, ignore_errors=True)
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        env.update(TMUX_TMPDIR=str(sockdir), HOME=str(self.dir))
        tmux = ["tmux", "-L", name]
        self.addCleanup(subprocess.run, [*tmux, "kill-server"], env=env, capture_output=True)
        subprocess.run([*tmux, "-f", "/dev/null", "new-session", "-d", "-s", "t"],
                       env=env, check=True)
        sock = subprocess.run([*tmux, "display-message", "-p", "#{socket_path}"], env=env,
                              capture_output=True, text=True, check=True).stdout.strip()
        (self.dir / "tmp").mkdir()
        job_env = {k: v for k, v in env.items() if k not in ("XDG_RUNTIME_DIR", "TMUX_AGENT_STATE_DIR")}
        job_env.update(TMUX_AGENT_QUIET="1", TMPDIR=str(self.dir / "tmp"), TMUX=f"{sock},1,0")
        r = subprocess.run([str(CLI), "refresh"], env=job_env, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([p.name for p in (self.dir / "tmp" / f"tmux-agent-{os.getuid()}").iterdir()],
                         [name])


class LockFallbackTests(Base):
    """Without flock, refresh takes a mkdir lock; it must never spin."""

    def setUp(self):
        super().setUp()
        self.bin = self.dir / "nolock"
        self.bin.mkdir()
        for tool in ("bash", "env", "cat", "head", "tail", "date", "rmdir", "id", "rm", "mv",
                     "tr", "dirname", "readlink", "sh", "ps", "grep", "sleep", "sort", "cut",
                     "sed", "awk", "cksum", "stat", "wc", "touch", "chmod"):
            found = shutil.which(tool)
            if found:
                (self.bin / tool).symlink_to(found)
        fake_tmux(self.bin)
        self.env = {**self.env, "PATH": str(self.bin), "TMUX_AGENT_LOCK_TRIES": "6"}

    def test_a_mkdir_that_always_fails_gives_up(self):
        (self.bin / "mkdir").write_text('#!/bin/sh\n[ "$1" = -p ] && exit 0\nexit 1\n')
        (self.bin / "mkdir").chmod(0o755)
        t = time.time()
        r = self.run_cli("refresh", "1")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertLess(time.time() - t, 10)
        self.assertFalse((self.state / "stamp").exists())

    def test_a_held_lock_gives_up_after_the_takeover_attempt(self):
        mkdir = shutil.which("mkdir")
        (self.bin / "mkdir").symlink_to(mkdir)
        (self.state / "lock.d").mkdir()
        r = self.run_cli("refresh", "1")
        # The stale lock is taken over halfway, so the scan then runs.
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.state / "stamp").exists())

    def test_a_missing_sleep_does_not_spin(self):
        (self.bin / "sleep").unlink()
        (self.bin / "mkdir").write_text('#!/bin/sh\n[ "$1" = -p ] && exit 0\nexit 1\n')
        (self.bin / "mkdir").chmod(0o755)
        t = time.time()
        r = self.run_cli("refresh", "1")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertLess(time.time() - t, 10)

    def stuck(self):
        """A lock that never frees: mkdir fails, a cached agent exists."""
        (self.bin / "mkdir").write_text('#!/bin/sh\n[ "$1" = -p ] && exit 0\nexit 1\n')
        (self.bin / "mkdir").chmod(0o755)
        (self.state / "stamp").write_text("1\n")
        self.write_state("%1", "blocked")

    def test_readers_fall_back_to_the_cached_state(self):
        self.stuck()
        for args in (("strip",), ("status",), ("status", "--porcelain"), ("pick-rows",),
                     ("mobile-rows",), ("window-dot", "@1"), ("pane-label", "%1")):
            with self.subTest(args=args):
                r = self.run_cli(*args)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertTrue(r.stdout.strip(), args)
        self.assertIn("claude", self.run_cli("strip").stdout)

    def test_a_guarded_send_refuses_when_it_cannot_refresh(self):
        self.stuck()
        for guard in (("--expect-state", "blocked"), ("--expect-epoch", "5")):
            with self.subTest(guard=guard):
                r = self.run_cli("send", "--pane", "%1", *guard, "--key", "y")
                self.assertEqual(r.returncode, 3, r.stderr)
                self.assertIn("send: could not refresh state", r.stderr)
        log = self.bin / "tmux.log"
        self.assertFalse(log.exists() and "send-keys" in log.read_text())

    def test_an_unwritable_state_dir_returns_at_once(self):
        (self.bin / "mkdir").symlink_to(shutil.which("mkdir"))
        self.state.chmod(0o500)
        self.addCleanup(self.state.chmod, 0o700)
        if os.access(self.state, os.W_OK):
            self.skipTest("running as a user who ignores permissions")
        t = time.time()
        r = self.run_cli("refresh", "1")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertLess(time.time() - t, 5)


class MobileTests(Base):
    def setUp(self):
        super().setUp()
        fake_tmux(self.fake)
        (self.state / "stamp").write_text(f"{self.now}\n")
        self.write_state("%1", "blocked")
        self.fzf_log = self.dir / "fzf.log"
        self.curl_log = self.dir / "curl.log"
        self.pid_file = self.dir / "fzf.pid"
        self.fzf = self.fake / "fzf"
        self.fzf.write_text(
            "#!/bin/sh\n"
            'case "$1" in --version) echo "0.74.4 (fake)"; exit 0 ;; '
            '--help) echo "  --listen"; exit 0 ;; esac\n'
            f'echo "$*" >>{self.fzf_log}\n'
            f'echo "key=$FZF_API_KEY" >>{self.fzf_log}\n'
            f'echo $$ >{self.pid_file}\n'
            "cat >/dev/null\n"
            f'sleep "${{FAKE_FZF_SLEEP:-0}}"\n'
            "exit 130\n")
        self.fzf.chmod(0o755)
        curl = self.fake / "curl"
        curl.write_text(f'#!/bin/sh\necho "argv: $*" >>{self.curl_log}\ncat >>{self.curl_log}\n')
        curl.chmod(0o755)

    def test_listen_port_is_keyed_and_the_key_stays_off_argv(self):
        r = self.run_cli("mobile", env={**self.env, "FAKE_FZF_SLEEP": "3"})
        self.assertEqual(r.returncode, 0, r.stderr)
        log = self.fzf_log.read_text()
        self.assertRegex(log, r"--listen=\d+")
        key = log.split("key=")[1].split()[0]
        self.assertRegex(key, r"^[0-9a-f]{32}$")
        curl = self.curl_log.read_text()
        self.assertIn(f"x-api-key: {key}", curl)
        self.assertNotIn(key, curl.split("\n")[0])
        self.assertIn("reload(", curl)

    def test_sigterm_stops_the_ticker_and_exits_130(self):
        p = subprocess.Popen([str(CLI), "mobile"], env={**self.env, "FAKE_FZF_SLEEP": "30"},
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        try:
            deadline = time.time() + 10
            while not self.pid_file.exists() and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(self.pid_file.exists())
            os.killpg(p.pid, signal.SIGTERM)
            self.assertEqual(p.wait(timeout=10), 130)
        finally:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()

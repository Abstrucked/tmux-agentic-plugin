"""Run with: pytest tests/test_notify_dismiss.py

Urgency and timeout of desktop notifications, and closing them (notify-send
-p / -r plus a D-Bus CloseNotification) once the agent is looked at or its
state moves on. notify-send and the D-Bus tools are stand-ins on PATH.
"""

import os
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from test_tmux_agent import CLI, ROOT, bash, fake_tmux

REMOTE = ROOT / "lib" / "remote.sh"
NOTIFY = ROOT / "lib" / "notify.sh"
PANE_FMT = ("#{pane_id}|#{pane_pid}|#{window_id}|"
            "#{session_name}:#{window_index}.#{pane_index}|#{pane_current_path}")


def settle(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class DismissTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ta-dismiss-")
        self.dir = Path(self._tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.state = self.dir / "state"
        self.state.mkdir()
        self.release = self.dir / "release"
        self.send_log = self.dir / "send.log"
        self.dbus_log = self.dir / "dbus.log"
        for tool in ("bash", "tr", "cat", "sed", "grep", "date", "env", "sleep",
                     "mkdir", "rm", "head", "cut", "awk", "touch", "dirname",
                     "basename", "stat", "ps", "id", "uname", "wc", "sort", "printf"):
            found = shutil.which(tool)
            if found:
                (self.bin / tool).symlink_to(found)
        self.addCleanup(self.reap)
        self.tmux_log = fake_tmux(self.bin)
        self.notify_send()

    def reap(self):
        # Let every waiting notify-send stand-in return, then check none is left.
        self.release.touch()
        gone = settle(lambda: subprocess.run(
            ["pgrep", "-f", str(self.dir)], capture_output=True).returncode != 0)
        if not gone:
            subprocess.run(["pkill", "-f", str(self.dir)])
        self._tmp.cleanup()
        self.assertTrue(gone, "processes left behind")

    def tool(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    def notify_send(self):
        # Prints an id with -p (the one given to -r, else 42), logs its
        # arguments, then holds like --wait until the release file appears
        # (CloseNotification stand-ins create it).
        self.tool("notify-send", f"""printf '%s\\n' "$*" >>{shlex.quote(str(self.send_log))}
id=42; prev=
for a; do [ "$prev" = -r ] && id=$a; prev=$a; done
case " $* " in *" -p "*) echo "$id" ;; esac
i=0
while [ ! -e {shlex.quote(str(self.release))} ] && [ $i -lt 100 ]; do sleep 0.1; i=$((i+1)); done
""")

    def dbus(self, name="gdbus"):
        self.tool(name, f'printf \'%s\\n\' "$*" >>{shlex.quote(str(self.dbus_log))}\n'
                        f'touch {shlex.quote(str(self.release))}\n')

    def env(self, extra=None):
        e = {"PATH": str(self.bin), "HOME": str(self.dir), "DISPLAY": ":0",
             "TMUX_AGENT_NOTIFY_METHOD": "desktop", **(extra or {})}
        return {k: v for k, v in e.items() if v is not None}

    def run_snippet(self, code, *args, env=None):
        script = (f'. "{REMOTE}"; . "{NOTIFY}"; SELF=/bin/true; '
                  f'TA_STATE_DIR="{self.state}"; TA_LOCK_FD=; {code}')
        r = bash(script, *args, env=self.env(env))
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def notify(self, state="blocked", pane="%3", env=None):
        self.run_snippet('notify claude "$1" s:1.0 /home/u/proj "$2"', state, pane, env=env)

    def sent(self, minimum=1):
        self.assertTrue(settle(lambda: len(self.lines(self.send_log)) >= minimum),
                        self.lines(self.send_log))
        return self.lines(self.send_log)

    @staticmethod
    def lines(path):
        return path.read_text().splitlines() if path.exists() else []

    # urgency and timeout

    def test_blocked_and_error_default_to_normal(self):
        for state in ("blocked", "error", "ready"):
            self.send_log.write_text("")
            self.notify(state)
            self.assertIn("-u normal ", self.sent()[0])
            self.assertNotIn("critical", self.sent()[0])
            self.release.touch()
            self.assertTrue(settle(lambda: not (self.state / "notif-%3").exists()))
            self.release.unlink()

    def test_critical_option_restores_old_urgency_for_blocked_and_error_only(self):
        for state, urgency in (("blocked", "critical"), ("error", "critical"),
                               ("ready", "normal")):
            self.send_log.write_text("")
            self.notify(state, env={"TMUX_AGENT_NOTIFY_URGENCY": "critical"})
            self.assertIn(f"-u {urgency} ", self.sent()[0])
            self.release.touch()
            self.assertTrue(settle(lambda: not (self.state / "notif-%3").exists()))
            self.release.unlink()

    def test_notify_command_sees_the_urgency_sent(self):
        dump = self.dir / "env.txt"
        for option, expected in (("", "normal"), ("critical", "critical")):
            self.notify(env={"TMUX_AGENT_NOTIFY_METHOD": "terminal",
                             "TMUX_AGENT_NOTIFY_URGENCY": option,
                             "TMUX_AGENT_NOTIFY_COMMAND": f"env >{dump}"})
            self.assertTrue(settle(lambda: dump.exists() and "TA_URGENCY" in dump.read_text()))
            self.assertIn(f"TA_URGENCY={expected}\n", dump.read_text())
            dump.unlink()

    def test_timeout_option_becomes_milliseconds(self):
        self.notify(env={"TMUX_AGENT_NOTIFY_TIMEOUT": "30"})
        self.assertIn("-t 30000 ", self.sent()[0])

    def test_timeout_ignores_non_numbers_and_empty(self):
        for value in ("", "soon", "-5", "3s"):
            self.send_log.write_text("")
            self.notify(env={"TMUX_AGENT_NOTIFY_TIMEOUT": value})
            self.assertNotIn(" -t ", self.sent()[0])
            self.release.touch()
            self.assertTrue(settle(lambda: not (self.state / "notif-%3").exists()))
            self.release.unlink()

    # the stored id, replace and dismiss

    def test_notification_id_is_stored_and_removed_on_close(self):
        self.notify()
        idfile = self.state / "notif-%3"
        self.assertTrue(settle(idfile.exists))
        self.assertEqual(idfile.read_text(), "42\n")
        self.assertIn(" -p ", self.sent()[0])
        self.release.touch()
        self.assertTrue(settle(lambda: not idfile.exists()))

    def test_second_notification_replaces_the_open_one(self):
        self.notify()
        self.assertTrue(settle((self.state / "notif-%3").exists))
        self.notify("error")
        first, second = self.sent(2)
        self.assertNotIn(" -r ", first)
        self.assertIn("-r 42 ", second)

    def test_remote_pane_id_file_stays_inside_the_state_dir(self):
        self.notify(pane="../k/%3")
        self.assertTrue(settle(lambda: any(self.state.glob("notif-*"))))
        files = [p.name for p in self.state.iterdir()]
        self.assertEqual([f for f in files if f.startswith("notif-")], ["notif-.._k_%3"])
        self.assertEqual([p for p in self.dir.iterdir() if p.is_file()
                          and p.name.startswith("notif")], [])
        self.assertFalse((self.dir / "k").exists())

    def test_dismiss_closes_via_gdbus_and_ends_the_waiter(self):
        self.dbus("gdbus")
        self.notify()
        idfile = self.state / "notif-%3"
        self.assertTrue(settle(idfile.exists))
        self.run_snippet('notify_dismiss "$1"', "%3")
        self.assertTrue(settle(lambda: self.dbus_log.exists()))
        self.assertEqual(
            self.lines(self.dbus_log),
            ["call --session --dest org.freedesktop.Notifications "
             "--object-path /org/freedesktop/Notifications "
             "--method org.freedesktop.Notifications.CloseNotification 42"])
        self.assertFalse(idfile.exists())

    def test_dismiss_falls_back_to_busctl_then_dbus_send(self):
        for tool, expected in (
                ("busctl", "--user call org.freedesktop.Notifications "
                           "/org/freedesktop/Notifications org.freedesktop.Notifications "
                           "CloseNotification u 42"),
                ("dbus-send", "--session --dest=org.freedesktop.Notifications "
                              "/org/freedesktop/Notifications "
                              "org.freedesktop.Notifications.CloseNotification uint32:42")):
            with self.subTest(tool=tool):
                self.dbus_log.unlink(missing_ok=True)
                self.release.unlink(missing_ok=True)
                for old in ("gdbus", "busctl", "dbus-send"):
                    (self.bin / old).unlink(missing_ok=True)
                self.dbus(tool)
                (self.state / "notif-%3").write_text("42\n")
                self.run_snippet('notify_dismiss "$1"', "%3")
                self.assertTrue(settle(self.dbus_log.exists))
                self.assertEqual(self.lines(self.dbus_log), [expected])

    def test_dismiss_without_an_open_notification_is_a_no_op(self):
        self.dbus()
        self.run_snippet('notify_dismiss "$1"', "%3")
        time.sleep(0.3)
        self.assertFalse(self.dbus_log.exists())

    # callers

    def cli_env(self, extra=None):
        return {**os.environ, "TMUX_AGENT_STATE_DIR": str(self.state),
                "PATH": f"{self.bin}:{os.environ['PATH']}", **(extra or {})}

    def closed(self, pane_id="42"):
        return settle(lambda: any(pane_id in c and "CloseNotification" in c
                                  for c in self.lines(self.dbus_log)))

    def test_seen_hook_dismisses(self):
        self.dbus()
        (self.state / "notif-%3").write_text("42\n")
        r = subprocess.run([str(CLI), "seen", "%3"], env=self.cli_env(),
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.closed(), self.lines(self.dbus_log))
        self.assertFalse((self.state / "notif-%3").exists())
        self.assertTrue((self.state / "seen-%3").exists())

    def test_seen_hook_for_a_remote_pane_dismisses(self):
        self.dbus()
        self.tool("ssh", "exit 0\n")
        (self.state / "notif-k_%3").write_text("42\n")
        subprocess.run([str(CLI), "seen", "k/%3"], env=self.cli_env(),
                       capture_output=True, text=True, timeout=10)
        self.assertTrue(self.closed(), self.lines(self.dbus_log))
        self.assertFalse((self.state / "notif-k_%3").exists())

    def test_attach_dismisses_the_pane_jumped_to(self):
        self.dbus()
        now = int(time.time())
        (self.state / "stamp").write_text(f"{now}\n")
        (self.state / "state-%3").write_text(
            f"blocked|claude|0|{now}|{now}|main:1.1|@1|/project|1\n")
        (self.state / "notif-%3").write_text("42\n")
        r = subprocess.run([str(CLI), "attach", "--urgent"], env=self.cli_env(),
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.closed(), self.lines(self.dbus_log))
        self.assertFalse((self.state / "notif-%3").exists())

    def test_focus_dismisses_the_pane_jumped_to(self):
        self.dbus()
        fake_tmux(self.bin, {
            "display-message -p -t %3 #{session_name}": "main",
            "list-clients -F #{client_activity}|#{client_name}|#{client_pid}|#{session_name}":
                "200|/dev/pts/4|4343|main\n",
            "show-option -gqv @tmux-agent-raise-command": "off"})
        (self.state / "notif-%3").write_text("42\n")
        r = subprocess.run([str(CLI), "focus", "--pane", "%3"], env=self.cli_env(),
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.closed(), self.lines(self.dbus_log))

    def scan(self, prev, report=None, panes="%3|999999|@1|main:1.1|/src/app\n"):
        now = int(time.time())
        fake_tmux(self.bin, {f"list-panes -a -F {PANE_FMT}": panes})
        (self.state / "stamp").write_text(f"{now - 10}\n")
        (self.state / "state-%3").write_text(
            f"{prev}|custom|0|{now - 10}|{now}|main:1.1|@1|/src/app|1\n")
        if report:
            (self.state / "report-%3").write_text(f"{report}|{now}\n")
        (self.state / "notif-%3").write_text("42\n")
        e = self.cli_env({"TMUX_AGENT_NOTIFY_METHOD": "off"})
        e.pop("TMUX_AGENT_QUIET", None)
        r = subprocess.run([str(CLI), "refresh", "1"], env=e, capture_output=True,
                           text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_scan_dismisses_when_a_blocked_agent_goes_on_working(self):
        self.dbus()
        self.scan("blocked", report="working")
        self.assertTrue(self.closed(), self.lines(self.dbus_log))
        self.assertFalse((self.state / "notif-%3").exists())

    def test_scan_dismisses_when_an_error_clears(self):
        self.dbus()
        self.scan("error", report="idle")
        self.assertTrue(self.closed(), self.lines(self.dbus_log))

    def test_scan_keeps_the_notification_while_the_state_holds(self):
        self.dbus()
        self.scan("blocked", report="blocked")
        time.sleep(0.4)
        self.assertFalse(self.dbus_log.exists())
        self.assertTrue((self.state / "notif-%3").exists())

    def test_scan_dismisses_when_the_pane_disappears(self):
        self.dbus()
        self.scan("blocked", report="blocked", panes="")
        self.assertTrue(self.closed(), self.lines(self.dbus_log))
        self.assertFalse((self.state / "notif-%3").exists())

    def test_remote_state_change_dismisses(self):
        self.dbus()
        before, after = self.dir / "before", self.dir / "after"
        before.write_text("blocked|claude|%3|main:1.2|@2|0|/src/api\n")
        after.write_text("working|claude|%3|main:1.2|@2|0|/src/api\n")
        (self.state / "notif-k_%3").write_text("42\n")
        self.run_snippet('ta_remote_notify k devbox "$1" "$2"', str(before), str(after),
                         env={"TMUX_AGENT_QUIET": ""})
        self.assertTrue(self.closed(), self.lines(self.dbus_log))
        self.assertFalse((self.state / "notif-k_%3").exists())


if __name__ == "__main__":
    unittest.main()

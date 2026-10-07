"""Run with: pytest tests/test_notify.py

Content, method selection and escaping of lib/notify.sh, with notify-send and
tmux replaced by stand-ins on PATH.
"""

import os
import shlex
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from test_tmux_agent import ROOT, bash, fake_tmux

REMOTE = ROOT / "lib" / "remote.sh"
NOTIFY = ROOT / "lib" / "notify.sh"
ESC, BEL = "\x1b", "\x07"


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.state = self.dir / "state"
        self.state.mkdir()
        self.tty = self.dir / "tty"
        self.tty.write_text("")
        # PATH holds only the stand-ins and symlinks to the tools the snippet
        # needs, so a host's real notify-send or osascript never leaks in.
        for tool in ("bash", "tr", "cat", "sed", "grep", "date", "env", "sleep",
                     "mkdir", "rm", "head", "cut", "awk", "touch", "dirname",
                     "basename", "stat", "ps", "id", "uname", "wc", "sort", "printf"):
            found = shutil.which(tool)
            if found:
                (self.bin / tool).symlink_to(found)

    def tmux(self, replies=None):
        return fake_tmux(self.bin, replies)

    def notify_send(self):
        log = self.dir / "send.log"
        script = self.bin / "notify-send"
        script.write_text('#!/bin/sh\nfor a; do printf "%s\\n" "$a"; done '
                          f'>>{shlex.quote(str(log))}\nprintf "\\n--\\n" '
                          f'>>{shlex.quote(str(log))}\n')
        script.chmod(0o755)
        return log

    def run_notify(self, agent="claude", state="blocked", pane="%3", env=None,
                   path="/home/u/proj", self_cmd="/bin/true"):
        # PATH is just the stand-ins plus the system tools bash needs, so a
        # real notify-send or osascript never leaks in.
        e = {"PATH": str(self.bin), "HOME": str(self.dir),
             "DISPLAY": ":0", **(env or {})}
        keys = ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS")
        unset = " ".join(k for k in keys if e.get(k) is None)
        for k in keys:
            if e.get(k) is None:
                e.pop(k, None)
        script = (f'unset {unset or "_ta_none"}; . "{REMOTE}"; . "{NOTIFY}"; SELF={shlex.quote(self_cmd)}; '
                  f'TA_STATE_DIR="{self.state}"; TA_LOCK_FD=; '
                  'notify "$@"; wait')
        r = bash(script, agent, state, "s:1.0", path, pane, env=e)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def wait_for(self, path, minimum=1):
        end = time.time() + 3
        while time.time() < end:
            if path.exists() and len(path.read_bytes()) >= minimum:
                time.sleep(0.05)
                return path.read_bytes()
            time.sleep(0.05)
        self.fail(f"{path} never written")

    def test_notify_send_content_per_state(self):
        self.tmux()
        log = self.notify_send()
        cases = {"blocked": ("needs input", "critical"),
                 "ready": ("finished", "normal"),
                 "error": ("hit an error", "critical")}
        for state, (what, urgency) in cases.items():
            log.write_text("")
            self.run_notify(state=state, env={"TMUX_AGENT_NOTIFY_METHOD": "desktop"})
            lines = self.wait_for(log).decode().split("\n")
            self.assertIn(urgency, lines)
            self.assertIn(f"claude {what}", lines)
            self.assertIn("proj · s:1.0", lines)

    def test_detail_and_markup_escaping(self):
        self.tmux()
        log = self.notify_send()
        (self.state / "detail-%3").write_text("1|perm|run <b>a&b</b>?\n")
        self.run_notify(env={"TMUX_AGENT_NOTIFY_METHOD": "desktop"})
        out = self.wait_for(log).decode()
        self.assertIn("run &lt;b&gt;a&amp;b&lt;/b&gt;?\nproj · s:1.0", out)

    def test_method_off_and_notify_off_are_silent(self):
        log = self.notify_send()
        self.tmux()
        self.run_notify(env={"TMUX_AGENT_NOTIFY_METHOD": "off",
                             "TMUX_AGENT_NOTIFY_COMMAND": f"touch {self.dir}/ran"})
        self.tmux({"show-option -gqv @tmux-agent-notify": "off"})
        self.run_notify(env={"TMUX_AGENT_NOTIFY_METHOD": "desktop",
                             "TMUX_AGENT_NOTIFY_COMMAND": f"touch {self.dir}/ran"})
        time.sleep(0.5)
        self.assertFalse(log.exists())
        self.assertFalse((self.dir / "ran").exists())

    def terminal(self, term, env=None, state="blocked"):
        self.tmux({"list-clients -F #{client_tty}|#{client_termname}":
                   f"{self.tty}|{term}\n"})
        self.run_notify(state=state, env={"TMUX_AGENT_NOTIFY_METHOD": "terminal",
                                          **(env or {})})
        return self.wait_for(self.tty).decode()

    def test_kitty_osc99(self):
        out = self.terminal("xterm-kitty")
        self.assertEqual(
            out, f"{ESC}]99;i=ta3:d=0;claude needs input{ESC}\\"
                 f"{ESC}]99;i=ta3:p=body;proj · s:1.0{ESC}\\")

    def test_foot_osc777_strips_semicolons(self):
        (self.state / "detail-%3").write_text("1|perm|a;b\n")
        out = self.terminal("foot")
        self.assertEqual(out, f"{ESC}]777;notify;claude needs input;ab proj · s:1.0{BEL}")

    def test_other_terminals_osc9_and_override(self):
        self.assertEqual(self.terminal("xterm-ghostty", state="ready"),
                         f"{ESC}]9;claude finished: proj · s:1.0{BEL}")
        self.tty.write_text("")
        out = self.terminal("xterm-ghostty", env={"TMUX_AGENT_OSC": "777"})
        self.assertEqual(out, f"{ESC}]777;notify;claude needs input;proj · s:1.0{BEL}")

    def test_control_characters_never_reach_tty(self):
        (self.state / "detail-%3").write_text(
            "1|perm|rm\x1b]0;pwn\x07 -rf\nnext\n")
        out = self.terminal("xterm-ghostty")
        self.assertEqual(out.count(ESC), 1)
        self.assertEqual(out.count(BEL), 1)
        self.assertNotIn("\n", out)

    def test_auto_picks_terminal_without_display(self):
        self.tmux({"list-clients -F #{client_tty}|#{client_termname}":
                   f"{self.tty}|xterm-ghostty\n"})
        log = self.notify_send()
        self.run_notify(env={"DISPLAY": None})
        self.assertIn(f"{ESC}]9;", self.wait_for(self.tty).decode())
        self.assertFalse(log.exists())

    def test_auto_picks_osascript_without_display(self):
        self.tmux()
        log = self.dir / "osa.log"
        osa = self.bin / "osascript"
        osa.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >>{shlex.quote(str(log))}\n')
        osa.chmod(0o755)
        self.run_notify(env={"DISPLAY": None})
        self.assertIn("claude needs input", self.wait_for(log).decode())
        self.assertEqual(self.tty.read_text(), "")

    def test_auto_picks_desktop_with_display(self):
        self.tmux()
        log = self.notify_send()
        self.run_notify(env={"DISPLAY": ":0"})
        self.assertIn("claude needs input", self.wait_for(log).decode())

    DBUS = "unix:path=/run/user/1000/bus"
    NO_DISPLAY = {"DISPLAY": None, "WAYLAND_DISPLAY": None}

    def test_auto_picks_desktop_with_dbus_only(self):
        self.tmux()
        log = self.notify_send()
        self.run_notify(env={**self.NO_DISPLAY, "DBUS_SESSION_BUS_ADDRESS": self.DBUS})
        self.assertIn("claude needs input", self.wait_for(log).decode())
        self.assertEqual(self.tty.read_text(), "")

    def test_auto_picks_desktop_with_dbus_in_tmux_env(self):
        self.tmux({"show-environment -g DBUS_SESSION_BUS_ADDRESS":
                   f"DBUS_SESSION_BUS_ADDRESS={self.DBUS}\n"})
        log = self.notify_send()
        self.run_notify(env=self.NO_DISPLAY)
        self.assertIn("claude needs input", self.wait_for(log).decode())

    def test_auto_picks_terminal_without_dbus_or_display(self):
        self.tmux({"list-clients -F #{client_tty}|#{client_termname}":
                   f"{self.tty}|xterm-ghostty\n"})
        log = self.notify_send()
        self.run_notify(env=self.NO_DISPLAY)
        self.assertIn(f"{ESC}]9;", self.wait_for(self.tty).decode())
        self.assertFalse(log.exists())

    def fake_self(self, detail="run the tests?"):
        calls = self.dir / "self.log"
        script = self.dir / "fake-self"
        script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >>{shlex.quote(str(calls))}\n'
                          f'printf "%s\\n" {shlex.quote(detail)}\n')
        script.chmod(0o755)
        return script, calls

    def test_remote_pane_fetches_detail_through_self(self):
        self.tmux()
        log = self.notify_send()
        script, calls = self.fake_self("run <b>x</b>?")
        self.run_notify(pane="k/%3", self_cmd=str(script),
                        env={"TMUX_AGENT_NOTIFY_METHOD": "desktop"})
        out = self.wait_for(log).decode()
        self.assertIn("run &lt;b&gt;x&lt;/b&gt;?\nproj · s:1.0", out)
        self.assertEqual(calls.read_text(), "detail --pane k/%3\n")

    def test_remote_pane_detail_reaches_notify_command(self):
        self.tmux()
        dump = self.dir / "env.txt"
        script, _ = self.fake_self()
        self.run_notify(pane="k/%3", self_cmd=str(script), env={
            "TMUX_AGENT_NOTIFY_METHOD": "terminal",
            "TMUX_AGENT_NOTIFY_COMMAND": f"env >{dump}"})
        got = dict(line.split("=", 1) for line in
                   self.wait_for(dump).decode().splitlines() if "=" in line)
        self.assertEqual(got["TA_BODY"], "run the tests? proj · s:1.0")
        self.assertEqual(got["TA_PANE"], "k/%3")

    def test_remote_pane_without_detail_still_notifies(self):
        self.tmux()
        log = self.notify_send()
        self.run_notify(pane="k/%3", self_cmd="/bin/false",
                        env={"TMUX_AGENT_NOTIFY_METHOD": "desktop"})
        self.assertIn("proj · s:1.0", self.wait_for(log).decode())

    def test_local_pane_never_calls_self(self):
        self.tmux()
        log = self.notify_send()
        script, calls = self.fake_self()
        (self.state / "detail-%3").write_text("1|perm|local one\n")
        self.run_notify(self_cmd=str(script), env={"TMUX_AGENT_NOTIFY_METHOD": "desktop"})
        self.assertIn("local one\nproj", self.wait_for(log).decode())
        self.assertFalse(calls.exists())

    def test_notify_command_env_contract(self):
        self.tmux()
        dump = self.dir / "env.txt"
        self.run_notify(env={"TMUX_AGENT_NOTIFY_METHOD": "terminal",
                             "TMUX_AGENT_NOTIFY_COMMAND": f"env >{dump}"})
        got = dict(line.split("=", 1) for line in
                   self.wait_for(dump).decode().splitlines() if "=" in line)
        self.assertEqual(got["TA_TITLE"], "claude needs input")
        self.assertEqual(got["TA_BODY"], "proj · s:1.0")
        self.assertEqual(got["TA_STATE"], "blocked")
        self.assertEqual(got["TA_AGENT"], "claude")
        self.assertEqual(got["TA_PANE"], "%3")
        self.assertEqual(got["TA_PATH"], "/home/u/proj")
        self.assertEqual(got["TA_URGENCY"], "critical")

    def test_notify_command_never_interpolates_data(self):
        self.tmux()
        dump = self.dir / "env.txt"
        evil = f"$(touch {self.dir}/pwned)"
        self.run_notify(agent=evil, env={
            "TMUX_AGENT_NOTIFY_METHOD": "terminal",
            "TMUX_AGENT_NOTIFY_COMMAND": f'printf %s "$TA_TITLE" >{dump}'})
        self.assertIn("$(touch", self.wait_for(dump).decode())
        time.sleep(0.3)
        self.assertFalse((self.dir / "pwned").exists())


if __name__ == "__main__":
    unittest.main()

"""Picker rows, fzf binds and the actions the binds run (lib/pick.sh)."""

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tmux_agent import CLI, ENGINE, ROOT, fake_tmux  # noqa: E402

PICK = ROOT / "lib" / "pick.sh"
KEY = "me@devbox.example:22"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


class PickTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ta-pickt-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.state = self.dir / "state"
        self.state.mkdir()
        self.fake = self.dir / "bin"
        self.fake.mkdir()
        self.now = int(time.time())
        (self.state / "stamp").write_text(f"{self.now}\n")
        self.env = {
            **os.environ,
            "TMUX_AGENT_QUIET": "1",
            "TMUX_AGENT_STATE_DIR": str(self.state),
            "PATH": f"{self.fake}:{os.environ['PATH']}",
        }
        fake_tmux(self.fake)

    def local(self, pane, state, agent="claude", path="/src/app"):
        (self.state / f"state-{pane}").write_text(
            f"{state}|{agent}|0|{self.now}|{self.now}|main:1.1|@1|{path}|1\n")

    def remote(self, line="blocked|claude|%3|main:1.2|@2|0|/src/api\n"):
        (self.state / f"remote-{KEY}").write_text(line)
        (self.state / f"remote-{KEY}.meta").write_text(f"ok|{self.now}\n")
        (self.state / "remotes").write_text(f"{KEY}|devbox|\n")
        (self.state / "remote.stamp").write_text(f"{self.now}\n")
        self.env["TMUX_AGENT_REMOTES"] = "devbox"

    def fzf(self, version="0.74.4 (test)", help_text="--listen"):
        """A fzf that records argv (one per line) and stdin, then cancels."""
        (self.fake / "fzf").write_text(
            "#!/bin/sh\n"
            f'case "$1" in --version) echo "{version}"; exit 0;; '
            f'--help) echo "{help_text}"; exit 0;; esac\n'
            f'printf "%s\\n" "$@" >{shlex.quote(str(self.dir / "argv"))}\n'
            f'printf "%s" "${{FZF_API_KEY:-}}" >{shlex.quote(str(self.dir / "apikey"))}\n'
            f'cat >{shlex.quote(str(self.dir / "rows"))}\nexit 130\n')
        (self.fake / "fzf").chmod(0o755)
        (self.fake / "curl").write_text("#!/bin/sh\nexit 1\n")
        (self.fake / "curl").chmod(0o755)

    def pick(self):
        r = subprocess.run([str(CLI), "pick"], capture_output=True, text=True,
                           env=self.env, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def argv(self):
        return (self.dir / "argv").read_text().splitlines()

    def rows(self):
        return (self.dir / "rows").read_text().splitlines()

    def test_rows_carry_a_dim_sanitized_detail_and_hidden_fields(self):
        self.local("%1", "blocked", "claude")
        long = "Bash: " + "x" * 100
        (self.state / "detail-%1").write_text(
            f"{self.now}|PermissionRequest|{long}\x1b[31m bad\n")
        self.local("%2", "idle", "codex")
        self.fzf()
        self.pick()
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        first = rows[0].split("\t")
        self.assertEqual(first[1:], ["%1", "claude", str(self.now)])
        self.assertIn("\x1b[2m", first[0])
        self.assertTrue(first[0].endswith("\x1b[0m"))
        detail = first[0].split("\x1b[2m")[1][:-len("\x1b[0m")]
        self.assertTrue(detail.startswith("Bash: xxx"), detail)
        self.assertLessEqual(len(detail), 60)
        self.assertNotIn("\x1b", detail)
        # No detail file: no dim column.
        second = rows[1].split("\t")
        self.assertNotIn("\x1b[2m", second[0])
        self.assertEqual(second[1:], ["%2", "codex", ""])

    def test_remote_rows_have_no_detail_and_a_bare_agent_field(self):
        self.remote()
        self.local("%1", "idle")
        # A detail file under the remote's id must not be picked up.
        (self.state / f"detail-{KEY}%3").write_text(f"{self.now}|Stop|nope\n")
        self.fzf()
        self.pick()
        rows = [r.split("\t") for r in self.rows()]
        self.assertEqual(rows[0][1:], [f"{KEY}/%3", "claude", ""])
        self.assertIn("claude@devbox", ANSI.sub("", rows[0][0]))
        self.assertNotIn("\x1b[2m", rows[0][0])
        self.assertNotIn("nope", rows[0][0])

    def test_binds_header_and_preview(self):
        self.local("%1", "blocked")
        self.fzf()
        self.pick()
        args = self.argv()
        joined = "\n".join(args)
        self.assertIn("--with-nth=1", args)
        header = args[args.index("--header") + 1]
        for key in ("enter go", "^y approve", "^n deny", "^e reply", "^s seen",
                    "^d diff", "^l reload"):
            self.assertIn(key, header)
        binds = [a for a in args if re.match(r"ctrl-[a-z]:", a)]
        self.assertEqual(sorted(b[:6] for b in binds),
                         ["ctrl-d", "ctrl-e", "ctrl-l", "ctrl-n", "ctrl-s", "ctrl-y"])
        by_key = {b[:6]: b for b in binds}
        self.assertIn("pick-action approve {2} {3} {4}", by_key["ctrl-y"])
        self.assertIn("pick-action deny {2} {3}", by_key["ctrl-n"])
        self.assertIn("pick-action reply {2} {3}", by_key["ctrl-e"])
        self.assertIn("pick-action seen {2} {3}", by_key["ctrl-s"])
        self.assertIn("refresh-preview", by_key["ctrl-d"])
        self.assertIn("pick-rows)", by_key["ctrl-l"])
        for k in ("ctrl-y", "ctrl-n", "ctrl-e", "ctrl-s"):
            self.assertIn("reload(", by_key[k])
        self.assertIn("transform-header", by_key["ctrl-y"])
        self.assertIn("pick-action preview {2} {3}", joined)

    def test_no_row_text_leaks_into_binds(self):
        self.local("%1", "blocked", path="/src/needle-project")
        (self.state / "detail-%1").write_text(
            f"{self.now}|Stop|$(touch /tmp/pwned) needle-detail\n")
        self.fzf()
        self.pick()
        args = [a for a in self.argv() if a.startswith(("ctrl-", "--preview", "--header"))
                or a.startswith("{")]
        self.assertTrue(args)
        for a in args:
            self.assertNotIn("needle", a)
            self.assertNotIn("pwned", a)

    def test_listen_ticker_uses_a_random_api_key(self):
        self.local("%1", "idle")
        self.fzf()
        self.pick()
        self.assertTrue(any(a.startswith("--listen=") for a in self.argv()))
        key = (self.dir / "apikey").read_text()
        self.assertRegex(key, r"^[0-9a-f]{32}$")
        self.assertNotIn(key, "\n".join(self.argv()))

    def test_no_listen_without_curl_support_or_new_enough_fzf(self):
        self.local("%1", "idle")
        self.fzf(help_text="--no-such-flag")
        self.pick()
        self.assertFalse(any(a.startswith("--listen") for a in self.argv()))
        self.assertEqual((self.dir / "apikey").read_text(), "")
        self.fzf(version="0.39.0")
        self.pick()
        self.assertFalse(any(a.startswith("--listen") for a in self.argv()))
        # Old fzf: no transform-header, keys still bound.
        binds = [a for a in self.argv() if a.startswith("ctrl-y:")]
        self.assertTrue(binds)
        self.assertNotIn("transform-header", binds[0])

    def test_interrupt_removes_the_temp_dir(self):
        self.local("%1", "idle")
        (self.fake / "fzf").write_text(
            '#!/bin/sh\ncase "$1" in --version) echo 0.74.4; exit 0;; '
            '--help) echo --listen; exit 0;; esac\n'
            f'echo "$TA_PICK_DIR" >{shlex.quote(str(self.dir / "pickdir"))}\n'
            # $PPID is the command substitution's subshell; its parent is pick.
            'kill -TERM "$(ps -o ppid= -p "$PPID" | tr -d " ")"\nsleep 5\n')
        (self.fake / "fzf").chmod(0o755)
        (self.fake / "curl").write_text("#!/bin/sh\nexit 1\n")
        (self.fake / "curl").chmod(0o755)
        r = subprocess.run([str(CLI), "pick"], capture_output=True, text=True,
                           env={**self.env, "TMPDIR": str(self.dir)}, timeout=20)
        self.assertEqual(r.returncode, 130, r.stderr)
        made = Path((self.dir / "pickdir").read_text().strip())
        self.assertEqual(made.parent, self.dir)
        self.assertFalse(made.exists())

    def test_enter_goes_to_the_pane_field(self):
        self.local("%5", "ready", "codex")
        (self.fake / "fzf").write_text(
            '#!/bin/sh\ncase "$1" in --version) echo 0.74.4; exit 0;; '
            '--help) exit 0;; esac\nhead -n 1\n')
        (self.fake / "fzf").chmod(0o755)
        self.pick()
        calls = (self.fake / "tmux.log").read_text().splitlines()
        self.assertIn("switch-client -t %5", calls)

    # The actions the binds run, with SELF replaced by a recorder.
    def action(self, *args, tty_input=None):
        selflog = self.dir / "self.log"
        fake_self = self.dir / "self"
        fake_self.write_text(
            f'#!/bin/sh\nprintf "%s\\n" "$*" >>{shlex.quote(str(selflog))}\n'
            f'[ -z "$FAKE_SELF_FAIL" ] || {{ echo "$FAKE_SELF_FAIL" >&2; exit 3; }}\n')
        fake_self.chmod(0o755)
        hdr = self.dir / "pickdir"
        hdr.mkdir(exist_ok=True)
        script = (f'. "{ENGINE}"; SELF={shlex.quote(str(fake_self))}; '
                  f'TA_PICK_DIR={shlex.quote(str(hdr))}; . "{PICK}"; pick_msg; '
                  'pick_action "$@"')
        r = subprocess.run(["bash", "-c", script, "ta", *args], capture_output=True,
                           text=True, env={**self.env, **self.extra_env},
                           input=tty_input, timeout=20)
        calls = selflog.read_text().splitlines() if selflog.exists() else []
        header = (hdr / "header").read_text().splitlines()
        return r, calls, header

    extra_env = {}

    def test_approve_sends_the_agents_key_guarded_by_state(self):
        r, calls, header = self.action("approve", "%1", "claude")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(calls, ["send --pane %1 --expect-state blocked --key 1"])
        self.assertEqual(len(header), 1)

    def test_agent_without_approve_key_says_so_and_sends_nothing(self):
        r, calls, header = self.action("approve", "%1", "gemini")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(calls, [])
        self.assertEqual(header[1], "no approve key for gemini")

    def test_refusal_reason_lands_in_the_header(self):
        self.extra_env = {"FAKE_SELF_FAIL": "send: pane changed: now working"}
        r, calls, header = self.action("approve", "%1", "claude")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(header[1], "send: pane changed: now working")

    def test_deny_has_no_state_guard(self):
        r, calls, _ = self.action("deny", "%1", "claude")
        self.assertEqual(calls, ["send --pane %1 --key Escape"])

    def test_reply_types_text_then_enter_and_skips_empty(self):
        r, calls, _ = self.action("reply", "%1", "claude", tty_input="hello world\n")
        self.assertEqual(calls, ["send --pane %1 --text hello world --key Enter"])
        r, calls, _ = self.action("reply", "%2", "claude", tty_input="\n")
        self.assertEqual(len(calls), 1)

    def test_approve_passes_the_epoch_only_when_present(self):
        _, calls, _ = self.action("approve", "%1", "claude", "1234")
        self.assertEqual(
            calls, ["send --pane %1 --expect-state blocked --expect-epoch 1234 --key 1"])
        _, calls, _ = self.action("approve", "%1", "claude", "")
        self.assertEqual(calls[1:], ["send --pane %1 --expect-state blocked --key 1"])

    def test_seen_forwards_remote_panes(self):
        _, calls, _ = self.action("seen", "%1", "claude")
        self.assertEqual(calls, ["seen %1"])
        _, calls, _ = self.action("seen", f"{KEY}/%3", "claude")
        self.assertEqual(calls, ["seen %1", f"seen {KEY}/%3"])

    def test_remote_preview_shows_the_detail_line(self):
        _, calls, _ = self.action("preview", f"{KEY}/%3", "claude")
        self.assertEqual(calls[0], f"detail --pane {KEY}/%3")

    def listen(self, fzf_version, help_text):
        """Run fzf_listen_start/stop; returns the shell's report and curl's argv log."""
        self.fzf(version=fzf_version, help_text=help_text)
        curl_log = self.dir / "curl.log"
        (self.fake / "curl").write_text(
            f'#!/bin/sh\nprintf "%s\\n" "$*" >>{shlex.quote(str(curl_log))}\ncat >/dev/null\n')
        script = (f'. "{ENGINE}"; . "{PICK}"; fzf_listen_start "reload(x)"; '
                  'echo "ARGS=${FZF_LISTEN_ARGS[*]}"; echo "N=${#FZF_LISTEN_ARGS[@]}"; echo "KEY=${FZF_API_KEY:-}"; '
                  'echo "TICKER=$FZF_LISTEN_TICKER"; '
                  '[ -z "$FZF_LISTEN_TICKER" ] || { kill -0 "$FZF_LISTEN_TICKER" && echo ALIVE; }; '
                  'sleep 2.5; p=$FZF_LISTEN_TICKER; fzf_listen_stop; echo "AFTER=$FZF_LISTEN_TICKER"; '
                  '[ -z "$p" ] || { sleep 0.2; kill -0 "$p" 2>/dev/null && echo STILL || echo DEAD; }')
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                           env=self.env, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
        return r.stdout, out, curl_log

    def test_fzf_listen_start_and_stop(self):
        stdout, out, curl_log = self.listen("0.74.4 (test)", "--listen")
        self.assertRegex(out["ARGS"], r"^--listen=\d+$")
        self.assertRegex(out["KEY"], r"^[0-9a-f]{32}$")
        self.assertIn("ALIVE", stdout)
        self.assertIn("DEAD", stdout)
        self.assertEqual(out["AFTER"], "")
        logged = curl_log.read_text()
        self.assertIn("-XPOST", logged)
        self.assertNotIn(out["KEY"], logged)

    def test_fzf_listen_start_with_old_fzf_does_nothing(self):
        stdout, out, curl_log = self.listen("0.39.0", "--listen")
        self.assertEqual(out["ARGS"], "")
        self.assertEqual(out["KEY"], "")
        self.assertEqual(out["TICKER"], "")
        self.assertNotIn("ALIVE", stdout)
        self.assertFalse(curl_log.exists())

    def test_ctrl_d_toggles_the_preview_between_pane_and_diff(self):
        _, calls, _ = self.action("preview", "%1", "claude")
        self.assertEqual(calls, ["detail --pane %1", "read --pane %1 --lines 20 --ansi"])
        self.action("toggle", "%1", "claude")
        self.assertTrue((self.dir / "pickdir" / "diff").exists())
        _, calls, _ = self.action("preview", "%1", "claude")
        self.assertEqual(calls[-1], "diff --pane %1")


if __name__ == "__main__":
    unittest.main()

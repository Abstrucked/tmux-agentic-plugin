"""Run with: pytest tests (or python3 -B -m unittest discover -s tests).

Classification fixtures, detection checks and CLI smoke tests for
lib/engine.sh, bin/tmux-agent, lib/install-hooks and the TPM entry point.
"""

import json
import os
import re
import shlex
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "lib" / "engine.sh"
CLI = ROOT / "bin" / "tmux-agent"
ENTRY = ROOT / "tmux-agentic.tmux"


def bash(script, *args, env=None):
    """Run script under bash with the engine sourced; args arrive as $1..."""
    prelude = f'set -euo pipefail; . "{ENGINE}"; '
    e = {**os.environ, "TMUX_AGENT_QUIET": "1", **(env or {})}
    return subprocess.run(
        ["bash", "-c", prelude + script, "ta", *args],
        capture_output=True, text=True, env=e,
    )


def fake_tmux(directory, replies=None):
    """Put a tmux stand-in in directory: it logs each call's arguments and
    answers the calls listed in replies ("args joined by spaces" -> output).
    Returns the log path."""
    log = Path(directory) / "tmux.log"
    cases = "".join(f"{shlex.quote(k)}) printf '%s' {shlex.quote(v)} ;;\n"
                    for k, v in (replies or {}).items())
    script = Path(directory) / "tmux"
    script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >>{shlex.quote(str(log))}\n'
                      f'case "$*" in\n{cases}esac\n')
    script.chmod(0o755)
    return log


class ClassifyTests(unittest.TestCase):
    def classify(self, tail, cpu=0, prev="", seen=0, output_changed=0):
        r = bash('ta_classify "$1" "$2" "$3" "$4" "$5"',
                 tail, str(cpu), prev, str(seen), str(output_changed))
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_blocked_prompt_wants_input(self):
        self.assertEqual(self.classify("Do you want to proceed?"), "blocked")

    def test_blocked_yn_prompt(self):
        self.assertEqual(self.classify("Create this file? [y/n]"), "blocked")

    def test_blocked_numbered_choice(self):
        self.assertEqual(self.classify(" 1. Yes"), "blocked")

    def test_idle_cpu_wakeup_does_not_mark_agent_working(self):
        self.assertEqual(self.classify("just some output", cpu=200, prev="idle"), "idle")

    def test_cpu_and_visible_output_change_mark_working(self):
        self.assertEqual(
            self.classify("new output", cpu=200, prev="idle", output_changed=1),
            "working",
        )

    def test_small_cpu_delta_does_not_mark_working_even_if_output_changes(self):
        self.assertEqual(
            self.classify("new output", cpu=5, prev="idle", output_changed=1),
            "idle",
        )

    def test_claude_thinking_ellipsis_is_working(self):
        self.assertEqual(self.classify("✶ Doodling…"), "working")

    def test_working_marker_esc_to_interrupt(self):
        self.assertEqual(self.classify("Refactoring... esc to interrupt"), "working")

    def test_working_marker_spinner(self):
        self.assertEqual(self.classify("⠹ thinking about it"), "working")

    def test_ready_on_done_marker(self):
        self.assertEqual(self.classify("✓ Done"), "ready")

    def test_ready_on_completed_sentence(self):
        self.assertEqual(self.classify("All tests completed."), "ready")

    def test_ready_and_seen_is_idle(self):
        self.assertEqual(self.classify("✓ Done", seen=1), "idle")

    def test_working_goes_quiet_becomes_ready(self):
        self.assertEqual(self.classify("some output", prev="working"), "ready")

    def test_quiet_shell_is_idle(self):
        self.assertEqual(self.classify("$ ls"), "idle")

    def test_previous_state_persists(self):
        self.assertEqual(self.classify("$ ", prev="blocked"), "blocked")

    def test_seen_does_not_clear_blocked(self):
        self.assertEqual(self.classify("Approve this change? ", seen=1), "blocked")

    def test_cpu_wins_over_prompt_text(self):
        # An approval prompt takes precedence over CPU churn from a TUI.
        self.assertEqual(self.classify("Do you want to proceed?", cpu=50), "blocked")

    def test_negative_delta_is_not_working(self):
        self.assertEqual(self.classify("$ ", cpu=-40, prev="working"), "ready")


class RollupTests(unittest.TestCase):
    def test_blocked_is_most_urgent(self):
        r = bash('ta_rollup working blocked ready idle')
        self.assertEqual(r.stdout.strip(), "blocked")

    def test_working_beats_ready(self):
        r = bash('ta_rollup ready idle working')
        self.assertEqual(r.stdout.strip(), "working")

    def test_ready_beats_idle(self):
        r = bash('ta_rollup idle ready')
        self.assertEqual(r.stdout.strip(), "ready")

    def test_empty_input_is_empty(self):
        r = bash('ta_rollup "" ""')
        self.assertEqual(r.stdout.strip(), "")

    def test_ranks_are_ordered(self):
        r = bash('echo "$(ta_state_rank blocked) $(ta_state_rank working) '
                 '$(ta_state_rank ready) $(ta_state_rank idle) $(ta_state_rank other)"')
        self.assertEqual(r.stdout.strip(), "0 1 2 3 4")

    def test_state_colors(self):
        r = bash('echo "$(ta_state_color blocked) $(ta_state_color working) '
                 '$(ta_state_color ready) $(ta_state_color idle)"')
        self.assertEqual(r.stdout.strip(), "red yellow blue green")

    def test_state_marks_are_distinct_and_reset_colour(self):
        r = bash('ta_state_mark ready')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "\x1b[34m◉\x1b[0m")
        r = bash('for s in blocked working ready idle; do ta_state_mark "$s"; echo; done')
        marks = [re.sub(r"\x1b\[[0-9;]*m", "", m) for m in r.stdout.splitlines()]
        self.assertEqual(marks, ["◆", "●", "◉", "○"])


class GlyphTests(unittest.TestCase):
    def test_default_glyphs_differ(self):
        # OpenCode shares the generic agent glyph until Nerd Fonts has its own.
        r = bash('ta_agent_glyph claude; ta_agent_glyph codex; ta_agent_glyph opencode; ta_agent_glyph other')
        glyphs = r.stdout.split()
        self.assertEqual(len(set(glyphs)), 3)
        self.assertEqual(glyphs[2], glyphs[3])

    def test_glyph_env_override(self):
        r = bash('ta_agent_glyph claude', env={"TMUX_AGENT_ICON_CLAUDE": "X"})
        self.assertEqual(r.stdout.strip(), "X")

    def test_default_codicons(self):
        r = bash('ta_agent_glyph claude; ta_agent_glyph codex; ta_agent_glyph opencode')
        self.assertEqual(r.stdout.split(), ["\uec82", "\uec81", "\uec67"])

    def test_agent_name_from_comm(self):
        r = bash('ta_agent_name_of claude-code ""')
        self.assertEqual(r.stdout.strip(), "claude")

    def test_agent_name_from_argv_path(self):
        r = bash('ta_agent_name_of node "/usr/lib/claude/dist/cli.js"')
        self.assertEqual(r.stdout.strip(), "claude")

    def test_agent_name_needs_word_boundary(self):
        r = bash('ta_agent_name_of vim "vim my-claude-notes.md"')
        self.assertEqual(r.returncode, 1)

    def test_unknown_process_has_no_agent(self):
        r = bash('ta_agent_name_of bash "bash"')
        self.assertEqual(r.returncode, 1)

    def test_agent_name_from_comm_path(self):
        # macOS ps reports comm as the executable's path.
        r = bash('ta_agent_name_of /opt/homebrew/bin/codex ""')
        self.assertEqual(r.stdout.strip(), "codex")


class DetectTests(unittest.TestCase):
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

    def test_detects_agent_running_directly(self):
        proc = self.spawn("exec -a claude sleep 300")
        r = bash('ta_detect_agent "$1"', str(proc.pid))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(), ["claude", str(proc.pid)])

    def test_detects_agent_child_of_shell(self):
        proc = self.spawn('bash -c "exec -a codex sleep 300"; sleep 300')
        r = bash('ta_detect_agent "$1"', str(proc.pid))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split()[0], "codex")

    def test_ignores_plain_shell(self):
        proc = self.spawn("exec -a vim sleep 300")
        r = bash('ta_detect_agent "$1"', str(proc.pid))
        self.assertEqual(r.returncode, 1)

    def test_missing_pid_fails(self):
        r = bash('ta_detect_agent 999999')
        self.assertEqual(r.returncode, 1)

    def test_proc_cpu_is_a_number(self):
        r = bash('ta_proc_cpu "$1"', str(os.getpid()))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertGreaterEqual(int(r.stdout.strip()), 0)

    def test_ps_time_in_hundredths(self):
        # procps (Linux) and BSD (macOS) ps "time" formats.
        for time_, ticks in (("00:00:01", "100"), ("0:01.23", "123"),
                             ("12:34.5", "75450"), ("1-02:03:04", "9378400")):
            with self.subTest(time=time_):
                r = bash('ta_ps_time_ticks "$1"', time_)
                self.assertEqual(r.stdout.strip(), ticks, r.stderr)
        self.assertEqual(bash('ta_ps_time_ticks bogus').returncode, 1)


class CliSmokeTests(unittest.TestCase):
    def run_cli(self, *args):
        e = {**os.environ, "TMUX_AGENT_QUIET": "1",
             "TMUX_AGENT_STATE_DIR": tempfile.mkdtemp(prefix="ta-test-")}
        return subprocess.run([str(CLI), *args], capture_output=True, text=True, env=e)

    def test_strip_exits_clean(self):
        r = self.run_cli("strip")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_status_exits_clean(self):
        r = self.run_cli("status")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_usage_on_unknown_command(self):
        r = self.run_cli("bogus")
        self.assertEqual(r.returncode, 1)

    def test_report_requires_args(self):
        r = self.run_cli("report")
        self.assertEqual(r.returncode, 1)

    MIXED_AGENTS = (("idle", "claude"), ("ready", "codex"),
                    ("working", "opencode"), ("blocked", "claude"))

    def run_strip(self, agents, max_=None, **env):
        # agents: (state, agent) per pane, written as cached state files;
        # max_ is passed as --max.
        with tempfile.TemporaryDirectory(prefix="ta-strip-") as directory:
            state_dir = Path(directory)
            (state_dir / "stamp").write_text(str(int(time.time())) + "\n")
            for pane, (state, agent) in enumerate(agents, 1):
                (state_dir / f"state-%{pane}").write_text(
                    f"{state}|{agent}|0|0|0|example:1.1|@1|/project\n"
                )
            r = subprocess.run(
                [str(CLI), "strip", *(["--max", max_] if max_ else [])],
                capture_output=True, text=True,
                env={**os.environ, "TMUX_AGENT_STATE_DIR": directory, **env},
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout

    def test_strip_collapses_to_counts_past_the_limit(self):
        out = self.run_strip([("blocked", "claude")] + [("working", "codex")] * 3
                             + [("idle", "opencode")] * 2)
        self.assertEqual(
            out,
            "#[fg=red]◆ #[fg=white]claude#[default]"
            "  #[fg=yellow]●#[fg=white]3#[default]"
            "  #[fg=green]○#[fg=gray]2#[default]",
        )

    def test_strip_counts_blocked_when_many_are_blocked(self):
        out = self.run_strip([("blocked", "claude")] * 3 + [("working", "codex")] * 2)
        self.assertEqual(
            out,
            "#[fg=red]◆#[fg=white]3#[default]  #[fg=yellow]●#[fg=white]2#[default]",
        )

    def test_strip_max_is_configurable(self):
        out = self.run_strip(self.MIXED_AGENTS, TMUX_AGENT_STRIP_MAX="2")
        self.assertEqual(
            out,
            "#[fg=red]◆ #[fg=white]claude#[default]"
            "  #[fg=yellow]●#[fg=white]1#[default]"
            "  #[fg=blue]◉#[fg=white]1#[default]"
            "  #[fg=green]○#[fg=gray]1#[default]",
        )

    def test_strip_max_argument_wins_over_environment(self):
        named = self.run_strip(self.MIXED_AGENTS)
        self.assertEqual(
            self.run_strip(self.MIXED_AGENTS, TMUX_AGENT_STRIP_MAX="2", max_="4"), named)
        # Anything but a number falls back to the default of 4.
        self.assertEqual(self.run_strip(self.MIXED_AGENTS, max_="$(id)"), named)

    def test_place_strip_replaces_an_earlier_strip(self):
        with tempfile.TemporaryDirectory(prefix="ta-place-") as directory:
            log = fake_tmux(directory, {
                "show -gv status-format[0]":
                    "L#[nolist align=absolute-centre]#(~/.local/bin/tmux-agent strip)"
                    "#[nolist align=right R",
            })
            r = subprocess.run(
                [str(CLI), "place-strip", "--max", "6"], capture_output=True, text=True,
                env={**os.environ, "PATH": f"{directory}:{os.environ['PATH']}"},
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(
                f"set -g status-format[0] L#[nolist align=absolute-centre]"
                f"#('{CLI}' strip --max 6)#[nolist align=right R",
                log.read_text().splitlines(),
            )

    def test_strip_shows_every_pane_with_distinct_state_colors(self):
        self.assertEqual(
            self.run_strip(self.MIXED_AGENTS),
            "#[fg=red]\u25c6 #[fg=white]claude#[default]"
            "  #[fg=yellow]\u25cf #[fg=white]opencode#[default]"
            "  #[fg=blue]\u25c9 #[fg=white]codex#[default]"
            "  #[fg=green]\u25cb #[fg=gray]claude#[default]",
        )



    def test_pick_rows_align_columns_and_hide_the_pane_id(self):
        with tempfile.TemporaryDirectory(prefix="ta-pick-") as directory:
            state_dir = Path(directory)
            now = int(time.time())
            (state_dir / "stamp").write_text(f"{now}\n")
            for pane, agent, path in (
                (1, "claude", "/src/app"),
                (2, "opencode", "/src/a-longer-project"),
                (3, "codex", "/x"),
            ):
                (state_dir / f"state-%{pane}").write_text(
                    f"idle|{agent}|0|{now}|{now}|work:{pane}.1|@1|{path}|12345\n"
                )
            fake = state_dir / "bin"
            fake.mkdir()
            (fake / "fzf").write_text(f'#!/bin/sh\ncat >"{state_dir}/rows"\nexit 130\n')
            (fake / "fzf").chmod(0o755)
            r = subprocess.run(
                [str(CLI), "pick"], capture_output=True, text=True,
                env={**os.environ, "TMUX_AGENT_STATE_DIR": directory,
                     "PATH": f"{fake}:{os.environ['PATH']}"},
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            raw = (state_dir / "rows").read_text().splitlines()
            # Only the state mark is coloured; the text after it stays plain.
            for line in raw:
                self.assertTrue(line.startswith("\x1b[32m○\x1b[0m"), repr(line))
                self.assertNotIn("\x1b[", line.partition("\x1b[0m")[2])
            rows = [re.sub(r"\x1b\[[0-9;]*m", "", line).split("\t")
                    for line in raw]
            self.assertEqual([pane for _, pane in rows], ["%1", "%2", "%3"])
            shown = [text for text, _ in rows]
            for column in ("work:", "0m"):
                self.assertEqual(len({line.index(column) for line in shown}), 1, shown)
            self.assertNotIn("12345", "".join(shown))

    def test_pick_switches_client_to_pane_in_another_session(self):
        with tempfile.TemporaryDirectory(prefix="ta-goto-") as directory:
            state_dir = Path(directory)
            now = int(time.time())
            (state_dir / "stamp").write_text(f"{now}\n")
            (state_dir / "state-%5").write_text(
                f"ready|codex|0|{now}|{now}|other:1.1|@4|/src/app|12345\n"
            )
            fake = state_dir / "bin"
            fake.mkdir()
            (fake / "fzf").write_text("#!/bin/sh\nhead -n 1\n")
            (fake / "tmux").write_text(f'#!/bin/sh\necho "$*" >>"{state_dir}/tmux.log"\n')
            for tool in ("fzf", "tmux"):
                (fake / tool).chmod(0o755)
            r = subprocess.run(
                [str(CLI), "pick"], capture_output=True, text=True,
                env={**os.environ, "TMUX_AGENT_STATE_DIR": directory,
                     "PATH": f"{fake}:{os.environ['PATH']}"},
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("switch-client -t %5",
                          (state_dir / "tmux.log").read_text().splitlines())
            self.assertTrue((state_dir / "seen-%5").exists())

    def run_attach(self, states, *args):
        # states: pane id -> state, written as cached state files. Returns
        # the result and the panes switch-client was sent to.
        with tempfile.TemporaryDirectory(prefix="ta-attach-") as directory:
            state_dir = Path(directory)
            now = int(time.time())
            (state_dir / "stamp").write_text(f"{now}\n")
            for pane, state in states.items():
                (state_dir / f"state-{pane}").write_text(
                    f"{state}|claude|0|{now}|{now}|main:1.1|@1|/project|1\n"
                )
            fake = state_dir / "bin"
            fake.mkdir()
            log = fake_tmux(fake)
            r = subprocess.run(
                [str(CLI), "attach", *args], capture_output=True, text=True,
                env={**os.environ, "TMUX_AGENT_STATE_DIR": directory,
                     "PATH": f"{fake}:{os.environ['PATH']}"},
            )
            calls = log.read_text().splitlines() if log.exists() else []
            return r, [c.split()[-1] for c in calls if c.startswith("switch-client")]

    URGENT_MIX = {"%1": "working", "%2": "ready", "%3": "blocked",
                  "%4": "idle", "%5": "blocked"}

    def test_attach_urgent_prefers_blocked_and_skips_working_and_idle(self):
        r, jumps = self.run_attach(self.URGENT_MIX, "--urgent")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(jumps, ["%3"])

    def test_attach_urgent_cycles_past_the_current_pane(self):
        for current, expected in (("%3", "%5"), ("%5", "%2"), ("%2", "%3"),
                                  ("%1", "%3")):
            with self.subTest(current=current):
                _, jumps = self.run_attach(self.URGENT_MIX, "--urgent",
                                           "--from", current)
                self.assertEqual(jumps, [expected])

    def test_attach_urgent_without_candidates_fails(self):
        r, jumps = self.run_attach({"%1": "working", "%2": "idle"}, "--urgent")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no agent needs you", r.stderr)
        self.assertEqual(jumps, [])

    def test_attach_next_still_takes_any_state(self):
        _, jumps = self.run_attach({"%1": "idle", "%2": "working"}, "--next")
        self.assertEqual(jumps, ["%2"])


def wait_for(predicate, timeout=5.0):
    """Poll predicate until it is true or timeout seconds pass."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class NotifyClickTests(unittest.TestCase):
    """A blocked transition notifies; clicking the notification focuses the
    pane through `focus`, which switches a client and raises its window."""

    PANE_FMT = ("#{pane_id}|#{pane_pid}|#{window_id}|"
                "#{session_name}:#{window_index}.#{pane_index}|#{pane_current_path}")
    CLIENT_FMT = "#{client_activity}|#{client_name}|#{client_pid}|#{session_name}"

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ta-notify-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.fake = self.dir / "bin"
        self.fake.mkdir()
        self.raised = self.dir / "raised"
        raise_cmd = self.fake / "raise-window"
        raise_cmd.write_text(f'#!/bin/sh\nprintf "%s" "$1" >{shlex.quote(str(self.raised))}\n')
        raise_cmd.chmod(0o755)
        self.log = fake_tmux(self.fake, {
            f"list-panes -a -F {self.PANE_FMT}": "%7|999999|@1|main:1.1|/src/app\n",
            "display-message -p -t %7 #{session_name}": "main",
            # The client on the pane's session wins over a more active one.
            f"list-clients -F {self.CLIENT_FMT}":
                "300|/dev/pts/3|4242|other\n200|/dev/pts/4|4343|main\n",
            "show-option -gqv @tmux-agent-raise-command": str(raise_cmd),
        })
        self.notified = self.dir / "notify-send.log"
        (self.fake / "notify-send").write_text(
            "#!/bin/sh\n"
            'for a; do [ "$a" = --wait ] && [ -n "${NO_WAIT:-}" ] && exit 1; done\n'
            f'printf "%s\\n" "$*" >>{shlex.quote(str(self.notified))}\n'
            'sleep "${HOLD:-0}"\n'
            'printf "%s" "${ACTION:-}"\n'
        )
        (self.fake / "notify-send").chmod(0o755)

    def refresh(self, **env):
        # Pane %7 was working; its agent hook just reported blocked.
        now = int(time.time())
        (self.dir / "stamp").write_text(f"{now}\n")
        (self.dir / "state-%7").write_text(
            f"working|custom|0|{now - 10}|{now}|main:1.1|@1|/src/app|1\n")
        (self.dir / "report-%7").write_text(f"blocked|{now}\n")
        e = {k: v for k, v in os.environ.items() if k != "TMUX_AGENT_QUIET"}
        e.update({"TMUX_AGENT_STATE_DIR": str(self.dir),
                  "PATH": f"{self.fake}:{os.environ['PATH']}", **env})
        return subprocess.run([str(CLI), "refresh", "1"], capture_output=True,
                              text=True, env=e, timeout=10)

    def switches(self):
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        return [c for c in calls if c.startswith("switch-client")]

    def test_click_switches_client_and_raises_its_window(self):
        r = self.refresh(ACTION="default")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(wait_for(lambda: self.raised.exists()), self.log.read_text())
        self.assertEqual(self.switches(), ["switch-client -c /dev/pts/4 -t %7"])
        self.assertEqual(self.raised.read_text(), "4343")
        self.assertTrue((self.dir / "seen-%7").exists())
        sent = self.notified.read_text()
        self.assertIn("-u critical -A default=Show --wait custom needs input", sent)

    def test_dismissed_notification_does_nothing(self):
        self.refresh(ACTION="")
        self.assertTrue(wait_for(lambda: self.notified.exists()))
        time.sleep(0.3)
        self.assertEqual(self.switches(), [])
        self.assertFalse(self.raised.exists())

    def test_waiting_notification_releases_the_scan_lock(self):
        self.refresh(HOLD="5")
        self.assertTrue(wait_for(lambda: self.notified.exists()))
        started = time.monotonic()
        r = self.refresh(HOLD="5")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(time.monotonic() - started, 3)

    def test_notify_send_without_wait_still_notifies(self):
        self.refresh(NO_WAIT="1")
        self.assertTrue(wait_for(lambda: self.notified.exists()))
        self.assertEqual(self.notified.read_text().splitlines(),
                         ["-a tmux-agent -u critical custom needs input app · main:1.1"])

    def test_focus_on_a_closed_pane_does_nothing(self):
        r = subprocess.run(
            [str(CLI), "focus", "--pane", "%99"], capture_output=True, text=True,
            env={**os.environ, "TMUX_AGENT_STATE_DIR": str(self.dir),
                 "PATH": f"{self.fake}:{os.environ['PATH']}"},
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.switches(), [])
        self.assertFalse(self.raised.exists())


class RaiseTests(unittest.TestCase):
    """lib/raise detects the window manager from the client's environment and
    focuses the window owned by the client's nearest ancestor. A sleep child
    stands in for the tmux client; this test process is its ancestor and the
    fake WM tools report it as the window owner."""

    RAISE = ROOT / "lib" / "raise"

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ta-raise-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.fake = self.dir / "bin"
        self.fake.mkdir()
        self.log = self.dir / "wm.log"
        self.owner = os.getpid()

    def tool(self, name, body=""):
        path = self.fake / name
        path.write_text(f'#!/bin/sh\nprintf "%s\\n" "{name} $*" >>{shlex.quote(str(self.log))}\n{body}')
        path.chmod(0o755)

    def raise_(self, client_env, env=None, option=""):
        fake_tmux(self.fake, {"show-option -gqv @tmux-agent-raise-command": option})
        # No awesome running unless a test says otherwise.
        if not (self.fake / "pgrep").exists():
            self.tool("pgrep", "exit 1\n")
        path = f"{self.fake}:{os.environ['PATH']}"
        client = subprocess.Popen(["sleep", "30"], env={"PATH": path, **client_env})
        self.addCleanup(client.wait)
        self.addCleanup(client.kill)
        base = {k: v for k, v in os.environ.items()
                if k not in ("HYPRLAND_INSTANCE_SIGNATURE", "SWAYSOCK", "DISPLAY",
                             "WAYLAND_DISPLAY", "TMUX")}
        r = subprocess.run([str(self.RAISE), str(client.pid)], capture_output=True,
                           text=True, env={**base, "PATH": path, **(env or client_env)})
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        # pgrep only probes for awesome; the rest are window-manager calls.
        return client.pid, [c for c in calls if not c.startswith("pgrep ")]

    def hyprctl(self):
        self.tool("hyprctl", f"""case "$1" in
clients) printf '[{{"pid": 1, "address": "0xdead"}}, {{"pid": {self.owner}, "address": "0xbeef"}}]' ;;
eval) printf '%s' "${{EVAL_REPLY:-}}" ;;
esac
""")

    def test_hyprland_lua_config_uses_eval(self):
        self.hyprctl()
        env = {"HYPRLAND_INSTANCE_SIGNATURE": "sig", "EVAL_REPLY": "ok"}
        _, calls = self.raise_(env)
        self.assertEqual(calls, [
            "hyprctl clients -j",
            'hyprctl eval hl.dispatch(hl.dsp.focus({ window = "address:0xbeef" }))',
        ])

    def test_hyprland_legacy_config_falls_back_to_dispatch(self):
        self.hyprctl()
        env = {"HYPRLAND_INSTANCE_SIGNATURE": "sig", "EVAL_REPLY": "unknown request"}
        _, calls = self.raise_(env)
        self.assertEqual(calls[-1], "hyprctl dispatch focuswindow address:0xbeef")

    def test_sway_focuses_the_nearest_ancestor_with_a_window(self):
        self.tool("swaymsg", f'[ "$1" = "[pid={self.owner}] focus" ] || exit 2\n')
        client, calls = self.raise_({"SWAYSOCK": "/run/sway.sock"})
        self.assertEqual(calls, [f"swaymsg [pid={client}] focus",
                                 f"swaymsg [pid={self.owner}] focus"])

    def test_awesome_jumps_to_the_client(self):
        self.tool("pgrep")
        script = self.dir / "awesome.lua"
        self.tool("awesome-client", f'printf "%s" "$1" >{shlex.quote(str(script))}\n')
        client, _ = self.raise_({"DISPLAY": ":9"})
        lua = script.read_text()
        self.assertIn(f"ipairs({{ {client},{self.owner},", lua)
        self.assertIn("c:jump_to(false)", lua)

    def test_x11_activates_the_window_with_xdotool(self):
        self.tool("xdotool", f'[ "$1 $3" = "search {self.owner}" ] && echo 777\nexit 0\n')
        _, calls = self.raise_({"DISPLAY": ":9"})
        self.assertEqual(calls[-1], "xdotool windowactivate 777")

    @unittest.skipUnless(Path("/proc/self/environ").exists(), "needs procfs")
    def test_client_environment_wins_over_a_stale_server_one(self):
        # tmux started under Hyprland, client attached from an X11 session.
        self.hyprctl()
        self.tool("xdotool", f'[ "$1 $3" = "search {self.owner}" ] && echo 777\nexit 0\n')
        _, calls = self.raise_({"DISPLAY": ":9"},
                               env={"HYPRLAND_INSTANCE_SIGNATURE": "stale"})
        self.assertEqual(calls[-1], "xdotool windowactivate 777")
        self.assertFalse(any(c.startswith("hyprctl") for c in calls), calls)

    def test_off_raises_nothing(self):
        self.hyprctl()
        _, calls = self.raise_({"HYPRLAND_INSTANCE_SIGNATURE": "sig"}, option="off")
        self.assertEqual(calls, [])

    def test_custom_command_gets_the_client_pid(self):
        self.tool("my-raise")
        client, calls = self.raise_({"HYPRLAND_INSTANCE_SIGNATURE": "sig"},
                                    option=str(self.fake / "my-raise"))
        self.assertEqual(calls, [f"my-raise {client}"])

    def test_unknown_desktop_does_nothing(self):
        self.hyprctl()
        _, calls = self.raise_({"WAYLAND_DISPLAY": "wayland-1"})
        self.assertEqual(calls, [])

class HookTests(unittest.TestCase):
    def state_of(self, event):
        r = bash('ta_hook_state "$1"', event)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_claude_and_codex_events_map_to_states(self):
        for event, state in (
            ("SessionStart", "idle"),
            ("UserPromptSubmit", "working"),
            ("PreToolUse", "working"),
            ("PostToolUse", "working"),
            ("PermissionRequest", "blocked"),
            ("Notification", "blocked"),
            ("Stop", "ready"),
            ("SessionEnd", "end"),
            ("SubagentStop", ""),
        ):
            with self.subTest(event=event):
                self.assertEqual(self.state_of(event), state)

    def test_opencode_events_map_to_states(self):
        for event, state in (
            ("session.execution.started", "working"),
            ("permission.asked", "blocked"),
            ("permission.replied", "working"),
            ("form.created", "blocked"),
            ("form.replied", "working"),
            ("session.execution.succeeded", "ready"),
            ("session.execution.failed", "ready"),
            ("session.execution.interrupted", "ready"),
            ("session.text.delta", ""),
        ):
            with self.subTest(event=event):
                self.assertEqual(self.state_of(event), state)

    def run_hook(self, state_dir, event, pane="%999"):
        # A pane id no tmux server knows: the background rescan drops it,
        # so only the synchronous report write is observed here.
        return subprocess.run(
            [str(CLI), "hook", "claude", event], input='{"session_id": "x"}',
            capture_output=True, text=True,
            env={**os.environ, "TMUX_AGENT_QUIET": "1",
                 "TMUX_AGENT_STATE_DIR": state_dir, "TMUX_PANE": pane},
        )

    def test_hook_reports_state_silently(self):
        with tempfile.TemporaryDirectory(prefix="ta-hook-") as directory:
            r = self.run_hook(directory, "Stop")
            self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
            report = (Path(directory) / "report-%999").read_text()
            self.assertEqual(report.split("|")[0], "ready")

    def test_hook_session_end_clears_report(self):
        with tempfile.TemporaryDirectory(prefix="ta-hook-") as directory:
            (Path(directory) / "report-%999").write_text("working|1\n")
            r = self.run_hook(directory, "SessionEnd")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse((Path(directory) / "report-%999").exists())

    def test_hook_outside_tmux_is_a_noop(self):
        with tempfile.TemporaryDirectory(prefix="ta-hook-") as directory:
            r = self.run_hook(directory, "Stop", pane="")
            self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
            self.assertEqual(list(Path(directory).iterdir()), [])


class InstallHooksTests(unittest.TestCase):
    def test_merge_is_idempotent_and_keeps_foreign_hooks(self):
        with tempfile.TemporaryDirectory(prefix="ta-home-") as home:
            settings = Path(home) / ".claude" / "settings.json"
            settings.parent.mkdir()
            settings.write_text(
                '{"model": "x", "hooks": {"Stop": [{"hooks": '
                '[{"type": "command", "command": "other"}]}]}}'
            )
            env = {**os.environ, "HOME": home}
            for _ in range(2):
                r = subprocess.run([str(CLI), "install-hooks", "claude"],
                                   capture_output=True, text=True, env=env)
                self.assertEqual(r.returncode, 0, r.stderr)
            data = json.loads(settings.read_text())
            self.assertEqual(data["model"], "x")
            stop = [h["command"] for e in data["hooks"]["Stop"] for h in e["hooks"]]
            self.assertEqual(len(stop), 2)
            self.assertEqual(stop[0], "other")
            self.assertTrue(stop[1].endswith("tmux-agent hook claude Stop"))
            notification = data["hooks"]["Notification"]
            self.assertEqual(notification[0]["matcher"],
                             "permission_prompt|elicitation_dialog")

    def test_remove_drops_only_tmux_agent_hooks(self):
        with tempfile.TemporaryDirectory(prefix="ta-home-") as home:
            settings = Path(home) / ".claude" / "settings.json"
            settings.parent.mkdir()
            settings.write_text('{"hooks": {"Stop": [{"hooks": '
                                '[{"type": "command", "command": "other"}]}]}}')
            env = {**os.environ, "HOME": home}
            for args in (["claude", "opencode"], ["--remove", "claude", "opencode"]):
                r = subprocess.run([str(CLI), "install-hooks", *args],
                                   capture_output=True, text=True, env=env)
                self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(settings.read_text())["hooks"],
                             {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]})
            self.assertFalse((Path(home) / ".config/opencode/plugins/tmux-agent").exists())

    def test_opencode_plugin_directory_is_linked(self):
        with tempfile.TemporaryDirectory(prefix="ta-home-") as home:
            r = subprocess.run([str(CLI), "install-hooks", "opencode"],
                               capture_output=True, text=True,
                               env={**os.environ, "HOME": home})
            self.assertEqual(r.returncode, 0, r.stderr)
            plugin = Path(home) / ".config/opencode/plugins/tmux-agent"
            self.assertEqual(plugin.resolve(), ROOT / "opencode")
            self.assertTrue((plugin / "index.js").is_file())
            self.assertTrue((plugin / "tui.js").is_file())


class EntryPointTests(unittest.TestCase):
    """tmux-agentic.tmux against a fake tmux: which commands it issues."""

    def run_entry(self, replies=None):
        with tempfile.TemporaryDirectory(prefix="ta-entry-") as directory:
            log = fake_tmux(directory, replies)
            r = subprocess.run(
                [str(ENTRY)], capture_output=True, text=True,
                env={**os.environ, "PATH": f"{directory}:{os.environ['PATH']}"},
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            return log.read_text().splitlines()

    def test_defaults_bind_picker_hook_focus_and_interpolate(self):
        calls = self.run_entry({
            "show-option -gqv status-right": "#[fg=blue]#{agent_status} host",
        })
        self.assertIn(f"bind-key a display-popup -E -w 80% -h 80% '{CLI}' pick", calls)
        self.assertIn(f"bind-key A run-shell '{CLI}' attach --urgent --from '#{{pane_id}}'"
                      " 2>/dev/null || tmux display-message 'no agent needs you'", calls)
        self.assertIn(f"set-hook -ga pane-focus-in run-shell \"'{CLI}' seen #{{pane_id}}\"",
                      calls)
        self.assertIn(f"set-option -gq status-right #[fg=blue]#('{CLI}' strip) host", calls)
        self.assertFalse([c for c in calls if c.startswith("set-option -gq status-left")])

    def test_options_change_key_size_limit_and_position(self):
        calls = self.run_entry({
            "show-option -gqv @tmux-agent-key": "off",
            "show-option -gqv @tmux-agent-urgent-key": "off",
            "show-option -gqv @tmux-agent-strip-position": "centre",
            "show-option -gqv @tmux-agent-strip-max": "6",
            "show -gv status-format[0]": "L#[nolist align=right R",
        })
        self.assertFalse([c for c in calls if c.startswith("bind-key")])
        self.assertIn(f"set -g status-format[0] L#[nolist align=absolute-centre]"
                      f"#('{CLI}' strip --max 6)#[nolist align=right R", calls)

    def test_focus_hook_is_not_added_twice(self):
        calls = self.run_entry({
            "show-hooks -g pane-focus-in":
                f"pane-focus-in[0] run-shell \"'{CLI}' seen #{{pane_id}}\"",
        })
        self.assertFalse([c for c in calls if c.startswith("set-hook")])


@unittest.skipUnless(shutil.which("node"), "node not installed")
class OpencodePluginTests(unittest.TestCase):
    """Drive the TUI plugin with a mocked OpenCode API and event stream."""

    SCRIPT = r"""
const { default: def } = await import(process.env.PLUGIN_URL);
const roots = { root: "root", child: "root" };
const events = [
  ["server.connected", {}],
  ["session.execution.started", { sessionID: "root" }],
  ["session.execution.started", { sessionID: "child" }],
  ["permission.asked", { sessionID: "child" }],
  ["permission.replied", { sessionID: "child" }],
  ["session.execution.succeeded", { sessionID: "child" }],
  ["session.execution.started", { sessionID: "elsewhere" }],
  ["form.created", { form: { sessionID: "root" } }],
  ["session.text.delta", { sessionID: "root" }],
  ["session.execution.interrupted", { sessionID: "root", reason: "shutdown" }],
  ["session.execution.succeeded", { sessionID: "root" }],
].map(([type, data]) => ({ type, data }));
const dispose = def.setup({
  data: { session: { root: (id) => roots[id] ?? id } },
  ui: { router: { current: () => ({ type: "session", sessionID: "root" }) },
        tabs: { list: () => [] } },
  client: { event: { subscribe: async function* ({ signal }) {
    yield* events;
    await new Promise((resolve) => signal.addEventListener("abort", resolve));
  } } },
});
await new Promise((resolve) => setTimeout(resolve, 500));
dispose();
"""

    def test_forwards_this_panes_root_session_and_prompts(self):
        plugin = ROOT / "opencode" / "tui.js"
        with tempfile.TemporaryDirectory(prefix="ta-oc-") as directory:
            log = Path(directory) / "calls"
            bin_ = Path(directory) / "tmux-agent"
            # The first hook is slow, so hooks run concurrently would log
            # out of order.
            bin_.write_text('#!/bin/sh\ncase "$*" in *started) sleep 0.2 ;; esac\n'
                            f'echo "$*" >>"{log}"\n')
            bin_.chmod(0o755)
            r = subprocess.run(
                ["node", "--input-type=module", "-e", self.SCRIPT],
                capture_output=True, text=True, timeout=30,
                env={**os.environ, "PLUGIN_URL": plugin.as_uri(),
                     "TMUX_PANE": "%1", "TMUX_AGENT_BIN": str(bin_)},
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            deadline = time.time() + 5
            want = [
                "hook opencode session.execution.started",
                "hook opencode permission.asked",
                "hook opencode permission.replied",
                "hook opencode form.created",
                "hook opencode session.execution.succeeded",
            ]
            calls = []
            while time.time() < deadline:
                calls = log.read_text().splitlines() if log.exists() else []
                if len(calls) >= len(want):
                    break
                time.sleep(0.05)
            self.assertEqual(calls, want)


if __name__ == "__main__":
    unittest.main()

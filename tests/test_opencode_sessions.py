"""OpenCode TUI plugin: one pane, several root sessions, one aggregated state."""
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCRIPT = r"""
const { default: def } = await import(process.env.PLUGIN_URL);
const roots = { A: "A", B: "B", subA: "A" };
const events = JSON.parse(process.env.EVENTS).map(([type, data]) => ({ type, data }));
const dispose = def.setup({
  data: { session: { root: (id) => roots[id] ?? id } },
  ui: { router: { current: () => ({ type: "session", sessionID: "A" }) },
        tabs: { list: () => [{ sessionID: "B" }] } },
  client: { event: { subscribe: async function* ({ signal }) {
    yield* events;
    await new Promise((resolve) => signal.addEventListener("abort", resolve));
  } } },
});
await new Promise((resolve) => setTimeout(resolve, 400));
dispose();
"""

STARTED = "session.execution.started"
OK = "session.execution.succeeded"
FAILED = "session.execution.failed"
ASKED = "permission.asked"


def run_plugin(events):
    """Feed events through tui.js; return the events it reported, in order."""
    with tempfile.TemporaryDirectory(prefix="ta-ocs-") as directory:
        log = Path(directory) / "calls"
        bin_ = Path(directory) / "tmux-agent"
        bin_.write_text(f'#!/bin/sh\necho "$*" >>"{log}"\n')
        bin_.chmod(0o755)
        r = subprocess.run(
            ["node", "--input-type=module", "-e", SCRIPT],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "PLUGIN_URL": (ROOT / "opencode" / "tui.js").as_uri(),
                 "TMUX_PANE": "%1", "TMUX_AGENT_BIN": str(bin_),
                 "EVENTS": json.dumps(events)},
        )
        if r.returncode != 0:
            raise AssertionError(r.stderr)
        time.sleep(0.3)
        calls = log.read_text().splitlines() if log.exists() else []
    return [c.removeprefix("hook opencode ") for c in calls]


class OpencodeSessionTests(unittest.TestCase):
    def test_other_session_finishing_keeps_pane_working(self):
        got = run_plugin([[STARTED, {"sessionID": "A"}], [STARTED, {"sessionID": "B"}],
                          [OK, {"sessionID": "B"}]])
        self.assertEqual(got, [STARTED])

    def test_permission_blocks_then_reply_resumes_then_ready(self):
        got = run_plugin([
            [STARTED, {"sessionID": "A"}],
            [ASKED, {"sessionID": "A", "id": "p1"}],
            ["permission.replied", {"sessionID": "A", "id": "p1"}],
            [OK, {"sessionID": "A"}],
        ])
        self.assertEqual(got, [STARTED, ASKED, STARTED, OK])

    def test_failed_reports_error(self):
        got = run_plugin([[STARTED, {"sessionID": "A"}], [FAILED, {"sessionID": "A"}]])
        self.assertEqual(got, [STARTED, FAILED])

    def test_error_outranks_working_in_other_session(self):
        got = run_plugin([[STARTED, {"sessionID": "A"}], [STARTED, {"sessionID": "B"}],
                          [FAILED, {"sessionID": "B"}], [OK, {"sessionID": "B"}]])
        self.assertEqual(got, [STARTED, FAILED, STARTED])

    def test_two_pending_requests_one_replied_stays_blocked(self):
        got = run_plugin([
            [STARTED, {"sessionID": "A"}],
            [ASKED, {"sessionID": "A", "id": "p1"}],
            [ASKED, {"sessionID": "A", "id": "p2"}],
            ["permission.replied", {"sessionID": "A", "id": "p1"}],
        ])
        self.assertEqual(got, [STARTED, ASKED])

    def test_reply_in_other_session_does_not_clear_prompt(self):
        got = run_plugin([
            [STARTED, {"sessionID": "A"}], [STARTED, {"sessionID": "B"}],
            [ASKED, {"sessionID": "A", "id": "p1"}],
            ["permission.replied", {"sessionID": "B", "id": "p2"}],
        ])
        self.assertEqual(got, [STARTED, ASKED])

    def test_subagent_execution_ignored_but_prompts_count_for_root(self):
        got = run_plugin([
            [STARTED, {"sessionID": "A"}],
            [OK, {"sessionID": "subA"}],
            [ASKED, {"sessionID": "subA", "id": "p1"}],
            ["permission.replied", {"sessionID": "subA", "id": "p1"}],
        ])
        self.assertEqual(got, [STARTED, ASKED, STARTED])

    def test_form_prompts_use_form_id(self):
        got = run_plugin([
            [STARTED, {"sessionID": "A"}],
            ["form.created", {"form": {"id": "f1", "sessionID": "A"}}],
            ["form.cancelled", {"form": {"id": "f1", "sessionID": "A"}}],
        ])
        self.assertEqual(got, [STARTED, ASKED, STARTED])

    def test_duplicate_state_not_rereported(self):
        got = run_plugin([[STARTED, {"sessionID": "A"}], [STARTED, {"sessionID": "A"}],
                          [STARTED, {"sessionID": "B"}]])
        self.assertEqual(got, [STARTED])

    def test_deleted_session_stops_blocking(self):
        got = run_plugin([
            [STARTED, {"sessionID": "A"}],
            [ASKED, {"sessionID": "A", "id": "p1"}],
            ["session.deleted", {"sessionID": "A"}],
        ])
        self.assertEqual(got, [STARTED, ASKED, OK])


if __name__ == "__main__":
    unittest.main()

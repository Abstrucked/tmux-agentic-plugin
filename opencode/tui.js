// tmux-agent OpenCode TUI plugin: forwards session lifecycle events to
// `tmux-agent hook opencode <event>` so the tmux status bar and desktop
// notifications follow the agent exactly instead of guessing from the pane.
//
// OpenCode 2 runs one shared server for every TUI, so this has to be a TUI
// plugin: only the TUI process lives in the tmux pane (TMUX_PANE). The
// directory is linked to ~/.config/opencode/plugins/tmux-agent by
// `tmux-agent install-hooks`.

import { spawn } from "node:child_process";
import { existsSync, realpathSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// Follow the plugins/ link back into the plugin checkout: its CLI sits at
// ../bin/tmux-agent. Fall back to tmux-agent on PATH.
function bundledBin() {
  try {
    const bin = join(dirname(realpathSync(fileURLToPath(import.meta.url))), "..", "bin", "tmux-agent");
    return existsSync(bin) ? bin : undefined;
  } catch {
    return undefined;
  }
}

const BIN = process.env.TMUX_AGENT_BIN || bundledBin() || "tmux-agent";

// Server bus events forwarded as-is; tmux-agent maps them to states.
// Execution events only count for root sessions: a subagent finishing does
// not mean the pane is done. Prompts for the user count from any session.
const EXECUTION = new Set([
  "session.execution.started",
  "session.execution.succeeded",
  "session.execution.failed",
  "session.execution.interrupted",
]);
const PROMPTS = new Set([
  "permission.asked",
  "permission.replied",
  "form.created",
  "form.replied",
  "form.cancelled",
]);

// Hooks run one at a time, in event order. Each one overwrites the pane's
// reported state, so hooks racing each other could leave a stale state
// behind, e.g. blocked after the permission prompt was already answered.
// The agent never waits on them: the hook rescans panes in the background.
const queue = [];
let running = false;

function report(event) {
  queue.push(event);
  if (!running) {
    next();
  }
}

function next() {
  const event = queue.shift();
  running = event !== undefined;
  if (!running) {
    return;
  }
  let done = false;
  const advance = () => {
    if (!done) {
      done = true;
      next();
    }
  };
  try {
    const child = spawn(BIN, ["hook", "opencode", event], { stdio: "ignore", detached: true });
    child.once("exit", advance);
    // tmux-agent missing or not executable: stay out of the agent's way.
    child.once("error", advance);
    child.unref();
  } catch {
    advance();
  }
}

function sessionOf(event) {
  const data = event?.data ?? {};
  return data.sessionID ?? data.form?.sessionID;
}

// A pane can show several root sessions (tabs), but it has one state. Each
// session keeps its own state and pending prompts; the pane reports the most
// urgent one, so B finishing never hides that A is still working or blocked.
const URGENCY = ["ready", "working", "error", "blocked"];
// Pane state -> the event tmux-agent already maps to it.
const REPORT = {
  blocked: "permission.asked",
  error: "session.execution.failed",
  working: "session.execution.started",
  ready: "session.execution.succeeded",
};
const STATE_OF = {
  "session.execution.started": "working",
  "session.execution.succeeded": "ready",
  "session.execution.interrupted": "ready",
  "session.execution.failed": "error",
};

// Prompts without an id share one slot: an unkeyed reply answers an unkeyed ask.
function requestOf(data) {
  return data?.requestID ?? data?.id ?? data?.permission?.id ?? data?.form?.id ?? "-";
}

export default {
  id: "tmux-agent",
  setup(api) {
    if (!process.env.TMUX_PANE) {
      return;
    }

    // The server streams every TUI's sessions. Keep the ones this pane has
    // shown (routed or tabbed), so they still count after navigating away.
    const owned = new Set();
    const root = (id) => api.data?.session?.root?.(id) ?? id;
    const mine = (id) => {
      const r = root(id);
      if (owned.has(r)) {
        return true;
      }
      const route = api.ui?.router?.current?.();
      const shown =
        (route?.type === "session" && root(route.sessionID) === r) ||
        (api.ui?.tabs?.list?.() ?? []).some((tab) => tab.sessionID === r);
      if (shown) {
        owned.add(r);
      }
      return shown;
    };

    const sessions = new Map(); // root session -> { state, pending: Set<request> }
    let paneState;
    const entry = (id) => {
      if (!sessions.has(id)) {
        sessions.set(id, { state: "ready", pending: new Set() });
      }
      return sessions.get(id);
    };
    const publish = () => {
      let state = "ready";
      for (const s of sessions.values()) {
        const own = s.pending.size > 0 ? "blocked" : s.state;
        if (URGENCY.indexOf(own) > URGENCY.indexOf(state)) {
          state = own;
        }
      }
      if (state !== paneState) {
        paneState = state;
        report(REPORT[state]);
      }
    };

    const handle = (event) => {
      const type = event?.type;
      // A deleted session can no longer run or ask; stop counting it.
      if (type === "session.deleted") {
        const gone = event.data?.sessionID ?? event.data?.info?.id;
        if (gone !== undefined && sessions.delete(gone)) {
          publish();
        }
        return;
      }
      const id = sessionOf(event);
      if (id === undefined || !(EXECUTION.has(type) || PROMPTS.has(type)) || !mine(id)) {
        return;
      }
      const top = root(id);
      if (EXECUTION.has(type) && top !== id) {
        return;
      }
      if (type === "session.execution.interrupted" && event.data?.reason === "shutdown") {
        return;
      }
      const s = entry(top);
      if (EXECUTION.has(type)) {
        s.state = STATE_OF[type];
        // A finished run cannot still be waiting on the user.
        if (s.state !== "working") {
          s.pending.clear();
        }
      } else if (type === "permission.asked" || type === "form.created") {
        s.pending.add(requestOf(event.data));
      } else {
        s.pending.delete(requestOf(event.data));
      }
      publish();
    };

    const abort = new AbortController();
    (async () => {
      // Reconnect with backoff: the shared server may restart under us.
      for (let delay = 500; !abort.signal.aborted; delay = Math.min(delay * 2, 10_000)) {
        try {
          for await (const event of api.client.event.subscribe({ signal: abort.signal })) {
            delay = 500;
            handle(event);
          }
        } catch {
          // Stream dropped; retry below unless disposed.
        }
        if (!abort.signal.aborted) {
          await new Promise((resolve) => setTimeout(resolve, delay));
        }
      }
    })();

    return () => abort.abort();
  },
};

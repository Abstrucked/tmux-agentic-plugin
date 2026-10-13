"""Remote discovery (lib/remote.sh): backoff for failing hosts, tailnet peers
that honour ~/.ssh/config, and tailnet discovery being opt-in."""

import getpass
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tmux_agent import ROOT, RemoteTestSupport, fake_tmux  # noqa: E402

REMOTE = ROOT / "lib" / "remote.sh"
ME = getpass.getuser()


class Base(RemoteTestSupport, unittest.TestCase):
    def sh(self, script, **env):
        """Run script under bash with lib/remote.sh sourced."""
        fake_tmux(self.fake)
        prelude = (f'set -uo pipefail; TA_STATE_DIR="{self.state}"; SELF=/bin/true; '
                   f'. "{REMOTE}"; ')
        return subprocess.run(["bash", "-c", prelude + script], capture_output=True,
                              text=True, env={**self.env, **env}, timeout=20, stdin=subprocess.DEVNULL)

    def meta(self, key, text):
        (self.state / f"remote-{key}.meta").write_text(text)

    def due(self, key="k"):
        r = self.sh(f'ta_remote_due "{key}"')
        return r.returncode == 0

    def tailnet_peer(self, *peers):
        """A tailscale stand-in listing online linux peers (name, ip)."""
        import json
        import shlex
        status = {"Self": {"DNSName": "laptop.tn.ts.net."},
                  "Peer": {f"n{i}": {"DNSName": f"{n}.tn.ts.net.", "OS": "linux",
                                     "Online": True, "TailscaleIPs": [ip]}
                           for i, (n, ip) in enumerate(peers)}}
        data = self.fake / "tailscale.json"
        data.write_text(json.dumps(status))
        script = self.fake / "tailscale"
        script.write_text("#!/bin/sh\n"
                          f'echo run >>{shlex.quote(str(self.fake / "tailscale.log"))}\n'
                          f"cat {shlex.quote(str(data))}\n")
        script.chmod(0o755)


class BackoffTests(Base):
    def test_consecutive_failures_back_off_up_to_thirty_minutes(self):
        now = int(time.time())
        # interval 10: 6 * 10 * 2^(n-1), capped at 1800
        for failures, wait in [(1, 60), (2, 120), (3, 240), (4, 480), (5, 960),
                               (6, 1800), (7, 1800), (40, 1800), (100000, 1800)]:
            with self.subTest(failures=failures):
                self.meta("k", f"err|{now - wait + 5}|{failures}\n")
                self.assertFalse(self.due(), "too early")
                self.meta("k", f"err|{now - wait}|{failures}\n")
                self.assertTrue(self.due(), "due")

    def test_old_two_field_meta_still_works(self):
        now = int(time.time())
        self.meta("k", f"err|{now - 61}\n")
        self.assertTrue(self.due())
        self.meta("k", f"err|{now - 30}\n")
        self.assertFalse(self.due())
        self.meta("k", f"noplugin|{now - 100}\n")
        self.assertFalse(self.due())
        self.meta("k", f"noplugin|{now - 301}\n")
        self.assertTrue(self.due())
        self.meta("k", f"err|{now - 61}|junk\n")
        self.assertTrue(self.due())

    def test_fresh_reads_two_and_three_field_meta(self):
        now = int(time.time())
        for text in (f"ok|{now}\n", f"ok|{now}|0\n"):
            self.meta("k", text)
            self.assertEqual(self.sh('ta_remote_fresh k').returncode, 0, text)
        self.meta("k", f"err|{now}|2\n")
        self.assertNotEqual(self.sh('ta_remote_fresh k').returncode, 0)
        self.meta("k", f"ok|{now - 61}|0\n")
        self.assertNotEqual(self.sh('ta_remote_fresh k').returncode, 0)

    def test_agents_of_a_fresh_host_with_either_meta_format(self):
        now = int(time.time())
        (self.state / "remotes").write_text(f"{self.KEY}|devbox||devbox|\n")
        (self.state / f"remote-{self.KEY}").write_text("blocked|claude|%3|main:1.2|@2|40|/src/api\n")
        for text in (f"ok|{now}\n", f"ok|{now}|0\n"):
            self.meta(self.KEY, text)
            r = self.sh("ta_remote_agents")
            self.assertEqual(r.stdout, f"blocked|claude|devbox|main:1.2|40|{self.KEY}/%3|/src/api\n", text)

    def test_failures_count_up_and_a_success_resets_them(self):
        self.answer("devbox", "", rc=255)
        meta = self.state / f"remote-{self.KEY}.meta"
        self.fetch()
        self.assertEqual(meta.read_text().split("|")[0::2], ["err", "1\n"])
        for expected in ("2", "3"):
            st, epoch, n = meta.read_text().strip().split("|")
            meta.write_text(f"{st}|{int(epoch) - 100000}|{n}\n")
            self.fetch()
            self.assertEqual(meta.read_text().strip().split("|")[0::2], ["err", expected])
        # An old two-field err meta counts as one failure.
        meta.write_text(f"err|{self.now - 100000}\n")
        self.fetch()
        self.assertEqual(meta.read_text().strip().split("|")[0::2], ["err", "2"])
        meta.write_text(f"err|{self.now - 100000}|5\n")
        (self.fake / "answer-devbox.rc").unlink()
        self.fetch()
        self.assertRegex(meta.read_text(), r"^ok\|\d+\|0\n$")

    def test_noplugin_does_not_count_as_a_failure(self):
        self.answer("devbox", "", rc=127)
        self.fetch()
        self.assertRegex((self.state / f"remote-{self.KEY}.meta").read_text(),
                         r"^noplugin\|\d+\|0\n$")

    def test_remotes_and_doctor_print_with_the_new_meta(self):
        now = int(time.time())
        self.answer("devbox")
        self.fetch()
        self.meta(self.KEY, f"err|{now - 180}|3\n")
        r = self.cli("remotes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), [
            f"devbox\t{self.KEY}\tlisted\terr 3m ago\t0 agents (3 failures)"])
        self.meta(self.KEY, f"err|{now - 180}|1\n")
        self.assertIn("\terr 3m ago\t0 agents", self.cli("remotes").stdout.rstrip("\n"))
        self.assertNotIn("failures", self.cli("remotes").stdout)
        self.meta(self.KEY, f"ok|{now - 180}|0\n")
        self.assertIn("\tok 3m ago\t", self.cli("remotes").stdout)
        self.meta(self.KEY, f"err|{now}|2\n")
        d = self.cli("doctor")
        self.assertIn("devbox (err)", d.stdout)
        self.meta(self.KEY, f"ok|{now}|0\n")
        self.assertRegex(self.cli("doctor").stdout, r"remotes\s.*1 host\(s\)")
        self.assertNotIn("devbox (", self.cli("doctor").stdout)


class TailnetTests(Base):
    def hosts(self, **env):
        r = self.sh("ta_remote_hosts", TMUX_AGENT_REMOTES="",
                    TMUX_AGENT_REMOTE_TAILSCALE="on", **env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.splitlines()

    def test_a_peer_without_ssh_config_is_dialled_by_its_address(self):
        self.tailnet_peer(("gpu", "100.64.0.7"))
        (self.fake / "G-gpu").write_text(f"hostname gpu\nuser {ME}\nport 22\n")
        self.assertEqual(self.hosts(), [f"{ME}@gpu:22|gpu||gpu||100.64.0.7"])
        (self.state / "remotes").write_text(f"{ME}@gpu:22|gpu||gpu||100.64.0.7\n")
        r = self.sh('ta_remote_target "$K"; echo "$TA_REMOTE_DEST|${TA_REMOTE_ARGV[*]}"',
                    K=f"{ME}@gpu:22")
        self.assertEqual(r.stdout.strip(), "gpu|ssh -o ConnectTimeout=3 -o HostName=100.64.0.7")

    def test_a_peer_with_a_config_keeps_its_user_port_and_name(self):
        self.tailnet_peer(("gpu", "100.64.0.7"), ("big", "100.64.0.8"), ("mixed", "100.64.0.9"))
        (self.fake / "G-gpu").write_text("hostname gpu.corp.example\nuser deploy\nport 2222\n")
        (self.fake / "G-big").write_text("hostname BIG\nuser root\nport 22\n")
        (self.fake / "G-mixed").write_text("hostname mixed\nuser ann\nport 2200\n")
        self.assertEqual(self.hosts(), [
            "deploy@gpu:2222|gpu||gpu||",
            "root@big:22|big||big||100.64.0.8",       # same name, any case
            "ann@mixed:2200|mixed||mixed||100.64.0.9",
        ])
        (self.state / "remotes").write_text("deploy@gpu:2222|gpu||gpu||\n")
        r = self.sh('ta_remote_target "$K"; echo "${TA_REMOTE_ARGV[*]}"', K="deploy@gpu:2222")
        self.assertEqual(r.stdout.strip(), "ssh -o ConnectTimeout=3")

    def test_an_unreadable_ssh_config_falls_back_to_the_local_user(self):
        self.tailnet_peer(("gpu", "100.64.0.7"))
        (self.fake / "G-gpu").write_text("")
        self.assertEqual(self.hosts(), [f"{ME}@gpu:22|gpu||gpu||100.64.0.7"])

    def test_peer_host_keys_are_still_pinned(self):
        (self.state / "tailscale-known-hosts").write_text("100.64.0.7,gpu ssh-ed25519 AAAA\n")
        (self.state / "remotes").write_text(f"{ME}@gpu:22|gpu||gpu||100.64.0.7\n"
                                           f"{ME}@other:22|other||other||100.64.0.8\n")
        known = self.state / "tailscale-known-hosts"
        r = self.sh('ta_remote_target "$K"; echo "${TA_REMOTE_ARGV[*]}"', K=f"{ME}@gpu:22")
        self.assertEqual(r.stdout.strip(), "ssh -o ConnectTimeout=3 -o HostName=100.64.0.7 "
                         f"-o UserKnownHostsFile={known} -o GlobalKnownHostsFile=/dev/null")
        r = self.sh('ta_remote_target "$K"; echo "${TA_REMOTE_ARGV[*]}"', K=f"{ME}@other:22")
        self.assertNotIn("UserKnownHostsFile", r.stdout)

    def test_five_field_lines_parse_as_before(self):
        sock = "/tmp/x/master-me@gpu.example:22"
        (self.state / "remotes").write_text(
            f"me@gpu.example:22|gpu.example|{sock}|me@gpu.example|22\n"
            "me@devbox.example:22|devbox||devbox|\n")
        r = self.sh('ta_remote_target "$K"; echo "$TA_REMOTE_DEST|$TA_REMOTE_NAME|${TA_REMOTE_ARGV[*]}"',
                    K="me@gpu.example:22")
        self.assertEqual(r.stdout.strip(), "me@gpu.example|gpu.example|ssh -o ConnectTimeout=3 "
                         f"-S {sock} -o ControlMaster=no -p 22")
        r = self.sh('ta_remote_target "$K"; echo "$TA_REMOTE_DEST|$TA_REMOTE_NAME|${TA_REMOTE_ARGV[*]}"',
                    K="me@devbox.example:22")
        self.assertEqual(r.stdout.strip(), "devbox|devbox|ssh -o ConnectTimeout=3")

    def test_the_other_readers_cope_with_six_fields(self):
        (self.state / "remotes").write_text(f"{self.KEY}|devbox||devbox||100.64.0.7\n")
        self.answer("devbox")
        self.meta(self.KEY, f"ok|{int(time.time())}|0\n")
        r = self.cli("remotes")
        self.assertEqual(r.stdout.splitlines()[0].split("\t")[:3], ["devbox", self.KEY, "listed"])
        self.assertRegex(self.cli("doctor").stdout, r"remotes\s.*1 host\(s\)")

    def test_tailnet_discovery_is_off_by_default(self):
        self.tailnet_peer(("gpu", "100.64.0.7"))
        env = {k: v for k, v in self.env.items() if k != "TMUX_AGENT_REMOTE_TAILSCALE"}
        self.env = env
        r = self.sh("ta_tailscale_peers; ta_remote_hosts", TMUX_AGENT_REMOTES="")
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertFalse((self.fake / "tailscale.log").exists())
        self.assertNotEqual(self.sh("ta_remote_enabled", TMUX_AGENT_REMOTES="",
                                    TMUX_AGENT_REMOTE_DISCOVER="off").returncode, 0)
        r = self.sh("ta_tailscale_peers", TMUX_AGENT_REMOTE_TAILSCALE="on")
        self.assertEqual(r.stdout, "gpu\t100.64.0.7\n")
        self.assertTrue((self.fake / "tailscale.log").exists())


if __name__ == "__main__":
    unittest.main()

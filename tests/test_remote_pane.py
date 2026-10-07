"""Control protocol, terminal rendering, and isolated real tmux mirrors."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import pty
import re
import select
import shlex
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import time
import unittest
import fcntl

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('remote_pane', ROOT / 'lib/remote-pane.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class ProtocolTests(unittest.TestCase):
    def test_responses_match_ids_and_preserve_percent_text_and_escape_sequences(self):
        sent, responses, events = [], [], []
        protocol = bridge.Protocol(sent.append, events.append)
        protocol.command('capture-pane', lambda ok, rows: responses.append((ok, rows)))
        for line in [b'%begin 1 1 0', b'%end 1 1 0', b'%output %2 escaped',
                     b'%begin 2 2 1', b'%end 99 99 1', b'%output %1 screen text',
                     b'\\033[31mred\\033[0m', b'%end 2 2 1']:
            protocol.line(line)
        self.assertEqual(sent, [b'capture-pane\n'])
        self.assertEqual(events, [b'%output %2 escaped'])
        self.assertEqual(responses, [(True, [b'%end 99 99 1', b'%output %1 screen text',
                                            b'\\033[31mred\\033[0m'])])

    def test_control_errors_are_not_displayed_as_screen_output(self):
        protocol = bridge.Protocol(lambda _: None, lambda _: None)
        protocol.command('send-keys')
        protocol.line(b'%begin 1 2 1')
        protocol.line(b'cannot find pane')
        with self.assertRaises(bridge.Closed):
            protocol.line(b'%error 1 2 1')

    def test_matching_end_marker_inside_a_captured_screen_is_literal_text(self):
        frames = []
        protocol = bridge.Protocol(lambda _: None, lambda _: None)
        protocol.command('capture-pane', lambda ok, rows: frames.append(rows), rows=2)
        for line in (b'%begin 1 2 1', b'%end 1 2 1', b'ordinary row', b'%end 1 2 1'):
            protocol.line(line)
        self.assertEqual(frames, [[b'%end 1 2 1', b'ordinary row']])

    def test_terminal_controls_are_filtered_except_generated_sgr(self):
        self.assertEqual(bridge.clean_line(b'\\033[31mred\\033[0m'), '\x1b[31mred\x1b[0m')
        clean = bridge.clean_line(b'\\033]52;c;clipboard\\007text\\033[2J')
        self.assertNotIn('\x1b', clean)
        self.assertNotIn('\x07', clean)
        self.assertEqual(bridge.unescape(b'\\134033'), b'\\033')

    def test_crop_respects_wide_and_combining_cells_and_keeps_colours(self):
        self.assertEqual(bridge.crop('A界e\u0301Z', 1, 3), '界e\u0301')
        self.assertEqual(bridge.crop('界Z', 1, 2), ' Z')
        self.assertEqual(bridge.crop('\x1b[31mAB\x1b[0m', 1, 1), '\x1b[31mB\x1b[0m')
        self.assertEqual(bridge.crop('👩\u200d💻X', 0, 2), '👩\u200d💻')


class InputTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        def callback(kind):
            return lambda *args: self.events.append((kind, args))
        self.parser = bridge.Input(*(callback(kind) for kind in ('key', 'raw', 'paste', 'pan', 'mouse')))

    def test_partial_keys_unicode_and_literal_ctrl_bracket(self):
        self.parser.feed(b'\x1b[')
        self.assertEqual(self.events, [])
        self.parser.feed(b'A' + '界'.encode() + b'\x1d\x1d')
        self.assertEqual(self.events[0], ('key', ('Up',)))
        raw = b''.join(args[0] for kind, args in self.events if kind == 'raw')
        self.assertEqual(raw, '界'.encode() + b'\x1d')

    def test_paste_markers_split_across_reads_and_embedded_keys_stay_literal(self):
        self.parser.feed(b'\x1b[20')
        self.parser.feed(b'0~hello\n\x1b[A\x1d\x1b[20')
        self.parser.feed(b'1~')
        self.assertEqual(self.events, [('paste', (b'hello\n\x1b[A\x1d',))])

    def test_pan_mouse_and_discarded_partial_input(self):
        self.parser.feed(b'\x1d\x1b[6~\x1d0\x1b[<0;8;4M')
        self.assertEqual(self.events, [('pan', ('NPage',)), ('pan', ('follow',)),
                                       ('mouse', (0, 8, 4, False))])
        self.parser.feed(b'\x1b[')
        self.parser.discard()
        self.parser.feed(b'A')
        self.assertEqual(self.events[-1], ('raw', (b'A',)))

    def test_escape_key_timeout(self):
        self.parser.feed(b'\x1b')
        self.assertEqual(self.events, [])
        self.parser.feed(expired=True)
        self.assertEqual(self.events, [('raw', (b'\x1b',))])


@unittest.skipUnless(shutil.which('tmux'), 'needs tmux')
class RealMirrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ta-mirror-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.sock = self.path / 'sock'
        self.env = {key: val for key, val in os.environ.items() if key not in ('TMUX', 'TMUX_PANE')}
        self.env.update(TERM='tmux-256color', TMUX_AGENT_QUIET='1', TMUX_AGENT_REMOTES='',
                        TMUX_AGENT_REMOTE_DISCOVER='off', TMUX_AGENT_REMOTE_TAILSCALE='off')
        self.tmux = ['tmux', '-S', str(self.sock)]
        self.run_tmux('-f', '/dev/null', 'new-session', '-d', '-s', 'source', '-x', '80', '-y', '24', 'cat')
        self.addCleanup(lambda: subprocess.run([*self.tmux, 'kill-server'], env=self.env, capture_output=True))
        self.pane = self.run_tmux('display-message', '-p', '-t', 'source:0', '#{pane_id}').strip()
        self.run_tmux('split-window', '-d', '-t', self.pane, 'cat')
        self.before = self.layout()
        self.state = self.path / 'state'
        self.state.mkdir(mode=0o700)
        self.status = self.path / 'mirror.status'
        self.launcher = self.path / 'launcher'
        self.launcher.write_text('#!/bin/sh\nexport TMUX=%s\nexport TMUX_AGENT_STATE_DIR=%s\nexec %s mirror-connect --pane %s\n' %
                                 (shlex.quote(str(self.sock) + ',1,0'), shlex.quote(str(self.state)),
                                  shlex.quote(str(ROOT / 'bin/tmux-agent')), shlex.quote(self.pane)))
        self.launcher.chmod(0o700)
        self.master, self.slave = pty.openpty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, self.slave)
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack('HHHH', 10, 50, 0, 0))
        self.saved = termios.tcgetattr(self.slave)
        self.process = subprocess.Popen(['python3', str(ROOT / 'lib/remote-pane.py'), '--pane', self.pane,
                                         '--identity', 'test/' + self.pane, '--launcher', str(self.launcher),
                                         '--status-file', str(self.status)], stdin=self.slave, stdout=self.slave,
                                        stderr=subprocess.PIPE, env=self.env)
        self.addCleanup(self.stop)
        self.output = bytearray()

    def stop(self):
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            self.process.wait(timeout=5)
        self.process.stderr.close()

    def run_tmux(self, *args):
        result = subprocess.run([*self.tmux, *args], env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def layout(self):
        return self.run_tmux('list-panes', '-a', '-F',
                             '#{pane_id}|#{pane_pid}|#{pane_width}|#{pane_height}|#{pane_active}|#{window_layout}|#{session_id}|#{window_active}')

    def wait_for(self, predicate, timeout=5):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if select.select([self.master], [], [], 0.05)[0]:
                self.output.extend(os.read(self.master, 65536))
            if predicate():
                return
            if self.process.poll() is not None:
                self.fail('bridge exited: %s\n%s' % (self.process.stderr.read().decode(), self.output.decode(errors='replace')))
        self.fail('mirror did not reach expected state: %s' % self.output[-4000:].decode(errors='replace'))

    def connected(self):
        try:
            return json.loads(self.status.read_text())['state'] == 'connected'
        except (OSError, ValueError):
            return False

    def test_interactive_snapshot_preserves_layout_resize_and_close(self):
        self.wait_for(self.connected)
        text = 'MIRROR_HELLOé界'
        os.write(self.master, text.encode())
        self.wait_for(lambda: text.encode() in self.output)
        self.assertIn(text, self.run_tmux('capture-pane', '-p', '-t', self.pane))
        self.assertEqual(self.layout(), self.before)
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack('HHHH', 18, 90, 0, 0))
        self.process.send_signal(signal.SIGWINCH)
        time.sleep(0.2)
        self.assertEqual(self.layout(), self.before)
        self.stop()
        self.assertEqual(self.process.returncode, 0)
        self.assertEqual(termios.tcgetattr(self.slave), self.saved)
        self.assertEqual(self.layout(), self.before)

    def test_paste_uses_literal_input_and_removes_its_buffer(self):
        self.wait_for(self.connected)
        os.write(self.master, b'\x1b[200~PASTED;display-message unsafe\x1b[201~')
        self.wait_for(lambda: b'PASTED;display-message unsafe' in self.output)
        buffers = self.run_tmux('list-buffers', '-F', '#{buffer_name}')
        self.assertNotIn('tmux-agent-mirror', buffers)
        self.assertEqual(self.layout(), self.before)

    def test_closed_remote_pane_exits_and_restores_terminal(self):
        self.wait_for(self.connected)
        self.run_tmux('kill-pane', '-t', self.pane)
        self.process.wait(timeout=5)
        self.assertEqual(self.process.returncode, 1, self.process.stderr.read().decode())
        self.assertEqual(termios.tcgetattr(self.slave), self.saved)
        self.assertEqual(json.loads(self.status.read_text())['state'], 'closed')

    def test_reconnect_discards_input_typed_while_disconnected(self):
        self.wait_for(self.connected)
        children = subprocess.check_output(['ps', '-axo', 'pid=,ppid='], text=True)
        pid = next(int(line.split()[0]) for line in children.splitlines()
                   if line.split()[1] == str(self.process.pid))
        os.kill(pid, signal.SIGTERM)
        self.wait_for(lambda: json.loads(self.status.read_text())['state'] == 'disconnected')
        os.write(self.master, b'DROPPED_INPUT')
        self.wait_for(self.connected)
        os.write(self.master, b'AFTER_RECONNECT')
        self.wait_for(lambda: b'AFTER_RECONNECT' in self.output)
        self.assertNotIn('DROPPED_INPUT', self.run_tmux('capture-pane', '-p', '-t', self.pane))
        self.assertEqual(self.layout(), self.before)

    def test_alternate_screen_colours_unicode_and_mouse_input(self):
        program = ("import os,tty; tty.setraw(0); "
                   "os.write(1, '\\x1b[?1049h\\x1b[?1000h\\x1b[?1006h\\x1b[2J"
                   "\\x1b[1;1H\\x1b[31mALT_é界\\x1b[0m\\x1b[2;1H'.encode()); "
                   "\nwhile True:\n data=os.read(0,1024); os.write(1,b'\\x1b[2;1H'+data.hex().encode())")
        self.run_tmux('respawn-pane', '-k', '-t', self.pane, 'python3', '-u', '-c', program)
        self.before = self.layout()
        self.wait_for(lambda: 'ALT_é界'.encode() in self.output)
        self.assertIn(b'\x1b[31m', self.output)
        # Allow the one-second metadata refresh to observe mouse modes even
        # if the app started between the initial metadata and capture.
        until = time.monotonic() + 1.2
        self.wait_for(lambda: time.monotonic() >= until)
        event = b'\x1b[<0;2;2M'
        os.write(self.master, event)
        self.wait_for(lambda: event.hex().encode() in self.output)
        self.assertEqual(self.layout(), self.before)

    def test_real_local_session_creation_reuse_tags_and_independent_close(self):
        local_sock = self.path / 'local-sock'
        local_tmux = ['tmux', '-S', str(local_sock)]
        local_state = self.path / 'local-state'
        local_state.mkdir(mode=0o700)
        fake = self.path / 'bin'
        fake.mkdir()
        sid = self.run_tmux('display-message', '-p', '-t', self.pane, '#{session_id}').strip()
        (fake / 'ssh').write_text('#!/bin/sh\ncase "$*" in\n*mirror-connect*) printf "TMUX_AGENT_MIRROR_V1\\n"; exec tmux -S %s -C attach-session -f ignore-size -t %s ;;\n*) exit 0 ;;\nesac\n' %
                                  (shlex.quote(str(self.sock)), shlex.quote(sid)))
        (fake / 'ssh').chmod(0o700)
        env = {**self.env, 'TMUX_AGENT_STATE_DIR': str(local_state),
               'TMUX_AGENT_REMOTE_VIEW': 'mirror', 'PATH': str(fake) + ':' + self.env['PATH']}
        subprocess.run([*local_tmux, '-f', '/dev/null', 'new-session', '-d', '-s', 'work', 'cat'],
                       env=env, check=True, capture_output=True)
        self.addCleanup(lambda: subprocess.run([*local_tmux, 'kill-server'], env=env, capture_output=True))
        env['TMUX'] = str(local_sock) + ',1,0'
        key = 'me@remote:22'
        (local_state / 'remotes').write_text(key + '|remote||remote|\n')
        (local_state / ('remote-' + key)).write_text('blocked|claude|%s|source:0.0|@0|0|/tmp\n' % self.pane)
        (local_state / ('remote-' + key + '.meta')).write_text('ok|%d\n' % time.time())
        (local_state / 'stamp').write_text('%d\n' % time.time())
        for _ in range(2):
            result = subprocess.run([str(ROOT / 'bin/tmux-agent'), 'attach', '--next'], env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        rows = subprocess.check_output([*local_tmux, 'list-panes', '-s', '-t', 'remote-agents', '-F',
                                       '#{pane_id}|#{@tmux-agent-mirror}|#{@tmux-agent-remote}|#{@tmux-agent-remote-pane}'],
                                      env=env, text=True).splitlines()
        self.assertEqual(len(rows), 1)
        pane, marker, host, remote_pane = rows[0].split('|')
        self.assertEqual((marker, host, remote_pane), ('on', key, self.pane))
        status = local_state / ('mirror-' + pane + '.status')
        def ready():
            try:
                return json.loads(status.read_text())['state'] == 'connected'
            except (OSError, ValueError):
                return False
        self.wait_for(ready)
        subprocess.run([*local_tmux, 'kill-pane', '-t', pane], env=env, check=True, capture_output=True)
        self.assertEqual(self.layout(), self.before)


if __name__ == '__main__':
    unittest.main()

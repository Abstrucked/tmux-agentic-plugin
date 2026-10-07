#!/usr/bin/env python3
"""An interactive, fixed-size view of one remote tmux pane.

The server owns terminal emulation. We request its rendered screen rather
than replaying a partial application byte stream into a second emulator.
Only generated SGR attributes reach the local terminal; protocol and
application control sequences never do. Python 3.9+, standard library only.
"""

import argparse
from collections import deque
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import termios
import time
import tty
import unicodedata

HEADER = b"TMUX_AGENT_MIRROR_V1"
META = ("#{pane_width}|#{pane_height}|#{cursor_x}|#{cursor_y}|#{cursor_flag}|"
        "#{history_size}|#{session_id}|#{pane_dead}|#{pane_in_mode}|"
        "#{mouse_any_flag}|#{mouse_sgr_flag}|#{mouse_button_flag}|"
        "#{mouse_all_flag}|#{mouse_utf8_flag}")
BLOCK = re.compile(rb"^%(begin|end|error) ([0-9]+) ([0-9]+) ([0-9]+)$")
SGR = re.compile(r"\x1b\[[0-9;:]*m")
OCTAL = re.compile(rb"\\([0-7]{3})")
MAX_BLOCK = 8 * 1024 * 1024
KEYS = {
    b"\x1b[A": "Up", b"\x1b[B": "Down", b"\x1b[C": "Right", b"\x1b[D": "Left",
    b"\x1bOA": "Up", b"\x1bOB": "Down", b"\x1bOC": "Right", b"\x1bOD": "Left",
    b"\x1b[H": "Home", b"\x1b[F": "End", b"\x1bOH": "Home", b"\x1bOF": "End",
    b"\x1b[1~": "Home", b"\x1b[4~": "End", b"\x1b[2~": "IC", b"\x1b[3~": "DC",
    b"\x1b[5~": "PPage", b"\x1b[6~": "NPage", b"\x1b[Z": "BTab",
    b"\x1bOP": "F1", b"\x1bOQ": "F2", b"\x1bOR": "F3", b"\x1bOS": "F4",
    b"\x1b[15~": "F5", b"\x1b[17~": "F6", b"\x1b[18~": "F7",
    b"\x1b[19~": "F8", b"\x1b[20~": "F9", b"\x1b[21~": "F10",
    b"\x1b[23~": "F11", b"\x1b[24~": "F12",
}


class Closed(Exception):
    pass


class Unsupported(Exception):
    pass


def quote(value):
    """A tmux parser argument, never a shell argument."""
    data = value.encode("utf-8") if isinstance(value, str) else value
    return '"' + ''.join("\\%03o" % byte for byte in data) + '"'


def unescape(data):
    return OCTAL.sub(lambda match: bytes([int(match[1], 8)]), data)


def clean_line(data):
    """Keep screen text and tmux-generated colour attributes only."""
    text = unescape(data).decode("utf-8", "replace")
    result = []
    pos = 0
    while pos < len(text):
        match = SGR.match(text, pos)
        if match:
            result.append(match[0])
            pos = match.end()
        else:
            char = text[pos]
            if ord(char) >= 32 and not 127 <= ord(char) < 160:
                result.append(char)
            pos += 1
    return ''.join(result)


def crop(text, left, width):
    """Crop by display cells while retaining SGR, combining marks and ZWJ."""
    pieces = []
    cluster = ''
    cells = 0
    position = 0
    pos = 0
    join = False

    def flush():
        nonlocal cluster, cells, position
        if cluster and position >= left and position + cells <= left + width:
            pieces.append(cluster)
        elif cluster and cells and position < left + width and position + cells > left:
            pieces.append(' ' * (min(position + cells, left + width) - max(position, left)))
        position += cells
        cluster = ''
        cells = 0

    while pos < len(text):
        match = SGR.match(text, pos)
        if match:
            flush()
            pieces.append(match[0])
            pos = match.end()
            continue
        char = text[pos]
        pos += 1
        if char == '\u200d':
            cluster += char
            join = True
        elif unicodedata.combining(char) or char in ('\ufe0e', '\ufe0f'):
            cluster += char
            if char == '\ufe0f':
                cells = max(cells, 2)
        elif join:
            cluster += char
            cells = max(cells, 2 if unicodedata.east_asian_width(char) in 'WF' else 1)
            join = False
        else:
            flush()
            cluster = char
            cells = 2 if unicodedata.east_asian_width(char) in 'WF' else 1
    flush()
    return ''.join(pieces)


class Protocol:
    """Control-mode framing, preserving percent-prefixed captured text."""
    def __init__(self, send, output):
        self.send = send
        self.output = output
        self.pending = deque()
        self.block = None
        self.lines = []
        self.callback = None
        self.expected_rows = None
        self.size = 0

    def command(self, command, callback=None, rows=None):
        self.pending.append((callback, rows))
        self.send(command.encode('utf-8') + b'\n')

    def line(self, line):
        match = BLOCK.match(line)
        if self.block is not None:
            if match and match[1] in (b'end', b'error') and match.groups()[1:] == self.block and (
                    match[1] == b'error' or self.expected_rows is None or len(self.lines) == self.expected_rows):
                callback, lines = self.callback, self.lines
                self.block = None
                self.lines = []
                self.callback = None
                self.expected_rows = None
                if callback:
                    callback(match[1] == b'end', lines)
                elif match[1] == b'error':
                    raise Closed('remote input or control command failed')
            else:
                self.size += len(line)
                if self.size > MAX_BLOCK:
                    raise ConnectionError('remote screen response exceeded 8 MiB')
                self.lines.append(line)
            return
        if match and match[1] == b'begin':
            self.block = match.groups()[1:]
            self.size = 0
            self.callback, self.expected_rows = self.pending.popleft() if int(match[4]) & 1 and self.pending else (None, None)
            return
        if line.startswith(b'%output ') or line.startswith(b'%extended-output '):
            self.output(line)
        elif line.startswith(b'%exit'):
            raise ConnectionError('remote control connection ended')


class Input:
    """Incremental input parser; pasted escape sequences remain literal."""
    def __init__(self, key, raw, paste, pan, mouse):
        self.key, self.raw, self.paste, self.pan, self.mouse = key, raw, paste, pan, mouse
        self.buffer = b''
        self.pasting = False
        self.pasted = bytearray()
        self.chord = False
        self.since = 0.0

    def feed(self, data=b'', expired=False):
        self.buffer += data
        if data:
            self.since = time.monotonic()
        while self.buffer:
            if self.pasting:
                end = self.buffer.find(b'\x1b[201~')
                if end < 0:
                    keep = min(5, len(self.buffer))
                    self.pasted.extend(self.buffer[:-keep])
                    self.buffer = self.buffer[-keep:]
                    if len(self.pasted) > MAX_BLOCK:
                        raise ValueError('paste exceeds 8 MiB')
                    return
                self.pasted.extend(self.buffer[:end])
                self.paste(bytes(self.pasted))
                self.pasted.clear()
                self.pasting = False
                self.buffer = self.buffer[end + 6:]
                continue
            if self.buffer.startswith(b'\x1b[200~'):
                self.pasting = True
                self.buffer = self.buffer[6:]
                continue
            event = re.match(rb'^\x1b\[<([0-9]+);([0-9]+);([0-9]+)([Mm])', self.buffer)
            if event:
                self.mouse(int(event[1]), int(event[2]), int(event[3]), event[4] == b'm')
                self.buffer = self.buffer[event.end():]
                continue
            match = next((seq for seq in KEYS if self.buffer.startswith(seq)), None)
            if match:
                name = KEYS[match]
                self.buffer = self.buffer[len(match):]
                if self.chord:
                    self.pan(name)
                    self.chord = False
                else:
                    self.key(name)
                continue
            if not expired and (any(seq.startswith(self.buffer) for seq in KEYS) or
                                b'\x1b[200~'.startswith(self.buffer) or
                                self.buffer.startswith(b'\x1b[<') and len(self.buffer) < 64):
                return
            byte, self.buffer = self.buffer[:1], self.buffer[1:]
            if self.chord:
                self.chord = False
                if byte == b'\x1d':
                    self.raw(byte)
                elif byte == b'0':
                    self.pan('follow')
                else:
                    self.raw(b'\x1d' + byte)
            elif byte == b'\x1d':
                self.chord = True
            else:
                self.raw(byte)

    def discard(self):
        self.buffer = b''
        self.pasted.clear()
        self.pasting = self.chord = False


class Terminal:
    def __init__(self, fd=0):
        self.fd = fd
        self.saved = None

    def __enter__(self):
        self.saved = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        self.write('\x1b[?1049h\x1b[?7l\x1b[?2004h\x1b[2J')
        return self

    def __exit__(self, *_):
        try:
            self.write('\x1b[0m\x1b[?25h\x1b[?1000l\x1b[?1002l\x1b[?1003l'
                       '\x1b[?1006l\x1b[?2004l\x1b[?7h\x1b[?1049l')
        finally:
            if self.saved is not None:
                termios.tcsetattr(self.fd, termios.TCSANOW, self.saved)

    @staticmethod
    def write(text):
        sys.stdout.buffer.write(text.encode('utf-8', 'replace'))
        sys.stdout.buffer.flush()

    def dimensions(self):
        size = os.get_terminal_size(self.fd)
        return max(1, size.columns), max(1, size.lines)


class Mirror:
    def __init__(self, args, terminal):
        self.args = args
        self.terminal = terminal
        self.process = None
        self.protocol = None
        self.selector = selectors.DefaultSelector()
        self.selector.register(0, selectors.EVENT_READ, 'input')
        self.ready = False
        self.dirty = True
        self.stopping = False
        self.next_try = 0
        self.retry = 0
        self.last_frame = 0
        self.last_meta = 0
        self.started = 0
        self.busy = False
        self.x = self.y = 0
        self.follow = True
        self.meta = None
        self.generation = 0
        self.request_generation = 0
        self.final_message = ''
        self.rows = []
        self.origin = (0, 0)
        self.buffers = {'out': b'', 'err': b''}
        self.write_buffer = bytearray()
        self.raw_input = bytearray()
        self.buffer_name = 'tmux-agent-mirror-%s' % os.getpid()
        self.input = Input(self.key, self.raw, self.paste, self.pan, self.mouse)

    def status(self, state, reason=''):
        # No screen contents, input or full subprocess command is recorded.
        if self.args.status_file:
            temporary = None
            try:
                path = Path(self.args.status_file)
                fd, temporary = tempfile.mkstemp(prefix='.mirror-status-', dir=str(path.parent))
                with os.fdopen(fd, 'w') as stream:
                    json.dump({'state': state, 'reason': reason[:1024]}, stream)
                os.replace(temporary, path)
            except OSError:
                pass
            finally:
                if temporary is not None:
                    try:
                        os.unlink(temporary)
                    except FileNotFoundError:
                        pass
        if state != 'connected':
            width, height = self.terminal.dimensions()
            label = crop('%s%s' % (state, ': ' + reason if reason else ''), 0, width)
            self.terminal.write('\x1b[?25l\x1b[0m\x1b[%d;1H\x1b[2K%s' % (height, label))

    def connect(self):
        self.disconnect()
        command = [self.args.launcher, 'mirror-transport', self.args.identity]
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, start_new_session=True)
        for stream, kind in ((self.process.stdout, 'out'), (self.process.stderr, 'err')):
            os.set_blocking(stream.fileno(), False)
            self.selector.register(stream, selectors.EVENT_READ, kind)
        os.set_blocking(self.process.stdin.fileno(), False)
        self.protocol = Protocol(self.send, self.output)
        self.started = time.monotonic()
        self.ready = self.busy = False
        self.dirty = True
        self.buffers = {'out': b'', 'err': b''}
        self.last_meta = self.last_frame = 0
        self.status('connecting')

    def disconnect(self):
        self.ready = self.busy = False
        self.input.discard()
        self.raw_input.clear()
        self.write_buffer.clear()
        if self.process:
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                try:
                    self.selector.unregister(stream)
                except (KeyError, ValueError):
                    pass
            if self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                    self.process.wait(timeout=1)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    if self.process.poll() is None:
                        os.killpg(self.process.pid, signal.SIGKILL)
                        self.process.wait()
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                stream.close()
        self.process = None

    def send(self, data):
        if len(self.write_buffer) + len(data) > MAX_BLOCK:
            raise ConnectionError('remote connection is too slow; input discarded')
        self.write_buffer.extend(data)
        try:
            self.selector.register(self.process.stdin, selectors.EVENT_WRITE, 'write')
        except KeyError:
            pass

    def output(self, line):
        if re.match(rb'^%(?:extended-)?output ' + re.escape(self.args.pane.encode()) + rb' ', line):
            self.dirty = True
            self.generation += 1

    def raw(self, data):
        if self.ready and self.meta and not self.meta['mode']:
            self.raw_input.extend(data)

    def flush_input(self):
        while self.raw_input:
            data = bytes(self.raw_input[:1024])
            del self.raw_input[:1024]
            self.protocol.command('send-keys -t %s -H %s' %
                                  (self.args.pane, ' '.join('%02x' % byte for byte in data)))

    def key(self, name):
        if self.ready and self.meta and not self.meta['mode']:
            self.flush_input()
            self.protocol.command('send-keys -t %s %s' % (self.args.pane, name))

    def paste(self, data):
        if not self.ready or not self.meta or self.meta['mode']:
            return
        self.flush_input()
        # tmux inserts bracket markers only when the application requested
        # them, including on tmux 3.2 which exposes no paste-mode format.
        for start in range(0, len(data), 4096):
            self.protocol.command('set-buffer %s-b %s %s' %
                                  ('-a ' if start else '', self.buffer_name,
                                   quote(data[start:start + 4096])))
        if data:
            self.protocol.command('paste-buffer -dpr -b %s -t %s' %
                                  (self.buffer_name, self.args.pane))

    def pan(self, action):
        width, height = self.terminal.dimensions()
        if action == 'follow':
            self.follow = True
        else:
            self.follow = False
            self.x += {'Left': -8, 'Right': 8}.get(action, 0)
            self.y += {'Up': -1, 'Down': 1, 'PPage': -(height - 1), 'NPage': height - 1}.get(action, 0)
        self.dirty = True

    def mouse(self, button, x, y, release):
        if not self.ready or not self.meta or self.meta['mode']:
            return
        width, height = self.terminal.dimensions()
        if not 1 <= x <= width or not 1 <= y <= self.view_rows():
            return
        rx, ry = x + self.origin[0], y + self.origin[1]
        if not 1 <= rx <= self.meta['width'] or not 1 <= ry <= self.meta['height']:
            return
        if not self.meta['mouse']:
            if button & 64:
                self.pan('Up' if button & 1 == 0 else 'Down')
            return
        if self.meta['sgr']:
            event = ('\x1b[<%d;%d;%d%s' % (button, rx, ry, 'm' if release else 'M')).encode()
        else:
            button = 3 if release else button
            values = (button + 32, rx + 32, ry + 32)
            if self.meta['utf8mouse']:
                event = b'\x1b[M' + ''.join(chr(v) for v in values).encode()
            elif max(values) <= 255:
                event = b'\x1b[M' + bytes(values)
            else:
                return
        self.raw(event)

    def view_rows(self):
        width, height = self.terminal.dimensions()
        return max(1, height - int(bool(self.meta and
                                       (self.meta['width'] > width or self.meta['height'] > height or not self.follow or self.meta['mode']))))

    def request(self):
        self.busy = True
        self.request_generation = self.generation
        self.last_meta = time.monotonic()
        self.protocol.command('display-message -p -t %s %s' % (self.args.pane, quote(META)), self.metadata, rows=1)

    def metadata(self, ok, lines):
        if not ok or len(lines) != 1:
            raise Closed('remote pane closed')
        fields = lines[0].decode('ascii').split('|')
        if len(fields) != 14 or not re.fullmatch(r'\$[0-9]+', fields[6]):
            raise ConnectionError('invalid remote pane metadata')
        names = ('width', 'height', 'cx', 'cy', 'cursor', 'history', 'session', 'dead',
                 'mode', 'mouse', 'sgr', 'buttonmouse', 'allmouse', 'utf8mouse')
        values = [int(value or '0') if index != 6 else value for index, value in enumerate(fields)]
        self.meta = dict(zip(names, values))
        if self.meta['dead']:
            raise Closed('remote pane closed')
        if not 1 <= self.meta['width'] <= 10000 or not 1 <= self.meta['height'] <= 10000:
            raise ConnectionError('invalid remote pane dimensions')
        # A pane may be moved by its owner while viewed. Follow its session
        # without selecting any remote window or pane.
        if getattr(self, 'session', None) not in (None, fields[6]):
            self.protocol.command('switch-client -t %s' % fields[6])
        self.session = fields[6]
        width, _ = self.terminal.dimensions()
        rows = self.view_rows()
        if self.follow:
            self.x = max(0, min(self.x, self.meta['cx']))
            self.x = max(self.x, self.meta['cx'] - width + 1)
            self.y = max(0, min(self.y, self.meta['cy']))
            self.y = max(self.y, self.meta['cy'] - rows + 1)
        self.x = max(0, min(self.x, max(0, self.meta['width'] - width)))
        self.y = max(-self.meta['history'], min(self.y, max(0, self.meta['height'] - rows)))
        self.origin = (self.x, self.y)
        end = min(self.meta['height'] - 1, self.y + rows - 1)
        self.protocol.command('capture-pane -p -e -C -N -t %s -S %d -E %d' %
                              (self.args.pane, self.y, end), self.screen, rows=end - self.y + 1)

    def screen(self, ok, lines):
        self.busy = False
        if not ok:
            raise Closed('remote pane closed')
        self.rows = [clean_line(line) for line in lines]
        self.render()
        self.last_frame = time.monotonic()
        self.dirty = self.generation != self.request_generation
        self.retry = 0
        self.status('connected')

    def render(self):
        width, height = self.terminal.dimensions()
        rows = self.view_rows()
        out = ['\x1b[?25l\x1b[0m']
        for index in range(height):
            text = crop(self.rows[index], self.origin[0], width) if index < min(rows, len(self.rows)) else ''
            if index == height - 1 and rows < height:
                label = ('remote pane in tmux copy mode; input paused' if self.meta['mode'] else
                         'view %d,%d · Ctrl-] arrows/PageUp/PageDown · 0 follow' % self.origin)
                text = crop(label, 0, width)
            out.append('\x1b[%d;1H\x1b[0m\x1b[2K%s\x1b[0m' % (index + 1, text))
        cx, cy = self.meta['cx'] - self.origin[0], self.meta['cy'] - self.origin[1]
        if self.meta['cursor'] and 0 <= cx < width and 0 <= cy < rows:
            out.append('\x1b[%d;%dH\x1b[?25h' % (cy + 1, cx + 1))
        # SGR reporting lets us translate mouse coordinates before passing
        # them to either an SGR or an older mouse application.
        tracking = 1003 if self.meta['allmouse'] else 1002 if self.meta['buttonmouse'] else 1000
        out.append('\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006h\x1b[?%dh' % tracking)
        self.terminal.write(''.join(out))

    def read(self, kind, stream):
        data = os.read(stream.fileno(), 65536)
        if not data:
            if kind == 'err':
                self.selector.unregister(stream)
                return
            if not self.ready:
                rc = self.process.poll()
                if rc == 127:
                    raise Unsupported('remote mirror endpoint unavailable; update the remote plugin or use @tmux-agent-remote-view attach')
                reason = self.buffers['err'].decode('utf-8', 'replace').strip()
                raise ConnectionError(reason[-1024:] or 'SSH connection closed before handshake')
            reason = self.buffers['err'].decode('utf-8', 'replace').strip()
            raise ConnectionError(reason[-1024:] or 'SSH connection closed')
        if kind == 'err':
            # Strip all terminal control characters from SSH diagnostics.
            self.buffers['err'] = (self.buffers['err'] + data)[-4096:]
            return
        self.buffers['out'] += data
        if len(self.buffers['out']) > MAX_BLOCK:
            raise ConnectionError('remote protocol line exceeded 8 MiB')
        while b'\n' in self.buffers['out']:
            line, self.buffers['out'] = self.buffers['out'].split(b'\n', 1)
            if not self.ready:
                if line == b'TMUX_AGENT_MIRROR_CLOSED':
                    raise Closed('remote pane closed')
                if line == HEADER:
                    self.ready = True
                    self.session = None
                    self.meta = None
                    self.status('connected')
                else:
                    raise Unsupported('remote mirror endpoint unavailable; update the remote plugin or use @tmux-agent-remote-view attach')
            else:
                self.protocol.line(line)

    def run(self):
        try:
            while not self.stopping:
                now = time.monotonic()
                try:
                    if self.process is None and now >= self.next_try:
                        self.connect()
                    for event, _ in self.selector.select(0.05):
                        if event.data == 'input':
                            data = os.read(0, 65536)
                            if not data:
                                self.stopping = True
                            elif self.ready:
                                self.input.feed(data)
                        elif event.data == 'write':
                            written = os.write(event.fileobj.fileno(), self.write_buffer)
                            del self.write_buffer[:written]
                            if not self.write_buffer:
                                self.selector.unregister(event.fileobj)
                        else:
                            self.read(event.data, event.fileobj)
                    if self.ready:
                        if self.input.buffer and now - self.input.since > 0.04:
                            self.input.feed(expired=True)
                        self.flush_input()
                        if not self.busy and len(self.protocol.pending) < 8 and (
                                self.dirty and now - self.last_frame >= 0.1 or now - self.last_meta >= 1):
                            self.request()
                    if self.process and now - self.started > 20 and not self.ready:
                        raise ConnectionError('SSH handshake timed out')
                    if self.busy and now - self.last_meta > 20:
                        raise ConnectionError('remote screen query timed out')
                except (ConnectionError, OSError) as error:
                    reason = ''.join(char for char in str(error) if char.isprintable())
                    self.disconnect()
                    delay = (1, 2, 5, 10)[min(self.retry, 3)]
                    self.retry += 1
                    self.next_try = time.monotonic() + delay
                    self.status('disconnected', '%s; retry in %ss' % (reason, delay))
        except (Closed, Unsupported, ValueError) as error:
            self.final_message = str(error)
            self.status('closed' if isinstance(error, Closed) else 'error', self.final_message)
            return 1
        finally:
            self.disconnect()
            self.selector.close()
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pane', required=True)
    parser.add_argument('--launcher', required=True)
    parser.add_argument('--identity', required=True)
    parser.add_argument('--status-file')
    args = parser.parse_args()
    if not re.fullmatch(r'%[0-9]+', args.pane) or not args.identity.endswith('/' + args.pane):
        parser.error('invalid remote pane identity')
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error('mirror requires a terminal')
    with Terminal() as terminal:
        mirror = Mirror(args, terminal)
        def stop(_signum, _frame):
            mirror.stopping = True
        def resized(_signum, _frame):
            mirror.dirty = True
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sig, stop)
        signal.signal(signal.SIGWINCH, resized)
        result = mirror.run()
    if mirror.final_message:
        print(mirror.final_message)
    return result


if __name__ == '__main__':
    sys.exit(main())

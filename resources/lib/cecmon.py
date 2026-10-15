"""
Passive HDMI-CEC watcher (Linux CEC framework, /dev/cecN).

Opens the adapter in MONITOR mode next to Kodi's libCEC. It never transmits
and never claims a logical address. It reports whether this box is the
displayed source:

  True   Set Stream Path / Active Source naming our physical address
  False  Set Stream Path / Active Source / Routing Change naming any other
         address, or a Standby broadcast

Messages that say nothing about routing are ignored.
"""
import errno
import fcntl
import os
import select
import struct
import threading


def _ioc(direction, nr, size):
    return (direction << 30) | (size << 16) | (ord("a") << 8) | nr


MSG_FMT = "=QQIIII16s7Bx"                  # struct cec_msg (56 bytes)
MSG_SIZE = struct.calcsize(MSG_FMT)
EVENT_SIZE = 80                            # struct cec_event

CEC_ADAP_G_PHYS_ADDR = _ioc(2, 1, 2)
CEC_RECEIVE = _ioc(3, 6, MSG_SIZE)
CEC_DQEVENT = _ioc(3, 7, EVENT_SIZE)
CEC_S_MODE = _ioc(1, 9, 4)

CEC_MODE_MONITOR = 0xE << 4                # | CEC_MODE_NO_INITIATOR (0)
CEC_EVENT_STATE_CHANGE = 1

PA_INVALID = 0xFFFF

OP_ROUTING_CHANGE = 0x80
OP_ACTIVE_SOURCE = 0x82
OP_SET_STREAM_PATH = 0x86
OP_STANDBY = 0x36


def pa_str(pa):
    if pa == PA_INVALID:
        return "f.f.f.f"
    return f"{pa >> 12 & 0xF}.{pa >> 8 & 0xF}.{pa >> 4 & 0xF}.{pa & 0xF}"


def judge(op, data, my_pa):
    """Return (verdict, reason); verdict is True/False/None."""
    if my_pa == PA_INVALID:
        return None, ""
    if op in (OP_ACTIVE_SOURCE, OP_SET_STREAM_PATH) and len(data) >= 2:
        pa = data[0] << 8 | data[1]
        name = "Active Source" if op == OP_ACTIVE_SOURCE else "Set Stream Path"
        return pa == my_pa, f"{name} {pa_str(pa)}"
    if op == OP_ROUTING_CHANGE and len(data) >= 4:
        pa = data[2] << 8 | data[3]
        return pa == my_pa, f"Routing Change -> {pa_str(pa)}"
    if op == OP_STANDBY:
        return False, "Standby"
    return None, ""


class CecWatcher(threading.Thread):
    """Background thread; calls on_verdict(bool, reason) for every
    routing-relevant message (not only on changes)."""

    def __init__(self, dev, on_verdict, log):
        super().__init__(name="cecpower-cec", daemon=True)
        self.dev = dev
        self.on_verdict = on_verdict
        self.log = log
        self.my_pa = PA_INVALID
        self._halt = threading.Event()

    def stop(self):
        self._halt.set()

    def run(self):
        while not self._halt.is_set():
            try:
                self._watch()
            except Exception as e:                      # noqa: BLE001
                self.log(f"CEC watcher error on {self.dev}: {e}; retry in 10 s",
                         error=True)
                self._halt.wait(10)

    # -- internals ---------------------------------------------------------
    def _watch(self):
        fd = os.open(self.dev, os.O_RDWR | os.O_NONBLOCK)
        try:
            fcntl.ioctl(fd, CEC_S_MODE, struct.pack("=I", CEC_MODE_MONITOR))
            buf = bytearray(2)
            fcntl.ioctl(fd, CEC_ADAP_G_PHYS_ADDR, buf, True)
            pa = struct.unpack("=H", buf)[0]
            if pa != PA_INVALID:
                self.my_pa = pa
            self.log(f"monitoring {self.dev}, our physical address {pa_str(self.my_pa)}")

            poller = select.poll()
            poller.register(fd, select.POLLIN | select.POLLPRI)
            while not self._halt.is_set():
                for _, mask in poller.poll(500):
                    if mask & (select.POLLERR | select.POLLHUP | select.POLLNVAL):
                        raise OSError("adapter reported error/hangup")
                    if mask & select.POLLPRI:
                        self._drain_events(fd)
                    if mask & select.POLLIN:
                        self._drain_msgs(fd)
        finally:
            os.close(fd)

    def _drain_events(self, fd):
        while True:
            ev = bytearray(EVENT_SIZE)
            try:
                fcntl.ioctl(fd, CEC_DQEVENT, ev, True)
            except OSError as e:
                if e.errno == errno.EAGAIN:
                    return
                raise
            _, kind, _ = struct.unpack_from("=QII", ev)
            if kind == CEC_EVENT_STATE_CHANGE:
                pa, _ = struct.unpack_from("=HH", ev, 16)
                if pa != PA_INVALID and pa != self.my_pa:  # ignore HPD bounces
                    self.log(f"physical address now {pa_str(pa)}")
                    self.my_pa = pa

    def _drain_msgs(self, fd):
        while True:
            buf = bytearray(MSG_SIZE)
            try:
                fcntl.ioctl(fd, CEC_RECEIVE, buf, True)
            except OSError as e:
                if e.errno == errno.EAGAIN:
                    return
                raise
            f = struct.unpack(MSG_FMT, buf)
            length, msg = f[2], f[6][:f[2]]
            if length >= 2:
                self.handle(msg[1], msg[2:])

    def handle(self, op, data):
        verdict, reason = judge(op, data, self.my_pa)
        if verdict is not None:
            self.on_verdict(verdict, reason)

#!/usr/bin/env python3
"""
cec_probe.py - passive CEC bus monitor for LibreELEC (Pi 4, Linux CEC framework).

Opens /dev/cecN in monitor mode alongside Kodi/libCEC. It never transmits and
never claims a logical address, so Kodi's CEC handling is unaffected. Prints
every message it sees plus a running ACTIVE/INACTIVE verdict for this box.

Usage (SSH, as root):
    python3 /storage/cec_probe.py [--all] [--dev /dev/cec0] | tee /storage/cec.log

  --all   try MONITOR_ALL (whole bus, incl. messages between other devices);
          falls back to plain monitor if the hardware/driver can't do it.

While running, type a note and press Enter to drop a marker line into the log
(e.g. "to DVD", "system off"). Ctrl-C to stop.
"""
import argparse
import errno
import fcntl
import os
import select
import struct
import sys
import time


def _ioc(direction, nr, size):            # _IOC(dir, 'a', nr, size)
    return (direction << 30) | (size << 16) | (ord("a") << 8) | nr


MSG_FMT = "=QQIIII16s7Bx"                  # struct cec_msg (56 bytes)
MSG_SIZE = struct.calcsize(MSG_FMT)
EVENT_SIZE = 80                            # struct cec_event
CAPS_FMT = "=32s32sIII"                    # struct cec_caps (76 bytes)
CAPS_SIZE = struct.calcsize(CAPS_FMT)

CEC_ADAP_G_CAPS = _ioc(3, 0, CAPS_SIZE)    # _IOWR
CEC_ADAP_G_PHYS_ADDR = _ioc(2, 1, 2)       # _IOR
CEC_RECEIVE = _ioc(3, 6, MSG_SIZE)         # _IOWR
CEC_DQEVENT = _ioc(3, 7, EVENT_SIZE)       # _IOWR
CEC_S_MODE = _ioc(1, 9, 4)                 # _IOW

CEC_MODE_NO_INITIATOR = 0x0
CEC_MODE_MONITOR = 0xE << 4
CEC_MODE_MONITOR_ALL = 0xF << 4
CEC_CAP_MONITOR_ALL = 1 << 5

CEC_EVENT_STATE_CHANGE = 1
CEC_EVENT_LOST_MSGS = 2

PA_INVALID = 0xFFFF

LA_NAMES = ["TV", "Rec1", "Rec2", "Tuner1", "Play1", "Audio", "Tuner2", "Tuner3",
            "Play2", "Rec3", "Tuner4", "Play3", "Bkup1", "Bkup2", "Spec", "Bcast"]

OPCODES = {
    0x00: "Feature Abort", 0x04: "Image View On", 0x0D: "Text View On",
    0x32: "Set Menu Language", 0x36: "Standby",
    0x44: "User Control Pressed", 0x45: "User Control Released",
    0x46: "Give OSD Name", 0x47: "Set OSD Name",
    0x70: "System Audio Mode Request", 0x71: "Give Audio Status",
    0x72: "Set System Audio Mode", 0x7A: "Report Audio Status",
    0x7D: "Give System Audio Mode Status", 0x7E: "System Audio Mode Status",
    0x80: "Routing Change", 0x81: "Routing Information", 0x82: "Active Source",
    0x83: "Give Physical Address", 0x84: "Report Physical Address",
    0x85: "Request Active Source", 0x86: "Set Stream Path",
    0x87: "Device Vendor ID", 0x89: "Vendor Command",
    0x8C: "Give Device Vendor ID", 0x8D: "Menu Request", 0x8E: "Menu Status",
    0x8F: "Give Device Power Status", 0x90: "Report Power Status",
    0x91: "Get Menu Language", 0x9D: "Inactive Source", 0x9E: "CEC Version",
    0x9F: "Get CEC Version", 0xA0: "Vendor Command With ID",
    0xA7: "Request Current Latency", 0xA8: "Report Current Latency",
    0xC0: "Initiate ARC", 0xC1: "Report ARC Initiated",
    0xC2: "Report ARC Terminated", 0xC3: "Request ARC Initiation",
    0xC4: "Request ARC Termination", 0xC5: "Terminate ARC",
}

POWER_STATES = {0: "on", 1: "standby", 2: "standby->on", 3: "on->standby"}


def ts():
    t = time.time()
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int(t % 1 * 1000):03d}"


def out(line):
    print(line, flush=True)


def pa_str(pa):
    if pa == PA_INVALID:
        return "f.f.f.f (none)"
    return f"{pa >> 12 & 0xF}.{pa >> 8 & 0xF}.{pa >> 4 & 0xF}.{pa & 0xF}"


def pa_from(data, offset=0):
    return data[offset] << 8 | data[offset + 1]


def describe(op, data):
    if op in (0x82, 0x84, 0x86, 0x81) and len(data) >= 2:
        return f" PA {pa_str(pa_from(data))}"
    if op == 0x80 and len(data) >= 4:
        return f" {pa_str(pa_from(data))} -> {pa_str(pa_from(data, 2))}"
    if op == 0x47:
        return " \"" + bytes(data).decode("ascii", "replace") + "\""
    if op == 0x90 and data:
        return f" {POWER_STATES.get(data[0], hex(data[0]))}"
    if op == 0x72 and data:
        return " on" if data[0] else " off"
    if data:
        return " " + data.hex(":")
    return ""


class Verdict:
    """Tracks whether this box is the displayed source."""

    def __init__(self):
        self.my_pa = PA_INVALID        # last *valid* physical address
        self.active = None

    def judge(self, op, data):
        if self.my_pa == PA_INVALID:
            return None
        if op in (0x82, 0x86) and len(data) >= 2:      # Active Source / Set Stream Path
            return pa_from(data) == self.my_pa
        if op == 0x80 and len(data) >= 4:              # Routing Change -> new PA
            return pa_from(data, 2) == self.my_pa
        if op == 0x36:                                 # any Standby we can see
            return False
        return None

    def update(self, op, data):
        v = self.judge(op, data)
        if v is not None and v != self.active:
            self.active = v
            out(f"              >>> this box is now {'ACTIVE' if v else 'INACTIVE'}")


def get_caps(fd):
    buf = bytearray(CAPS_SIZE)
    fcntl.ioctl(fd, CEC_ADAP_G_CAPS, buf, True)
    driver, name, _avail, caps, _ver = struct.unpack(CAPS_FMT, buf)
    return (driver.split(b"\0")[0].decode(), name.split(b"\0")[0].decode(), caps)


def get_pa(fd):
    buf = bytearray(2)
    fcntl.ioctl(fd, CEC_ADAP_G_PHYS_ADDR, buf, True)
    return struct.unpack("=H", buf)[0]


def set_mode(fd, want_all):
    if want_all:
        try:
            fcntl.ioctl(fd, CEC_S_MODE,
                        struct.pack("=I", CEC_MODE_NO_INITIATOR | CEC_MODE_MONITOR_ALL))
            return "MONITOR_ALL (whole bus)"
        except OSError as e:
            out(f"MONITOR_ALL refused ({e}); falling back to plain monitor")
    fcntl.ioctl(fd, CEC_S_MODE,
                struct.pack("=I", CEC_MODE_NO_INITIATOR | CEC_MODE_MONITOR))
    return "MONITOR (our traffic + broadcasts)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", default="/dev/cec0")
    ap.add_argument("--all", action="store_true", help="try MONITOR_ALL")
    args = ap.parse_args()

    fd = os.open(args.dev, os.O_RDWR | os.O_NONBLOCK)
    driver, name, caps = get_caps(fd)
    out(f"Adapter {name} ({driver}), MONITOR_ALL "
        f"{'supported' if caps & CEC_CAP_MONITOR_ALL else 'NOT supported'}")
    try:
        mode = set_mode(fd, args.all)
    except OSError as e:
        sys.exit(f"Could not enter monitor mode on {args.dev}: {e} (needs root)")

    v = Verdict()
    pa = get_pa(fd)
    if pa != PA_INVALID:
        v.my_pa = pa
    my_la_mask = 0
    out(f"Monitoring {args.dev} in {mode}; our physical address {pa_str(pa)}")

    poller = select.poll()
    poller.register(fd, select.POLLIN | select.POLLPRI)
    stdin_fd = sys.stdin.fileno() if sys.stdin.isatty() else None
    if stdin_fd is not None:
        poller.register(stdin_fd, select.POLLIN)
        out("Type a note + Enter to insert a marker.")

    while True:
        for pfd, mask in poller.poll():
            if pfd == stdin_fd:
                note = sys.stdin.readline().strip()
                out(f"{ts()}  ======== MARK: {note or '(blank)'} ========")
                continue

            if mask & select.POLLPRI:                       # adapter events
                ev = bytearray(EVENT_SIZE)
                try:
                    fcntl.ioctl(fd, CEC_DQEVENT, ev, True)
                except OSError as e:
                    if e.errno != errno.EAGAIN:
                        raise
                else:
                    _, kind, _ = struct.unpack_from("=QII", ev)
                    if kind == CEC_EVENT_STATE_CHANGE:
                        pa, my_la_mask = struct.unpack_from("=HH", ev, 16)
                        if pa == PA_INVALID:
                            note = f"(HPD bounce - keeping {pa_str(v.my_pa)})"
                        else:
                            v.my_pa = pa
                            note = ""
                        out(f"{ts()}  [event] PA {pa_str(pa)}, "
                            f"LA mask {my_la_mask:#06x} {note}".rstrip())
                    elif kind == CEC_EVENT_LOST_MSGS:
                        out(f"{ts()}  [event] lost messages")

            if mask & select.POLLIN:                        # bus traffic
                while True:
                    buf = bytearray(MSG_SIZE)
                    try:
                        fcntl.ioctl(fd, CEC_RECEIVE, buf, True)
                    except OSError as e:
                        if e.errno == errno.EAGAIN:
                            break
                        raise
                    f = struct.unpack(MSG_FMT, buf)
                    length = f[2]
                    if length == 0:
                        continue
                    msg = f[6][:length]
                    init, dest = msg[0] >> 4, msg[0] & 0xF
                    direction = "TX" if my_la_mask & (1 << init) else "RX"
                    route = f"{LA_NAMES[init]:>6} -> {LA_NAMES[dest]:<6}"
                    if length == 1:
                        out(f"{ts()}  {direction} {route} (poll)")
                        continue
                    op, data = msg[1], msg[2:]
                    name = OPCODES.get(op, f"op {op:#04x}")
                    out(f"{ts()}  {direction} {route} {name}{describe(op, data)}")
                    v.update(op, data)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass

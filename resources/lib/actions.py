"""Low-power actions. Each one saves what it changes and restores exactly that."""
import os
import re
import subprocess

CPUFREQ = "/sys/devices/system/cpu/cpufreq/policy0"
LED_CANDIDATES = (("ACT", "led0"), ("PWR", "led1"))
ETH_MARKER = "ethernet_disabled"


def _read(path):
    with open(path) as f:
        return f.read().strip()


def _write(path, value):
    with open(path, "w") as f:
        f.write(str(value))


class Actions:
    def __init__(self, data_dir, log, jsonrpc, builtin, screensaver_active):
        self.data_dir = data_dir
        self.log = log
        self.jsonrpc = jsonrpc
        self.builtin = builtin
        self.screensaver_active = screensaver_active
        self._applied = {}          # action name -> saved state needed to undo

    # -- public ------------------------------------------------------------
    def recover(self):
        """Undo anything a previous run left behind (crash, reboot while asleep)."""
        marker = os.path.join(self.data_dir, ETH_MARKER)
        if os.path.exists(marker):
            self.log("ethernet was left disabled by a previous run - re-enabling")
            self._connman("enable", "ethernet")
            os.remove(marker)

    def sleep(self, s):
        steps = (("screensaver", s.screensaver, self._ss_on),
                 ("leds", s.leds, self._leds_off),
                 ("cpu", s.cpu, lambda: self._cpu_low(s.cpu_mhz)),
                 ("ethernet", s.ethernet, self._eth_off))
        for name, enabled, fn in steps:
            if enabled and name not in self._applied:
                self._try(name, fn)

    def wake(self):
        # slowest first so the network is back by the time anyone looks
        for name, fn in (("ethernet", self._eth_on), ("cpu", self._cpu_restore),
                         ("leds", self._leds_restore), ("screensaver", self._ss_off)):
            if name in self._applied:
                state = self._applied.pop(name)
                try:
                    fn(state)
                except Exception as e:                  # noqa: BLE001
                    self.log(f"restore {name} failed: {e}", error=True)

    # -- helpers -----------------------------------------------------------
    def _try(self, name, fn):
        try:
            state = fn()
        except Exception as e:                          # noqa: BLE001
            self.log(f"{name} low-power step failed: {e}", error=True)
            return
        if state is not None:
            self._applied[name] = state

    # screensaver
    def _ss_on(self):
        if self.screensaver_active():
            return None                                 # not ours to undo
        self.builtin("ActivateScreensaver")
        return True

    def _ss_off(self, _):
        if self.screensaver_active():
            # JSON-RPC input wakes the screensaver like a key press would
            self.jsonrpc("Input.ExecuteAction", {"action": "noop"})

    # LEDs
    def _leds_off(self):
        saved = []
        for names in LED_CANDIDATES:
            path = next((f"/sys/class/leds/{n}" for n in names
                         if os.path.isdir(f"/sys/class/leds/{n}")), None)
            if not path:
                continue
            m = re.search(r"\[([^\]]+)\]", _read(path + "/trigger"))
            saved.append((path, m.group(1) if m else "none",
                          _read(path + "/brightness")))
            _write(path + "/trigger", "none")
            _write(path + "/brightness", 0)
        return saved or None

    def _leds_restore(self, saved):
        for path, trigger, brightness in saved:
            _write(path + "/trigger", trigger)
            if trigger == "none":
                _write(path + "/brightness", brightness)

    # CPU
    def _cpu_low(self, mhz):
        current = int(_read(CPUFREQ + "/scaling_max_freq"))
        floor = int(_read(CPUFREQ + "/scaling_min_freq"))
        target = max(floor, int(mhz) * 1000)
        if target >= current:
            return None
        _write(CPUFREQ + "/scaling_max_freq", target)
        return current

    def _cpu_restore(self, saved):
        _write(CPUFREQ + "/scaling_max_freq", saved)

    # Ethernet (ConnMan owns the interface on LibreELEC; don't touch ip link)
    def _connman(self, *args):
        r = subprocess.run(["connmanctl", *args], capture_output=True,
                           text=True, timeout=20)
        out = (r.stdout + r.stderr).strip()
        if r.returncode != 0 and "Already" not in out:
            raise RuntimeError(f"connmanctl {' '.join(args)}: {out}")

    def _eth_off(self):
        os.makedirs(self.data_dir, exist_ok=True)
        marker = os.path.join(self.data_dir, ETH_MARKER)
        open(marker, "w").close()           # written first: ConnMan persists it
        try:
            self._connman("disable", "ethernet")
        except Exception:
            os.remove(marker)
            raise
        return True

    def _eth_on(self, _):
        self._connman("enable", "ethernet")
        marker = os.path.join(self.data_dir, ETH_MARKER)
        if os.path.exists(marker):
            os.remove(marker)

"""service.cecpower - low-power mode while this box is not the CEC active source."""
import json
import threading
import time

import xbmc
import xbmcaddon
import xbmcvfs

from resources.lib.actions import Actions
from resources.lib.cecmon import CecWatcher
from resources.lib.policy import Policy

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo("id")
DATA_DIR = xbmcvfs.translatePath(f"special://profile/addon_data/{ADDON_ID}")


def log(msg, error=False, debug=False):
    level = xbmc.LOGERROR if error else (xbmc.LOGDEBUG if debug else xbmc.LOGINFO)
    xbmc.log(f"[{ADDON_ID}] {msg}", level)


def jsonrpc(method, params):
    return xbmc.executeJSONRPC(json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}))


class Settings:
    def __init__(self):
        a = xbmcaddon.Addon()
        self.delay_s = max(10, a.getSettingInt("sleep_delay_s"))
        self.backstop_s = max(0, a.getSettingInt("idle_backstop_s"))
        self.screensaver = a.getSettingBool("act_screensaver")
        self.leds = a.getSettingBool("act_leds")
        self.cpu = a.getSettingBool("act_cpu")
        self.cpu_mhz = a.getSettingInt("cpu_mhz")
        self.ethernet = a.getSettingBool("act_ethernet")
        self.device = a.getSettingString("cec_device") or "/dev/cec0"
        self.verbose = a.getSettingBool("verbose_log")


class Service(xbmc.Monitor):
    def __init__(self):
        super().__init__()
        self.settings = Settings()
        self.lock = threading.Lock()
        self.policy = Policy(time.monotonic())
        self.actions = Actions(
            DATA_DIR, log, jsonrpc, xbmc.executebuiltin,
            lambda: xbmc.getCondVisibility("System.ScreenSaverActive"))

    def onSettingsChanged(self):
        self.settings = Settings()
        log("settings reloaded")

    def on_verdict(self, verdict, reason):
        if self.settings.verbose:
            log(f"CEC: {reason} -> {'selected' if verdict else 'not selected'}")
        with self.lock:
            self.policy.cec(verdict, time.monotonic())

    def run(self):
        log("starting")
        self.actions.recover()
        watcher = CecWatcher(self.settings.device, self.on_verdict, log)
        watcher.start()
        try:
            while not self.waitForAbort(1):
                s = self.settings
                with self.lock:
                    decision = self.policy.step(
                        time.monotonic(), xbmc.getGlobalIdleTime(),
                        # Player.Playing is false while paused, unlike isPlaying()
                        xbmc.getCondVisibility("Player.Playing"),
                        s.delay_s, s.backstop_s)
                if decision == "sleep":
                    log("entering low power")
                    self.actions.sleep(s)
                elif decision == "wake":
                    log("leaving low power")
                    self.actions.wake()
        finally:
            watcher.stop()
            self.actions.wake()
            log("stopped")


if __name__ == "__main__":
    Service().run()

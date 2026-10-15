# service.cecpower — CEC Power Saver

Kodi service add-on for LibreELEC (Pi 4, LE 12 / Kodi 21). Puts the box into a
low-power state while it is not the displayed HDMI source, and restores it the
moment it is selected again.

## How it decides

A background thread opens `/dev/cec0` in the kernel's passive **monitor** mode,
alongside Kodi's libCEC. It never transmits. MONITOR_ALL is not supported by
`vc4_hdmi`, so it sees broadcasts plus traffic to/from this box — that is enough.

| CEC message | Meaning |
|---|---|
| Set Stream Path / Active Source = our PA | selected → wake |
| Set Stream Path / Active Source = other PA | not selected |
| Routing Change → other PA | not selected (Denon power-off goes to 3.1.0.0) |
| Standby (broadcast) | not selected (TV-remote power-off) |

HPD bounces (PA briefly f.f.f.f on every Denon switch) are ignored; the last
valid PA is kept.

**Sleep** when nothing is playing (paused counts as not playing — pair with
Kodi's CEC setting "Pause playback when switching to another source"), the box has not been selected for the delay
(unknown at boot counts as not selected), there has been no input for the
delay, and at least the delay has passed since the last wake / end of playback.
Optional idle backstop: also sleep while selected after N seconds of no input.
Both timers are set in seconds (sleep delay minimum 10 s, default 180 s).

**Wake** on any "we were selected" message, playback starting, or any input.

## Low-power actions (each optional, each restores exactly what it saved)

- Activate the configured Kodi screensaver (dismissed via JSON-RPC noop on wake)
- ACT/PWR LEDs → trigger none, brightness 0
- `policy0/scaling_max_freq` capped (default 600 MHz = Pi 4 floor)
- Ethernet via `connmanctl disable ethernet` — **off by default**. ConnMan
  persists this across reboots, so a marker file in addon_data is written first
  and the service re-enables Ethernet at startup if it finds it.

Ethernet jack LEDs can't be changed at runtime; use `dtparam=eth_led0=4`,
`dtparam=eth_led1=4` in config.txt if wanted.

## Topology this was built against

TV 0.0.0.0 · Denon 3.0.0.0 · Audio-Kodi 3.2.0.0 · DVD 3.3.0.0 · Video-Kodi 3.5.0.0.
The TV always powers on to its own home screen (Active Source 0.0.0.0).
A separate keep-alive service re-sends Set OSD Name every ~10 s; it does not
affect this add-on.

## Troubleshooting

Enable "Log CEC routing messages" and watch `kodi.log` for `[service.cecpower]`.
`cec_probe.py` (included; standalone monitor with typed markers, run over SSH) is the bench tool.

"""Pure sleep/wake decision logic (no Kodi imports, unit-testable)."""


class Policy:
    """
    Sleep when ALL of:
      - nothing is playing, and >= delay since playback stopped / last wake
      - this box has not been the CEC source for >= delay  (unknown counts
        as "not the source", measured from service start)
      - no user input for >= delay
    or (backstop, if > 0): still the source but no input for >= backstop.

    Wake on ANY of:
      - a CEC message saying this box was selected (every such message,
        not only a change, so a re-select after a backstop sleep wakes us)
      - playback starts
      - user input (Kodi idle time went down)
    """

    def __init__(self, now):
        self.active = None              # None = unknown since start
        self.inactive_since = now
        self.selected_event = False     # set by every "we were selected" msg
        self.asleep = False
        self.awake_since = now          # every wake earns a full delay
        self.last_idle = 0

    def cec(self, verdict, now):
        if verdict:
            self.selected_event = True
            self.active = True
        else:
            if self.active is not False:
                self.inactive_since = now
            self.active = False

    def step(self, now, idle, playing, delay_s, backstop_s):
        """Return 'sleep', 'wake' or None."""
        input_seen = idle < self.last_idle
        self.last_idle = idle
        selected, self.selected_event = self.selected_event, False

        if self.asleep:
            if selected or playing or input_seen:
                self.asleep = False
                self.awake_since = now
                return "wake"
            return None

        if playing:
            self.awake_since = now      # end of playback restarts the clock
            return None
        awake_for = now - self.awake_since
        if self.active is not True:
            if (now - self.inactive_since >= delay_s and idle >= delay_s
                    and awake_for >= delay_s):
                self.asleep = True
                return "sleep"
        elif backstop_s > 0 and idle >= backstop_s and awake_for >= backstop_s:
            self.asleep = True
            return "sleep"
        return None

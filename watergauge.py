class WaterGauge:
    """
    prod = WattGauge()
    prod.set_liters(millis(), current_liters)
    prod.set_liters(millis(), current_liters)
    prod.set_liters(millis(), current_liters)

    prod.get_liters_per_second()
    """
    def __init__(self):
        self._t = []            # t(end-2), t(end-1), t(end)
        self._p = []            # P(sum) in t[n]
        self._liters_per_ms = 0.0   # average value, but only if sensible

    def get_current_liters(self):
        "Get the latest stored value"
        try:
            return self._p[-1]
        except IndexError:
            return 0

    def get_milliliters_per_second(self):
        "Get a best guess of the water usage (l/ms -> ml/s)"
        return int(self._liters_per_ms * 1000000)

    def set_liters(self, time_ms, current_l):
        "Feed data to the WaterGauge: do this often"
        try:
            self._p[2]
        except IndexError:
            # This is during the first values only. Do this in an exception
            # handler which is the uncommon (slow) case.
            if len(self._p) == 0 or current_l != self._p[-1]:
                self._t.append(time_ms)
                self._p.append(current_l)
            if len(self._p) < 3:
                return
            self._recalculate()
            return

        # New values? Lets gooo!
        if current_l != self._p[2]:
            self._t.append(time_ms)
            self._p.append(current_l)
            self._t.pop(0)  # tests with python3.10 say append+pop is ..
            self._p.pop(0)  # .. cheaper than doing x,y,z=a,b,c transform

            self._recalculate()
            return

        # Unchanged litre count.

        # It already had a flow of 0? Keep 0.
        if self._liters_per_ms == 0.0:
            return

        # Compare flows.
        last_flow = (
            (self._p[2] - self._p[0]) / (self._t[2] - self._t[0]))
        last_last_flow = (
            (self._p[2] - self._p[1]) / (self._t[2] - self._t[1]))

        if last_last_flow > (1.1 * last_flow):
            # >10% flow increase? Use only latest.
            last_flow = last_last_flow
            t_prev = self._t[1]
            p_prev = self._p[1]
        else:
            # Use both times for better averages.
            t_prev = self._t[0]
            p_prev = self._p[0]

        # Would we have expected another increase by now? If so,
        # then the flow has likely been turned off.
        hypothetical_flow = (current_l - p_prev) / (time_ms - t_prev)
        assert hypothetical_flow <= last_flow, (
            self._t, self._p, time_ms, hypothetical_flow, last_flow)

        if hypothetical_flow >= (0.5 * last_flow):
            # Hypothetical flow 50% of the previous flow or more,
            # keep, for now.
            pass
        else:
            # Set it to zero. Likely the faucet has been turned off.
            self._liters_per_ms = 0.0

    def _recalculate(self):
        # print('t=', self._t, ';p=', self._p)

        # Recalculate based on two values or one, depending on the timing.
        t10 = self._t[1] - self._t[0]
        t21 = self._t[2] - self._t[1]
        p10 = self._p[1] - self._p[0]
        p21 = self._p[2] - self._p[1]

        if p21 > p10:
            # More liters than the last time. Take only latest values.
            t21 = self._t[2] - self._t[1]
            self._liters_per_ms = (p21 / t21)
        elif 0.8 < (t21 / t10) < 1.2:
            # Small time difference. Take average of two values.
            t20 = self._t[2] - self._t[0]
            p20 = self._p[2] - self._p[0]
            self._liters_per_ms = (p20 / t20)
        else:
            # Take only the last in other cases.
            self._liters_per_ms = (p21 / t21)

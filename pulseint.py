from asyncio import sleep
from logging import getLogger
from time import time


log = getLogger()


class DigitalPulseReadError(ValueError):
    pass


class AnalogCalibrator:
    MIN_DIFF = 500  # we get 16-bit values..

    def __init__(self, low=16500, high=18500):
        self._many_values = []
        self.low, self.high = low, high
        self.med1, self.med2 = None, None

        if self.low and self.high:
            self.calculate()

    def feed(self, value):
        self._many_values.append(value)

        if len(self._many_values) >= 800:
            self.calibrate()
            self._many_values = []

    def calibrate(self):
        # order values, and truncate 5% from each side
        vals = list(sorted(self._many_values))
        p5 = len(vals) // 20
        assert p5 > 1, (p5, vals)
        vals = vals[p5:-p5]
        low, high = vals[0], vals[-1]

        # first time only
        if self.low is None:
            self.low = low
        if self.high is None:
            self.high = max(high, low + self.MIN_DIFF)

        # we must be able to correct ourselves, so we make low
        # slightly less low and high slightly less high
        if (self.high - self.low - 1) > self.MIN_DIFF:
            self.high -= 1
        if (self.high - self.low - 1) > self.MIN_DIFF:
            self.low += 1

        # take the new values
        if low < self.low:
            self.low = low
        if high > self.high:
            self.high = high

        self.calculate()

    def calculate(self):
        diff = (self.high - self.low)
        self.med1 = self.low + diff // 3
        self.med2 = self.high - diff // 3


class AnalogCalibratingPulseParser:
    def __init__(self, low=None, high=None):
        kwargs = {}
        if low is not None:
            kwargs['low'] = low
        if high is not None:
            kwargs['high'] = high

        self.calibrator = AnalogCalibrator(**kwargs)
        self._was_above = None
        self.high_pulse = None

    def feed(self, value):
        self.high_pulse = None
        self.calibrator.feed(value)

        # initial
        if self._was_above is None:
            if self.calibrator.med1 is not None:
                if value < self.calibrator.med1:
                    self._was_above = False
                elif value > self.calibrator.med2:
                    self._was_above = True
            return

        if self._was_above is False:
            if value > self.calibrator.med2:
                self._was_above = True
                self.high_pulse = True
        elif self._was_above is True:
            if value < self.calibrator.med1:
                self._was_above = False
                self.high_pulse = False


class AnalogPulseInterpreter:
    """
    FIXME: documentation about expected range..
    """
    SLEEP_BETWEEN_READINGS = 0.1

    def __init__(self, on_pulse, on_no_pulse):
        self._parser = AnalogCalibratingPulseParser(low=16500, high=18500)
        self.on_pulse = on_pulse
        self.on_no_pulse = on_no_pulse

    async def run(self):
        # Collect a bunch of readings a while so we can debug this. Do this one
        # minute every hour.
        t0 = time()
        dbg_show = False
        dbg_coll = []
        prev_low, prev_high = None, None
        thigh = None

        while True:
            value = self.analog_read()
            self._parser.feed(value)
            low = self._parser.calibrator.low
            high = self._parser.calibrator.high

            if (time() - t0) % 3600 < 60:
                dbg_coll.append(value)
                if len(dbg_coll) >= 20:
                    dbg_show = True
            elif dbg_coll:
                dbg_show = True

            if dbg_show:
                values = ' '.join(str(v) for v in dbg_coll)
                dbg_coll.clear()
                dbg_show = False
                log.debug(f'Analog readings: {values}')

            if self._parser.high_pulse is True:
                thigh = time()
                log.debug(f'got (high) pulse {value} [{low}..{high}]')
                # self.on_pulse()
            elif self._parser.high_pulse is False:
                # The pulse is on 1 digit of 10, so the duration of one
                # pulse will give us an approximation of flow rate.
                # We've measured it to be about 11%. (This depends on
                # the med1/med2 values from the calibrator.)
                if thigh is not None:
                    # 11% of 10,000mL = 1100 -> 1100/dT (mL/s)
                    flow_mlps = 1100 / (time() - thigh)
                else:
                    flow_mlps = None

                log.debug(
                    f'got (low) pulse {value} [{low}..{high}] '
                    f'{flow_mlps} (mL/s)')
                self.on_pulse(estimated_flow_mlps=flow_mlps)

            if low != prev_low or high != prev_high:
                log.debug(
                    f'recalibrated: {prev_low}->{low} {prev_high}->{high}')
                prev_low = low
                prev_high = high

            await sleep(self.SLEEP_BETWEEN_READINGS)


class DigitalPulseInterpreter:
    """
    FIXME: documentation about expected range..
    """
    MAX_PULSE_EVERY = 1         # no_pulse every 1s if there was a recent pulse
    MIN_PULSE_EVERY = 60        # no_pulse if nothing for this long
    LONG_TIMEOUT = 0.100        # normal sleep
    SHORT_TIMEOUT = 0.010       # the short one for bounce avoidance
    PULSE_CONFIRM_COUNT = 3     # pulse confirmed after 3 (extra) good readings

    def __init__(self, on_pulse, on_no_pulse):
        self.on_pulse = on_pulse
        self.on_no_pulse = on_no_pulse

    async def run(self):
        old_value = await self.get_stable_value()
        old_time = time()
        pulse_time = old_time - self.MIN_PULSE_EVERY

        while True:
            new_value = self.digital_read()
            if new_value != old_value:
                checked_value = await self.get_stable_value()

                if checked_value == new_value:
                    # log.debug(f'new value, changing to {checked_value}')
                    if checked_value:
                        self.on_pulse()
                        pulse_time = time()
                    old_value = checked_value

                else:
                    log.debug(f'absorbed jitter, keeping {checked_value}')

            # If there was no recent pulse, send a no_pulse every
            # MIN_PULSE_EVERY.
            # If there _was_ a recent pulse, send a no_pulse every
            # MAX_PULSE_EVERY.
            new_time = time()
            if ((new_time - old_time) >= self.MIN_PULSE_EVERY or (
                    (new_time - pulse_time) < self.MIN_PULSE_EVERY and
                    (new_time - old_time) >= self.MAX_PULSE_EVERY)):
                self.on_no_pulse()
                old_time = new_time

            await sleep(self.LONG_TIMEOUT)

    async def get_stable_value(self):
        values = []

        while True:
            await sleep(self.SHORT_TIMEOUT)
            values.append(self.digital_read())

            if len(values) >= self.PULSE_CONFIRM_COUNT:
                if len(set(values[-self.PULSE_CONFIRM_COUNT:])) == 1:
                    return values[-1]

            if len(values) >= 100:
                raise DigitalPulseReadError('bouncing values', values)

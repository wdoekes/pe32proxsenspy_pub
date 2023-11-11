from asyncio import sleep
from logging import getLogger
from time import time


log = getLogger()


class DigitalPulseReadError(ValueError):
    pass


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
                    log.debug(f'new value, changing to {checked_value}')
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

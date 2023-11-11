from asyncio import run as run_async_code
from unittest import TestCase

from pulseint import DigitalPulseInterpreter, DigitalPulseReadError


class StopTheTest(RuntimeError):
    pass


class GotPulse(RuntimeError):
    pass


class GotNoPulse(RuntimeError):
    pass


class FlakyExampleSensor(DigitalPulseInterpreter):
    def __init__(self):
        super().__init__((lambda: None), (lambda: None))
        self._calls = 0

    def digital_read(self):
        self._calls += 1
        assert self._calls < 500, (self._calls, 'should have error by now')
        # ping-pong values are bad
        return (self._calls % 2) == 1


class BetterExampleSensor(DigitalPulseInterpreter):
    def __init__(self, stop_after, values):
        def raise_1():
            raise GotPulse()

        def raise_2():
            raise GotNoPulse()

        super().__init__(raise_1, raise_2)
        self._values = values
        self._stop_after = stop_after
        self._calls = 0

    def digital_read(self):
        idx = self._calls % len(self._values)
        ret = self._values[idx]
        self._calls += 1
        if self._calls >= self._stop_after:
            raise StopTheTest()
        return ret


class DigitalPulseInterpreterTest(TestCase):
    def test_value_error_after_ping_pong(self):
        flaky_sensor = FlakyExampleSensor()
        with self.assertRaises(DigitalPulseReadError):
            run_async_code(flaky_sensor.run())

    def test_only_zeroes(self):
        "We have some noise between our zeroes"
        sensor = BetterExampleSensor(20, [
            0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 1, 0, 0, 0, 0])
        with self.assertRaises(StopTheTest):
            run_async_code(sensor.run())

    def test_only_ones(self):
        "We have some noise between our ones"
        sensor = BetterExampleSensor(20, [
            1, 1, 1, 0, 1, 1, 1, 1, 1, 0, 0, 1, 1, 1, 1])
        sensor = BetterExampleSensor(20, [1])
        with self.assertRaises(StopTheTest):
            run_async_code(sensor.run())

    def test_pulse(self):
        sensor = BetterExampleSensor(20, [
            0, 0, 0, 1, 0, 0, 0, 0, 1, 1, 1, 1, 0])
        with self.assertRaises(GotPulse):
            run_async_code(sensor.run())

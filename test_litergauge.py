import unittest

from litergauge import LiterGauge


class LiterGaugeTestCase(unittest.TestCase):
    def test_litergauge(self):
        inputs = (
            # At t = 0
            ("10:10:00.000", 0, 0), 
            ("10:11:00.000", 0, 0),
            ("10:12:00.000", 0, 0),
            # +1
            ("10:12:20.000", 1, 0),
            ("10:12:30.000", 1, 0),
            ("10:12:40.000", 1, 0),
            ("10:12:50.000", 1, 0),
            # +2/20 secs
            ("10:13:00.000", 2, 25),
            ("10:13:20.000", 3, 50),
            ("10:13:40.000", 4, 50),
            ("10:14:00.000", 5, 50),
            # Nothing for a while
            ("10:14:30.000", 5, 20),
            ("10:15:00.000", 5, 0),
            # And then slow increase
            ("10:15:30.000", 6, 11),
            ("10:16:00.000", 6, 8),
            ("10:16:30.000", 7, 16),
            ("10:17:00.000", 7, 11),
            ("10:17:30.000", 8, 16),
            ("10:18:00.000", 8, 11),
            # Fast increase and then quick stop
            ("10:19:00.000", 8, 6),
            ("10:20:00.000", 12, 26),
            ("10:20:10.000", 16, 400),
            ("10:20:20.000", 16, 0),
            ("10:20:30.000", 16, 0),
        )
        gauge = LiterGauge()
        for tmstr, relative, flow in inputs:
            h, m, s = tmstr.split(':', 2)
            s, ms = s.split('.')
            current_ms = (
                int(h) * 1000 * 3600 +
                int(m) * 1000 * 60 +
                int(s) * 1000 +
                int(ms))
            gauge.set_liters(current_ms, relative)
            calculated_flow = gauge.get_milliliters_per_second()
            self.assertEqual(calculated_flow, flow, (
                f'got unexpected {calculated_flow} at '
                f'("{tmstr}", {relative}, {flow})'))

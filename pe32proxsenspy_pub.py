#!/usr/bin/env python3
import asyncio

from contextlib import AsyncExitStack
from logging import getLogger
from os import environ, getpid
from time import time

from RPi import GPIO
from asyncio_mqtt import Client as MqttClient
from smbus import SMBus

from litergauge import LiterGauge
from pulseint import AnalogPulseInterpreter, DigitalPulseInterpreter


__version__ = 'pe32proxsenspy_pub-FIXME'

log = getLogger()


def millis():
    return int(time() * 1000)


APP_START_TM = millis()


class DeadMansSwitchTripped(Exception):
    pass


async def dead_mans_switch(processor):
    while True:
        tdelta = processor.ms_since_last_value()
        # FIXME: a day? we'll use less water when we're away!
        if tdelta >= 86.4e+06:  # 86400 seconds = 1 day
            raise DeadMansSwitchTripped(
                f'more than {tdelta} ms have passed without changes')
        await asyncio.sleep(1)


class Pe32ProxSensPublisher:
    def __init__(self, prefix):
        self._prefix = prefix  # 'w_' (water) or 'g_' (gas)

        self._mqtt_broker = environ.get(
            'PE32PROXSENS_BROKER', 'test.mosquitto.org')
        self._mqtt_topic = environ.get(
            'PE32PROXSENS_TOPIC', 'myhome/infra/water/xwwwform')
        self._mqttc = None
        self._guid = environ.get(
            'PE32PROXSENS_GUID', 'EUI48:11:22:33:44:55:66')

    def open(self):
        # Unfortunately this does use a thread for keepalives. Oh well.
        # As long as it's implemented correctly, I guess we can live
        # with it.
        self._mqttc = MqttClient(self._mqtt_broker)
        return self._mqttc

    async def publish(self, absolute, relative, flow):
        log.info(f'publish: {absolute} {relative} {flow}')

        tm = millis() - APP_START_TM
        mqtt_string = (
            f'device_id={self._guid}&'
            f'{self._prefix}absolute_l={absolute}&'
            f'{self._prefix}relative_l={relative}&'
            f'{self._prefix}flow_mlps={flow}&'
            f'dbg_uptime={tm}&'
            f'dbg_version={__version__}').encode('ascii')

        try:
            await self._mqttc.publish(self._mqtt_topic, payload=mqtt_string)
        except Exception as e:
            log.error(f'Got error during publish of {mqtt_string}: {e}')
            exit(1)

        log.debug(f'Published: {mqtt_string}')


class ProximitySensorProcessor:
    MIN_PUBLISH_MS = 300000  # at least once every 5 minutes

    def __init__(self, publisher, liters_per_pulse=1):
        self._liters = 0
        self._litergauge = LiterGauge()
        self._publisher = publisher
        self._liters_per_pulse = liters_per_pulse
        self._last_pulse = millis()

        self._published_absolute_liters = None
        self._published_relative_liters = None
        self._published_flow = None
        self._published_time = millis()

    def ms_since_last_value(self):
        return millis() - self._last_pulse

    def pulse(self):
        self._liters += self._liters_per_pulse
        self._last_pulse = millis()
        self._update()

    def no_pulse(self):
        self._update()

    def _update(self):
        self._litergauge.set_liters(millis(), self._liters)

        absolute_liters = -1
        relative_liters = self._liters
        flow = self._litergauge.get_milliliters_per_second()

        if (absolute_liters != self._published_absolute_liters or
                relative_liters != self._published_relative_liters or
                flow != self._published_flow or
                (millis() - self._published_time) >= self.MIN_PUBLISH_MS):
            self._published_absolute_liters = absolute_liters
            self._published_relative_liters = relative_liters
            self._published_flow = flow
            self._published_time = millis()

            # Schedule for execution in someone elses time.
            loop = asyncio.get_event_loop()
            # PROBLEM: If the connection is broken, we get a
            # asyncio_mqtt.error.MqttCodeError from publish().
            # ... which is unhandled.
            # > loop.call_soon() is specifically meant to be used for
            # > callbacks, which usually are very simple functions used to hook
            # > into events (job done, exception was raised in future, etc.),
            # > and they are not expected to cooperate.
            # This should not be done with call_soon. Instead we should have a
            # different continuous task that waits for an event and then does
            # the publish(). Here we should just poke that publish.
            loop.call_soon(
                asyncio.create_task, self._publisher.publish(
                    absolute_liters, relative_liters, flow))


class GpioProximitySensorInterpreter(DigitalPulseInterpreter):
    """
    GPIO interface to LJ12A3-4-Z/BX

    NPN-NO (Normally Open, sens wire LOW (light on) when metals are detected)

    "This switch has a low response frequency but a good stability."
    """
    def __init__(self, gpio_pin, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._gpio_pin = gpio_pin

    async def open(self):
        log.info(GPIO.RPI_INFO)
        GPIO.setwarnings(True)
        GPIO.setmode(GPIO.BOARD)

        GPIO.setup(self._gpio_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    def close(self):
        log.debug('(GpioProximitySensorInterpreter.close)')
        GPIO.cleanup()

    def digital_read(self):
        # Not this in asyncio yet...?
        # > if GPIO.wait_for_edge(self._gpio_pin, GPIO.RISING,
        # >   timeout=self.LONG_TIMEOUT_MS) is None:
        return GPIO.input(self._gpio_pin) == GPIO.LOW


class ADS1115ConfigReg:
    __fields__ = (
        # 15    = Operational status or single-shot conversion start
        ('conversion', 1, 0b1),
        # 14:12 = Input multiplexer configuration (ADS1115 only)
        #         100 : AINP = AIN0 and AINN = GND
        ('mux', 3, 0b100),
        # 11:9  = Programmable gain amplifier configuration
        #         001 : FSR = +/-4.096 V
        ('pga', 3, 0b001),
        # 8     = Device operating mode
        #         0 : continuous (vs. one-shot / low power)
        ('mode', 1, 0b0),
        # 7:5   = Data rate
        #         100 : 128 SPS (default), samples/second?
        ('dr', 3, 0b100),
        # 4     = Comparator mode (ADS1114 and ADS1115 only)
        #         0 : Traditional comparator (default)
        ('comp_mode', 1, 0b0),
        # 3     = Comparator polarity (ADS1114 and ADS1115 only)
        #         0 : Active low (default)
        ('comp_pol', 1, 0b0),
        # 2     = Latching comparator (ADS1114 and ADS1115 only)
        #         0 : Nonlatching comparator (default)
        ('comp_latch', 1, 0b0),
        # 1:0   = Comparator queue and disable (ADS1114 and ADS1115 only)
        #         11 : Disable comparator (default)
        ('comp_que', 2, 0b11),
    )

    def __init__(self, **kwargs):
        for name, bits, default in self.__fields__:
            setattr(self, name, default)

    def from_int(self, total):
        for name, bits, default in reversed(self.__fields__):
            bitmask = (1 << bits) - 1
            value = total & bitmask
            setattr(self, name, value)
            total >>= bits

    def as_int(self):
        total = 0
        for name, bits, default in self.__fields__:
            total <<= bits
            bitmask = (1 << bits) - 1
            value = getattr(self, name) & bitmask
            total |= value
        return total

    def as_bytes(self):
        value = self.as_int()
        return [value >> 8, value & 0xFF]


class AnalogHallsensorInterpreter(AnalogPulseInterpreter):
    """
    I2C interface to ADS1115 (ADC) backed by SS49/AH49 Hall sensor

    FIXME: document config needed to get i2c up and running on RPi
    """
    # We can change address to 0x49, 0x4A and 0x4B by hooking the
    # address pin to (VDD/)SDA/SCL/GND.
    ADS1115_ADDRESS = 0x48

    def __init__(self, i2c_dev, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._i2c_dev = i2c_dev  # 1 means '/dev/i2c-1'

    async def open(self):
        self._bus = SMBus(self._i2c_dev)

        # Select configuration register: 0x01
        reg = ADS1115ConfigReg()
        reg.comparator = 0b100  # select AINP = AIN0 and AINN = GND
        reg.pga = 0b001         # select +/-4.096 V
        self._bus.write_i2c_block_data(
            self.ADS1115_ADDRESS, 0x01, reg.as_bytes())

    def close(self):
        log.debug('(AnalogHallsensorInterpreter.close)')

    def analog_read(self):
        # Read value: 0x00
        data = self._bus.read_i2c_block_data(self.ADS1115_ADDRESS, 0x00, 2)
        raw_adc = data[0] << 8 | data[1]
        if raw_adc > 32767:
            raw_adc -= 65535
        return raw_adc


async def main(proxsens_gpio_pin, publisher_class=Pe32ProxSensPublisher):
    async def cancel_tasks(tasks):
        log.debug(f'Checking tasks {tasks!r}')
        for task in tasks:
            if task.done():
                log.debug(f'- task {task} was already done')
                continue
            try:
                log.debug(f'- task {task} to be cancelled')
                task.cancel()
                await task
            except asyncio.CancelledError:
                pass

    async with AsyncExitStack() as stack:
        # Keep track of the asyncio tasks that we create, so that
        # we can cancel them on exit
        tasks = set()
        stack.push_async_callback(cancel_tasks, tasks)

        if proxsens_gpio_pin == 1:
            publisher = publisher_class('g_')  # gas
        else:
            publisher = publisher_class('w_')  # water
        await stack.enter_async_context(publisher.open())

        if proxsens_gpio_pin == 1:
            processor = ProximitySensorProcessor(
                publisher, liters_per_pulse=10)
        else:
            processor = ProximitySensorProcessor(publisher)

        # Create signal interpreter, open connection and push
        # shutdown code.
        if proxsens_gpio_pin == 1:
            proxsens_client = AnalogHallsensorInterpreter(
                i2c_dev=proxsens_gpio_pin,
                on_pulse=processor.pulse, on_no_pulse=processor.no_pulse)
        else:
            proxsens_client = GpioProximitySensorInterpreter(
                gpio_pin=proxsens_gpio_pin,
                on_pulse=processor.pulse, on_no_pulse=processor.no_pulse)
        await proxsens_client.open()
        stack.callback(proxsens_client.close)  # synchronous!

        # Start our two tasks.
        # XXX: do we need a publisher task here as well; one that can
        # die if there is something permanently wrong with the mqtt?
        tasks.add(asyncio.create_task(
            proxsens_client.run(), name='proxsens_client'))
        tasks.add(asyncio.create_task(
            dead_mans_switch(processor), name='dead_mans_switch'))

        # Execute tasks and handle exceptions.
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_EXCEPTION)
        assert done
        for task in done:
            task.result()  # raises exceptions if any


if __name__ == '__main__':
    import logging
    import sys

    called_from_cli = (
        # Reading just JOURNAL_STREAM or INVOCATION_ID will not tell us
        # whether a user is looking at this, or whether output is passed to
        # systemd directly.
        any(fp.isatty() for fp in (sys.stdin, sys.stdout, sys.stderr)) or
        not environ.get('JOURNAL_STREAM'))
    sys.stdout.reconfigure(line_buffering=True)  # PYTHONUNBUFFERED, but better
    logging.basicConfig(
        level=(
            logging.DEBUG if environ.get('PE32PROXSENS_DEBUG', '')
            else logging.INFO),
        format=(
            '%(asctime)s %(message)s' if called_from_cli
            else '%(message)s'),
        stream=sys.stdout,
        datefmt='%Y-%m-%d %H:%M:%S')

    print(f"pid {getpid()}: send SIGINT or SIGTERM to exit.")
    loop = asyncio.get_event_loop()
    proxsens_gpio_pin = int(sys.argv[1])  # XXX: fixme, parse "GPIO6->22
    main_coro = main(proxsens_gpio_pin, publisher_class=Pe32ProxSensPublisher)
    loop.run_until_complete(main_coro)
    loop.close()
    print('end of main')

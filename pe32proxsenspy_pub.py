#!/usr/bin/env python3
import asyncio

from contextlib import AsyncExitStack
from logging import getLogger
from os import environ, getpid
from time import time

from asyncio_mqtt import Client as MqttClient
from RPi import GPIO

from litergauge import LiterGauge
from pulseint import DigitalPulseInterpreter


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
    def __init__(self):
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

    async def publish(self, absolute, relative, speed):
        log.info(f'publish: {absolute} {relative} {speed}')

        tm = millis() - APP_START_TM
        mqtt_string = (
            f'device_id={self._guid}&'
            f'w_absolute_l={absolute}&'
            f'w_relative_l={relative}&'
            f'w_speed_mlps={speed}&'  # FIXME: speed->flow?
            f'dbg_uptime={tm}&'
            f'dbg_version={__version__}').encode('ascii')

        await self._mqttc.publish(self._mqtt_topic, payload=mqtt_string)

        log.debug(f'Published: {mqtt_string}')


class ProximitySensorProcessor:
    MIN_PUBLISH_MS = 300000  # at least once every 5 minutes

    def __init__(self, publisher):
        self._liters = 0
        self._litergauge = LiterGauge()
        self._publisher = publisher
        self._last_pulse = millis()

        self._published_absolute_liters = None
        self._published_relative_liters = None
        self._published_flow = None
        self._published_time = millis()

    def ms_since_last_value(self):
        return millis() - self._last_pulse

    def pulse(self):
        self._liters += 1
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
            loop.call_soon(
                asyncio.create_task, self._publisher.publish(
                    absolute_liters, relative_liters, flow))


class GpioProximitySensorInterpreter(DigitalPulseInterpreter):
    """
    GPIO interface to LJ12A3-4-Z/BX

    NPN-NO (Normally Open, light on when metals er detected, LOW sensing wire)

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

        publisher = publisher_class()
        await stack.enter_async_context(publisher.open())

        processor = ProximitySensorProcessor(publisher)

        # Create signal interpreter, open connection and push
        # shutdown code.
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

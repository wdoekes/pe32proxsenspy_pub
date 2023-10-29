#!/usr/bin/env python3
import asyncio
import logging
import os
import sys
import time

from contextlib import AsyncExitStack

from asyncio_mqtt import Client as MqttClient
from RPi import GPIO

from litergauge import LiterGauge


APP_START_TM = int(time.time() * 1000)


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


__version__ = 'pe32proxsenspy_pub-FIXME'

log = logging.getLogger()


class Pe32ProxSensPublisher:
    def __init__(self):
        self._mqtt_broker = os.environ.get(
            'PE32PROXSENS_BROKER', 'test.mosquitto.org')
        self._mqtt_topic = os.environ.get(
            'PE32PROXSENS_TOPIC', 'myhome/infra/water/xwwwform')
        self._mqttc = None
        self._guid = os.environ.get(
            'PE32PROXSENS_GUID', 'EUI48:11:22:33:44:55:66')

    def open(self):
        # Unfortunately this does use a thread for keepalives. Oh well.
        # As long as it's implemented correctly, I guess we can live
        # with it.
        self._mqttc = MqttClient(self._mqtt_broker)
        return self._mqttc

    async def publish(self, absolute, relative, speed):
        log.info(f'publish: {absolute} {relative} {speed}')

        tm = int(time.time() * 1000) - APP_START_TM
        mqtt_string = (
            f'device_id={self._guid}&'
            f'w_absolute_l={absolute}&'
            f'w_relative_l={relative}&'
            f'w_speed_mlps={speed}&'
            f'dbg_uptime={tm}&'
            f'dbg_version={__version__}').encode('ascii')

        await self._mqttc.publish(self._mqtt_topic, payload=mqtt_string)

        log.debug(f'Published: {mqtt_string}')


class ProximitySensorProcessor:
    def __init__(self, publisher):
        self._liters = 0
        self._litergauge = LiterGauge()
        self._publisher = publisher
        self._last_pulse = self._last_activity = int(time.time() * 1000)

    def ms_since_last_value(self):
        return int(time.time() * 1000 - self._last_pulse)

    def pulse(self):
        self._liters += 1
        self._last_pulse = last_activity = int(time.time() * 1000)

        self._litergauge.set_liters(last_activity, self._liters)

        loop = asyncio.get_event_loop()
        loop.call_soon(asyncio.create_task, self.publish_pulse())

    def no_pulse(self):
        last_activity = int(time.time() * 1000)

        self._litergauge.set_liters(last_activity, self._liters)

        loop = asyncio.get_event_loop()
        loop.call_soon(asyncio.create_task, self.publish_pulse())

    async def publish_pulse(self):
        await self._publisher.publish(
            -1, self._liters, self._litergauge.get_milliliters_per_second())


class ProximitySensorClient:
    LONG_TIMEOUT = 0.100        # normal sleep
    SHORT_TIMEOUT = 0.010       # the short one for bounce avoidance
    PULSE_CONFIRM_COUNT = 3     # pulse confirmed after 3 (extra) good readings

    def __init__(self, gpio_pin, processor):
        self._gpio_pin = gpio_pin
        self._processor = processor

    async def open(self):
        log.info(GPIO.RPI_INFO)
        GPIO.setwarnings(True)
        GPIO.setmode(GPIO.BOARD)

        GPIO.setup(self._gpio_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    def close(self):
        log.debug('(ProximitySensorClient.close)')
        GPIO.cleanup()

    async def run(self):
        # Not this in asyncio yet...?
        # > if GPIO.wait_for_edge(self._gpio_pin, GPIO.RISING,
        # >   timeout=self.LONG_TIMEOUT_MS) is None:

        old_value = await self.get_stable_value()
        old_time = time.time()

        while True:
            new_value = GPIO.input(self._gpio_pin)
            if new_value != old_value:
                checked_value = await self.get_stable_value()

                if checked_value == new_value:
                    log.debug(f'new value, changing to {checked_value}')
                    if checked_value == GPIO.LOW:
                        self._processor.pulse()
                    old_value = checked_value

                else:
                    log.debug(f'absorbed jitter, keeping {checked_value}')

            await asyncio.sleep(self.LONG_TIMEOUT)

            new_time = time.time()
            if new_time - old_time >= 60:
                self._processor.no_pulse()
                old_time = new_time

    async def get_stable_value(self):
        values = []

        while True:
            await asyncio.sleep(self.SHORT_TIMEOUT)
            values.append(GPIO.input(self._gpio_pin))

            if len(values) >= self.PULSE_CONFIRM_COUNT:
                if len(set(values[:-self.PULSE_CONFIRM_COUNT])) == 1:
                    return values[-1]

            if len(values) >= 100:
                raise ValueError('bouncing values', values)


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

        # Create ProximitySensorProcessor client, open connection and push
        # shutdown code.
        proxsens_client = ProximitySensorClient(proxsens_gpio_pin, processor)
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
    called_from_cli = (
        # Reading just JOURNAL_STREAM or INVOCATION_ID will not tell us
        # whether a user is looking at this, or whether output is passed to
        # systemd directly.
        any(os.isatty(i.fileno())
            for i in (sys.stdin, sys.stdout, sys.stderr)) or
        not os.environ.get('JOURNAL_STREAM'))
    sys.stdout.reconfigure(line_buffering=True)  # PYTHONUNBUFFERED, but better
    logging.basicConfig(
        level=(
            logging.DEBUG if os.environ.get('PE32PROXSENS_DEBUG', '')
            else logging.INFO),
        format=(
            '%(asctime)s %(message)s' if called_from_cli
            else '%(message)s'),
        stream=sys.stdout,
        datefmt='%Y-%m-%d %H:%M:%S')

    print(f"pid {os.getpid()}: send SIGINT or SIGTERM to exit.")
    loop = asyncio.get_event_loop()
    proxsens_gpio_pin = int(sys.argv[1])  # XXX: fixme, parse "GPIO6->22
    main_coro = main(proxsens_gpio_pin, publisher_class=Pe32ProxSensPublisher)
    loop.run_until_complete(main_coro)
    loop.close()
    print('end of main')

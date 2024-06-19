pe32proxsenspy_pub
==================

**Project Energy 32:** *Read analog water meter or analog gas meter pulses
using inductive sensors, export water and gas usage using MQTT.*

The water sensor component uses a digital proximity sensor (LJ12A3-4-Z/BX) that
detects metallic proximity over a distance of at most 4mm.

The gas sensor component uses an analog *hall sensor* (SS49E) that ...

... alternatives at e.g. https://github.com/gizmocuz/esp_proximity_sensor_mqtt


----
TODO
----

* Fewer syscalls, more waiting (see below).

* Fix images/setup/howto here.

* Detect MQTT send misconfiguration? Right now we saw multiple pushes
  being stuck (multiple tasks hanging at mqtt publish?).


*Fewer syscalls*

The basis for this is the following code. Ideally we'd like to do fewer
syscalls (longer epoll_wait) than we do now. For another day.

.. code-block:: python

    import time
    import RPi.GPIO as GPIO

    from datetime import datetime

    GPIO6 = 22

    def main():
        # Aqua-pin:
        PIN_AQUA = GPIO6

        print(GPIO.RPI_INFO)
        GPIO.setwarnings(True)
        GPIO.setmode(GPIO.BOARD)

        GPIO.setup(PIN_AQUA, GPIO.IN)

        current = GPIO.input(PIN_AQUA)
        long_timeout_ms = 5000
        short_timeout_ms = 100

        t0 = time.time()
        liters = 0
        while True:
            if current == GPIO.LOW:
                if GPIO.wait_for_edge(PIN_AQUA, GPIO.RISING, timeout=long_timeout_ms) is None:
                    #print('we are LOW, no change')
                    continue
                if GPIO.wait_for_edge(PIN_AQUA, GPIO.FALLING, timeout=short_timeout_ms) is not None:
                    print(datetime.now(), 'we saw LOW->HIGH->LOW change, ignoring')
                    continue
                current = GPIO.HIGH
                #print('we re now HIGH')
            else:
                if GPIO.wait_for_edge(PIN_AQUA, GPIO.FALLING, timeout=long_timeout_ms) is None:
                    #print('we are HIGH, no change')
                    continue
                if GPIO.wait_for_edge(PIN_AQUA, GPIO.RISING, timeout=short_timeout_ms) is not None:
                    print(datetime.now(), 'we saw HIGH->LOW->HIGH change, ignoring')
                    continue
                current = GPIO.LOW
                #print('we re now LOW')
                liters += 1
                print(datetime.now(), liters)


    try:
        main()
    except KeyboardInterrupt:
        pass
    finally:
        GPIO.cleanup()


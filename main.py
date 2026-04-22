"""Synchronous main loop for StudioLight.

One loop, four tickables:

  * WifiManager      -- advances the Wi-Fi state machine
  * StatusIndicator  -- renders the current state to the LED strip
  * ConfigServer     -- accepts HTTP config requests while in AP mode
  * LightController  -- receives ON/OFF UDP commands from Ableton while
                        connected to Wi-Fi

No asyncio, no tasks, no callbacks (other than the one-shot "config saved"
hook from ConfigServer back to WifiManager). The loop sleeps for
``_LOOP_PERIOD_MS`` between iterations, which is fast enough for smooth
animations and slow enough to leave plenty of CPU for USB CDC.

A short purple countdown runs before anything touches Wi-Fi. This gives a
guaranteed window for Ctrl-C in the REPL before the radio comes up -- a
lighter-weight escape hatch than holding BOOT at power-on (boot.py already
honours that too via SAFE MODE).

Display policy when Wi-Fi is connected:
  * For the first ``_CONFIRM_GREEN_MS`` ms after joining, show solid green
    so the operator visually confirms the join.
  * After that, the strip reflects the LightController: solid red when
    Ableton has asked for ``ON``, fully off otherwise.
  * If the link drops, we fall back to the Wi-Fi indicator patterns.
"""
import time
import json


_STRIP_PIN = 14
_STRIP_PIXELS = 64
_PIXEL_ORDER = "GRB"
_STARTUP_DELAY_SECONDS = 5
_LOOP_PERIOD_MS = 40  # ~25 Hz
_CONFIG_PATH = "/networks.json"
_UDP_LISTEN_PORT = 8000
_CONFIRM_GREEN_MS = 2000


def _countdown_blip(pin, num_pixels, seconds):
    """One brief purple flash per remaining second, with a print to serial.

    Purely synchronous, intentionally -- it runs before any other module
    is imported so the REPL is reachable if something in wifi_manager or
    status_indicator would otherwise crash at import time.
    """
    try:
        import machine
        import neopixel
        np = neopixel.NeoPixel(machine.Pin(pin), num_pixels)
    except Exception as e:
        print("main: countdown skipped (neopixel init failed:", e, ")")
        time.sleep(seconds)
        return

    dim_purple_grb = (0, 2, 6)
    off = (0, 0, 0)
    for remaining in range(seconds, 0, -1):
        print("main: wifi starts in", remaining,
              "s (hold BOOT + reset for safe mode)")
        for i in range(num_pixels):
            np[i] = dim_purple_grb
        np.write()
        time.sleep(0.1)
        for i in range(num_pixels):
            np[i] = off
        np.write()
        time.sleep(0.9)


def _read_config_server_password(config_path):
    try:
        with open(config_path, "r") as f:
            cfg = json.loads(f.read())
        return cfg.get("config_server", {}).get("password", "micropython")
    except Exception:
        return "micropython"


def run():
    from wifi_manager import WifiManager
    from status_indicator import StatusIndicator
    from config_server import ConfigServer
    from light_controller import LightController

    print("main: starting up")
    _countdown_blip(_STRIP_PIN, _STRIP_PIXELS, _STARTUP_DELAY_SECONDS)

    indicator = StatusIndicator(
        pin=_STRIP_PIN,
        num_pixels=_STRIP_PIXELS,
        pixel_order=_PIXEL_ORDER,
    )
    wifi = WifiManager(config_path=_CONFIG_PATH)
    server = ConfigServer(
        port=8080,
        password=_read_config_server_password(_CONFIG_PATH),
        config_path=_CONFIG_PATH,
        on_saved=wifi.reload_and_reconnect,
    )
    controller = LightController(port=_UDP_LISTEN_PORT)

    print("main: entering main loop")
    last_state = None
    connected_at_ms = None

    while True:
        now_ms = time.ticks_ms()

        wifi.tick(now_ms)

        if wifi.state != last_state:
            print("main: state ->", wifi.state, wifi.info)
            if last_state == "ap_mode" and wifi.state != "ap_mode":
                server.stop()
            if wifi.state != "connected":
                controller.stop()
                connected_at_ms = None
            last_state = wifi.state

        if wifi.state == "connected":
            if connected_at_ms is None:
                connected_at_ms = now_ms
            controller.tick()
            if time.ticks_diff(now_ms, connected_at_ms) < _CONFIRM_GREEN_MS:
                display_state = "connected"
            else:
                display_state = "light_on" if controller.light_on else "light_off"
        else:
            display_state = wifi.state

        indicator.render(display_state, now_ms)

        if wifi.state == "ap_mode":
            server.tick()

        time.sleep_ms(_LOOP_PERIOD_MS)


run()

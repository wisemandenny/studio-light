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
  * For the first ``_CONNECT_CELEBRATION_MS`` ms after joining, show the
    8x8 trippy rainbow plasma so the operator visually confirms the join
    from across the room.
  * After that, the strip reflects the LightController: solid red when
    Ableton has asked for ``ON``, fully off otherwise.
  * If the link drops, we fall back to the Wi-Fi indicator patterns.

Display policy while in AP mode:
  * Default: amber square-wave blink ("come configure me").
  * If every configured password has already failed AND the operator is
    actively using the config page (recent HTTP activity), switch to the
    amber-amber-red warning flash so a bad password is obvious at a
    glance -- the original reason this feature exists.

BOOT-button as runtime override:
  A short click of the BOOT button (GPIO0) while the device is running
  disconnects Wi-Fi and forces AP mode, so the operator can re-enter the
  config page without a power cycle. This is distinct from -- and
  compatible with -- boot.py's safe-mode check, which reads the same pin
  once at reset. Runtime polling here will never see the boot-time hold
  because boot.py has already finished by the time main.py runs.
"""
import time
import json


_STRIP_PIN = 14
_STRIP_PIXELS = 64
_PIXEL_ORDER = "GRB"
# 8x8 matrix geometry for the trippy-rainbow animation. If your matrix is
# wired serpentine (every other row reversed -- very common on cheap 8x8
# WS2812B modules), flip this to True. It only affects how per-pixel
# animations are written; solid-colour frames look identical either way.
_MATRIX_WIDTH = 8
_MATRIX_HEIGHT = 8
_MATRIX_SERPENTINE = False
_STARTUP_DELAY_SECONDS = 5
_LOOP_PERIOD_MS = 40  # ~25 Hz
_CONFIG_PATH = "/networks.json"
_UDP_LISTEN_PORT = 8000
# Duration of the trippy rainbow "hello, I joined the network" banner.
# Long enough to be unmistakable from across a studio; short enough that it
# doesn't delay actual recording work.
_CONNECT_CELEBRATION_MS = 4000
# Treat the config page as "actively being used" for this long after the
# last HTTP request on the config server. Inside this window, ap_mode
# switches to the auth-failed flash pattern if every known password has
# already failed -- the bright red pip is the "hey, check your typing" cue.
_CONFIG_ACTIVITY_WINDOW_MS = 30000

# BOOT button (GPIO0) is polled each tick to offer a "force AP mode" action
# at runtime. It must stay low for this many consecutive ticks before we
# treat it as a real click -- at _LOOP_PERIOD_MS = 40ms, 2 ticks is ~80ms,
# comfortably past contact bounce (typically <10ms) while still feeling
# instant on rapid repeat presses. The ripple animation itself is
# non-blocking, so this debounce window is the only source of latency
# between a physical click and the first ripple frame.
_BOOT_BUTTON_PIN = 0
_BOOT_BUTTON_DEBOUNCE_TICKS = 2


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


def _init_boot_button(pin_num):
    """Return a pull-up input on ``pin_num``, or None if init fails.

    Failing soft matters: on a dev environment without ``machine`` (or on
    a board where GPIO0 is somehow unavailable), we shouldn't crash the
    whole startup just because the optional override doesn't work.
    """
    try:
        import machine
        return machine.Pin(pin_num, machine.Pin.IN, machine.Pin.PULL_UP)
    except Exception as e:
        print("main: BOOT button init failed ({}); runtime override disabled"
              .format(e))
        return None


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
        width=_MATRIX_WIDTH,
        height=_MATRIX_HEIGHT,
        serpentine=_MATRIX_SERPENTINE,
    )
    wifi = WifiManager(config_path=_CONFIG_PATH)
    server = ConfigServer(
        port=8080,
        password=_read_config_server_password(_CONFIG_PATH),
        config_path=_CONFIG_PATH,
        on_saved=wifi.reload_and_reconnect,
        validator=wifi.validate_credentials,
    )
    controller = LightController(port=_UDP_LISTEN_PORT)

    boot_button = _init_boot_button(_BOOT_BUTTON_PIN)

    print("main: entering main loop")
    last_state = None
    connected_at_ms = None
    boot_low_ticks = 0
    boot_fired = False

    while True:
        now_ms = time.ticks_ms()

        # BOOT-button poll: edge-trigger on a stable low, require release
        # before re-arming. Every accepted click plays the ripple; only
        # the *first* click (while still on Wi-Fi) tears down the link
        # and brings up the AP. Subsequent presses once already in AP
        # mode are pure "yes, the button works" feedback for the
        # operator and never re-initialise the radio.
        # Skipped silently if the pin object failed to initialise
        # (e.g. on a build without machine.Pin).
        if boot_button is not None:
            pressed = boot_button.value() == 0
            if pressed:
                boot_low_ticks += 1
                if (boot_low_ticks >= _BOOT_BUTTON_DEBOUNCE_TICKS
                        and not boot_fired):
                    boot_fired = True
                    # start_ripple() is non-blocking -- it just records a
                    # (start_ms, color) entry that render() will animate
                    # over the next ~600ms. Stacking is handled inside
                    # the indicator; we don't need to care here.
                    indicator.start_ripple(now_ms)
                    if wifi.state != "ap_mode":
                        print("main: BOOT click -> ripple + forcing AP mode")
                        wifi.force_ap_mode()
                        last_state = None  # force the state-change log line
                    else:
                        print("main: BOOT click -> ripple (already in ap_mode)")
            else:
                boot_low_ticks = 0
                boot_fired = False

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
            if time.ticks_diff(now_ms, connected_at_ms) < _CONNECT_CELEBRATION_MS:
                display_state = "connected"
            else:
                display_state = "light_on" if controller.light_on else "light_off"
        elif wifi.state == "ap_mode":
            recently_active = (
                server.activity_at_ms != 0
                and time.ticks_diff(now_ms, server.activity_at_ms)
                < _CONFIG_ACTIVITY_WINDOW_MS
            )
            if wifi.had_auth_failure and recently_active:
                display_state = "ap_mode_auth_failed"
            else:
                display_state = "ap_mode"
        else:
            display_state = wifi.state

        indicator.render(display_state, now_ms)

        if wifi.state == "ap_mode":
            server.tick()

        time.sleep_ms(_LOOP_PERIOD_MS)


run()

"""Boot-time entry point.

Adds two protections on top of the previous no-op boot.py:

1. Safe mode via BOOT button (GPIO0).
   If GPIO0 is held LOW at reset (i.e. the BOOT button is held down), we skip
   `main.py` entirely and drop directly to the REPL. This is our always-works
   escape hatch for when main.py hangs, starves USB, or otherwise misbehaves
   -- we never need to re-flash the chip or race an 8-second window again.

   Note: main.py polls the same pin at runtime for a separate purpose
   (a short click forces AP mode). The two uses don't conflict because
   this check only runs once at reset; by the time main.py is polling,
   boot.py is already done.

2. Fail-safe wifi fallback is intentionally removed.
   The previous boot.py's `except` branch called WifiManager.setup_network()
   directly, which also starts the AP and (on ESP32-S3) may starve the native
   USB CDC. If main.py fails, we now just let the REPL come up; recovery
   tooling like mpremote remains available.
"""
import machine

_SAFE_MODE_PIN = 0  # GPIO0 is the BOOT button on most ESP32-S3 dev boards.


def _safe_mode_requested() -> bool:
    try:
        pin = machine.Pin(_SAFE_MODE_PIN, machine.Pin.IN, machine.Pin.PULL_UP)
        # Read a few times to debounce; the button is actively held low.
        low_count = sum(1 for _ in range(5) if pin.value() == 0)
        return low_count >= 4
    except Exception as e:
        print(f"boot.py: safe-mode check failed ({e}); proceeding normally")
        return False


if _safe_mode_requested():
    print("boot.py: SAFE MODE (BOOT button held) -- skipping main.py")
else:
    try:
        import main  # noqa: F401 -- side-effect import
    except Exception as e:
        # Don't auto-start WifiManager on failure; USB reliability matters more
        # than network-availability when main.py has already crashed.
        print(f"boot.py: main.py raised {type(e).__name__}: {e}")
        print("boot.py: staying at REPL for recovery")

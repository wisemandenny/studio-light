"""UDP command listener for the Ableton companion script.

The Ableton plugin (Ableton/StudioLight.py) sends plain-text UDP
datagrams to this port whenever Live's record-mode toggles. Two shapes
are accepted:

* ``ON r g b`` -- turn the recording light on with the supplied RGB.
  Each channel is a decimal 0-255. This is what current plugins emit.
* ``ON`` (no colour) -- turn on with the default red. This preserves
  backward compatibility with older plugin versions that predate the
  colour-picker feature.
* ``OFF`` -- turn the light off. Colour is unchanged so the next ``ON``
  without an RGB resumes the previous colour.
* ``HEARTBEAT`` -- keepalive from the Ableton script. Updates
  ``last_message_at_ms`` without changing light state. Used by the
  main loop's idle-mode timeout.

The plugin sends each command five times for reliability, so this
listener drains all pending datagrams on every tick and keeps only the
final interpretation (state + colour).

State is exposed as ``light_on`` (bool) and ``light_color`` ((r,g,b))
for the main loop to read and translate into a display state for the
LED strip. Nothing here writes to the strip directly -- that stays the
indicator's job.

The listen socket is created lazily by ``start()`` (called from ``tick()``)
and torn down by ``stop()`` whenever the caller decides the controller
shouldn't be listening (e.g. while Wi-Fi is down).
"""
import socket
import time


_DEFAULT_COLOR = (255, 0, 0)


class LightController:
    def __init__(self, port=8000):
        self.port = port
        self.light_on = False
        self.light_color = _DEFAULT_COLOR
        self.last_message_at_ms = 0
        self._sock = None

    def start(self):
        if self._sock is not None:
            return True
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", self.port))
            s.settimeout(0.01)
            self._sock = s
            print("light_controller: listening on UDP :{}".format(self.port))
            return True
        except OSError as e:
            print("light_controller: bind failed:", e)
            self._sock = None
            return False

    def stop(self):
        if self._sock is None:
            return
        try:
            self._sock.close()
        except Exception:
            pass
        self._sock = None
        print("light_controller: stopped")

    def _parse_on(self, msg):
        """Return the RGB tuple encoded in an ``ON`` message.

        Bare ``ON`` resolves to the default red, matching the pre-colour
        plugin. A malformed tail (non-int or missing channels) also falls
        back to default rather than silently using partial data.
        """
        parts = msg.split()
        if len(parts) < 4:
            return _DEFAULT_COLOR
        try:
            r = int(parts[1]) & 0xFF
            g = int(parts[2]) & 0xFF
            b = int(parts[3]) & 0xFF
        except ValueError:
            return _DEFAULT_COLOR
        return (r, g, b)

    def tick(self):
        """Drain every pending datagram and apply the final on/off state.

        Draining (rather than handling one per tick) matters because the
        Ableton plugin transmits each state change five times in quick
        succession for loss-tolerance on UDP; without draining, we'd leave
        four stale datagrams in the kernel buffer.
        """
        if self._sock is None and not self.start():
            return

        while True:
            try:
                data, addr = self._sock.recvfrom(64)
            except OSError:
                return
            if not data:
                return
            msg = data.decode("utf-8", "replace").strip().upper()
            if msg.startswith("ON"):
                new_color = self._parse_on(msg)
                # Log any transition that a human would care about:
                # off->on, or on->on-with-a-different-colour.
                if not self.light_on:
                    print("light_controller: ON {} from {}".format(new_color, addr[0]))
                elif new_color != self.light_color:
                    print("light_controller: color {} from {}".format(new_color, addr[0]))
                self.light_on = True
                self.light_color = new_color
            elif msg == "OFF":
                if self.light_on:
                    print("light_controller: OFF from", addr[0])
                self.light_on = False
            elif msg == "HEARTBEAT":
                pass
            else:
                print("light_controller: ignoring unknown message:", repr(msg))
            self.last_message_at_ms = time.ticks_ms()

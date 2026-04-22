"""UDP command listener for the Ableton companion script.

The Ableton plugin (Ableton/StudioLight.py) sends plain-text ``ON`` and
``OFF`` datagrams to this port whenever Live's record-mode toggles. The
plugin sends each command five times for reliability, so this listener
drains all pending datagrams on every tick and keeps only the final
interpretation ("on" or "off").

State is exposed as the ``light_on`` attribute for the main loop to read
and translate into a display state for the LED strip. Nothing here writes
to the strip directly -- that stays the indicator's job.

The listen socket is created lazily by ``start()`` (called from ``tick()``)
and torn down by ``stop()`` whenever the caller decides the controller
shouldn't be listening (e.g. while Wi-Fi is down).
"""
import socket
import time


class LightController:
    def __init__(self, port=8000):
        self.port = port
        self.light_on = False
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
            if msg == "ON":
                if not self.light_on:
                    print("light_controller: ON from", addr[0])
                self.light_on = True
            elif msg == "OFF":
                if self.light_on:
                    print("light_controller: OFF from", addr[0])
                self.light_on = False
            else:
                print("light_controller: ignoring unknown message:", repr(msg))
            self.last_message_at_ms = time.ticks_ms()

import socket
import subprocess
import time
import re
import Live  # type: ignore[import-not-found]  # provided by Ableton runtime


# Red is the canonical "recording in progress" colour. The light only
# deviates from this when the user intentionally changes the master
# track's colour -- that's the in-Ableton "color picker" for this
# device. We chose the master track specifically because nobody ever
# changes it for normal musical reasons, so any change is almost
# certainly "I want my recording light to be this colour".
_DEFAULT_COLOR = (255, 0, 0)


def _unpack_rgb(packed):
    """Unpack Live's 32-bit ``track.color`` integer into (r, g, b).

    Live stores track colours as a packed ARGB int; we only care about
    the low 24 bits. ``None`` (or anything non-int) falls back to the
    default recording colour so we never send a malformed datagram.
    """
    try:
        value = int(packed)
    except (TypeError, ValueError):
        return _DEFAULT_COLOR
    return ((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)


class StudioLight:
    def __init__(self, c_instance):
        self.c_instance = c_instance
        self.song = self.c_instance.song()
        self.master_track = self.song.master_track

        self.target_mac = self._canonical_mac("a0:f2:62:eb:2e:a4")
        self.ip = None
        self.port = 8000
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        # Baseline the master track's colour at load time. As long as
        # the user hasn't touched it, we keep sending the default red;
        # the moment it differs from this baseline we treat that as the
        # user "picking" a new recording-light colour. Changing it back
        # to the baseline reverts the light to red, which matches the
        # natural expectation of "undo my pick".
        self._baseline_master_color = self._current_master_color_int()
        self._master_color_listener_attached = False

        self.find_ip_by_mac()
        self.song.add_record_mode_listener(self.on_record_mode_changed)

        # Master-track colour listener is best-effort: if a given Live
        # version doesn't expose it, we silently fall back to only
        # re-reading the colour on record-mode toggles, which is still
        # functional -- just not live-updating while a take is rolling.
        try:
            self.master_track.add_color_listener(self.on_master_color_changed)
            self._master_color_listener_attached = True
        except Exception:
            pass

    @staticmethod
    def _canonical_mac(mac_str):
        """Return a MAC address in lowercase ``xx:xx:xx:xx:xx:xx`` form.

        macOS ``arp -a`` strips leading zeros from each octet (so
        ``0a:1b:00:...`` prints as ``a:1b:0:...``), which breaks naive
        string comparisons against a zero-padded constant. Normalising both
        sides with this helper before comparison avoids that class of
        false-negative.
        """
        parts = re.split(r"[:\-]", (mac_str or "").strip().lower())
        if len(parts) != 6:
            return (mac_str or "").strip().lower()
        return ":".join(p.zfill(2) for p in parts)

    @staticmethod
    def _local_broadcast_addresses():
        """Return every IPv4 broadcast address reported by ``ifconfig``.

        Replaces the previous hardcoded ``192.168.117.255`` -- that only
        refreshed the ARP cache on one specific subnet, so the scan silently
        failed on every other network.
        """
        try:
            raw = subprocess.check_output(
                ["ifconfig"], stderr=subprocess.DEVNULL
            )
        except Exception:
            return []
        out = (raw or b"").decode(errors="replace")
        return re.findall(r"broadcast (\d+\.\d+\.\d+\.\d+)", out)

    def find_ip_by_mac(self):
        """Scan the local ARP table for the board's MAC address and cache its IP.

        Works on any subnet: we enumerate every broadcast address the host
        currently has and ping each to elicit ARP replies, rather than
        assuming a single hardcoded subnet. MACs are normalised before
        comparison so a zero-stripped ``arp -a`` entry still matches.
        """
        try:
            # Fire off one broadcast ping per interface, in parallel. These
            # are side-effects only (we never read their output); we just
            # want the kernel to populate its ARP cache with responding hosts.
            pings = []
            for bcast in self._local_broadcast_addresses():
                try:
                    pings.append(subprocess.Popen(
                        ["ping", "-c", "1", bcast],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    ))
                except Exception:
                    pass

            # Give the responders ~800 ms to reply, then reap the pings so
            # we don't leak zombie processes into Live.
            time.sleep(0.8)
            for p in pings:
                try:
                    p.terminate()
                except Exception:
                    pass

            raw = subprocess.check_output(["arp", "-a"])
            output = (raw or b"").decode(errors="replace")

            mac_re = re.compile(r"at ([0-9a-fA-F:]+)")
            ip_re = re.compile(r"\(([0-9.]+)\)")

            for line in output.split("\n"):
                mac_match = mac_re.search(line)
                if not mac_match:
                    continue
                if self._canonical_mac(mac_match.group(1)) != self.target_mac:
                    continue
                ip_match = ip_re.search(line)
                if ip_match:
                    self.ip = ip_match.group(1)
                    self.c_instance.show_message("Light Found at " + self.ip)
                    return

            self.c_instance.show_message("Light MAC not found on network")
        except Exception as e:
            self.c_instance.show_message("ARP Scan Error: " + str(e))

    def _current_master_color_int(self):
        """Read the raw packed colour int off the master track.

        Returns ``None`` if the attribute is missing or unreadable, so
        ``_effective_color`` can fall back to the default cleanly.
        """
        try:
            return int(self.master_track.color)
        except Exception:
            return None

    def _effective_color(self):
        """Return the RGB tuple to send with the next ``ON`` datagram.

        While the master track's colour matches what it was when the
        script loaded, we treat that as "user hasn't picked anything" and
        send the default red. As soon as the user changes the master
        colour, that becomes the recording-light colour.
        """
        current = self._current_master_color_int()
        if current is None or current == self._baseline_master_color:
            return _DEFAULT_COLOR
        return _unpack_rgb(current)

    def _send(self, msg):
        if not self.ip:
            return
        # Five copies for UDP loss tolerance, matching the existing
        # on/off semantics. The receiver drains all pending datagrams
        # per tick so duplicates collapse back into one state update.
        for _ in range(5):
            try:
                self.sock.sendto(msg.encode(), (self.ip, self.port))
            except Exception:
                pass

    def on_record_mode_changed(self):
        if not self.ip:
            self.find_ip_by_mac()

        if not self.ip:
            return

        if self.song.record_mode:
            r, g, b = self._effective_color()
            self._send("ON {} {} {}".format(r, g, b))
        else:
            self._send("OFF")

    def on_master_color_changed(self):
        """Live-update the light if the user changes the colour mid-take.

        Outside of recording there's nothing to do: the next record-on
        will read the fresh colour itself. We deliberately avoid the ARP
        re-scan fallback here -- that path belongs to record-mode
        transitions, which is the user's natural "is the light working?"
        checkpoint.
        """
        if self.ip and self.song.record_mode:
            r, g, b = self._effective_color()
            self._send("ON {} {} {}".format(r, g, b))

    def disconnect(self):
        try:
            self.song.remove_record_mode_listener(self.on_record_mode_changed)
        except Exception:
            pass
        if self._master_color_listener_attached:
            try:
                self.master_track.remove_color_listener(self.on_master_color_changed)
            except Exception:
                pass
        try:
            self.sock.close()
        except Exception:
            pass

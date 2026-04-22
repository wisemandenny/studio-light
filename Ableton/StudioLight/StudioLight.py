import socket
import subprocess
import time
import re
import Live  # type: ignore[import-not-found]  # provided by Ableton runtime


class StudioLight:
    def __init__(self, c_instance):
        self.c_instance = c_instance
        self.song = self.c_instance.song()

        self.target_mac = self._canonical_mac("a0:f2:62:eb:2e:a4")
        self.ip = None
        self.port = 8000
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        self.find_ip_by_mac()
        self.song.add_record_mode_listener(self.on_record_mode_changed)

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

    def on_record_mode_changed(self):
        if not self.ip:
            self.find_ip_by_mac()

        if self.ip:
            msg = "ON" if self.song.record_mode else "OFF"
            for _ in range(5):
                self.sock.sendto(msg.encode(), (self.ip, self.port))

    def disconnect(self):
        self.song.remove_record_mode_listener(self.on_record_mode_changed)
        self.sock.close()

"""Synchronous Wi-Fi manager for MicroPython.

One class, one state machine, no asyncio, no callbacks. The host code drives
progress by calling ``tick(now_ms)`` regularly from its main loop. Nothing
in the normal tick path blocks for more than a few milliseconds; the per-
attempt timeout is enforced by ``time.ticks_diff`` rather than by ``sleep``.

Public state values (read from the ``.state`` attribute):

  * ``booting``    -- initial; no attempt started yet
  * ``connecting`` -- actively attempting a known network
  * ``connected``  -- ``wlan.isconnected()`` is True
  * ``ap_mode``    -- all candidates exhausted; soft-AP is up

Transitions:

  booting    -> connecting (candidates exist)
  booting    -> ap_mode    (no candidates and AP policy allows it)
  connecting -> connected  (join succeeded within the per-attempt window)
  connecting -> connecting (next candidate, on per-candidate timeout)
  connecting -> ap_mode    (all candidates exhausted, AP policy allows)
  connected  -> connecting (link lost)
  ap_mode    -> booting    (reload_and_reconnect() called)

Link-loss detection:

  ``wlan.isconnected()`` alone is not enough. When an AP disappears
  ungracefully -- the classic case is a mobile phone hotspot being
  switched off -- ESP-IDF never receives a deauth frame and the STA
  can go on reporting ``isconnected() == True`` for a very long time,
  while no packets actually flow. That is exactly how the device ended
  up sitting in "light_off" mode after a studio hotspot disappeared.

  To catch that, we also run a cheap active TCP probe to the current
  default gateway every ``_PROBE_INTERVAL_MS`` (see _poll_connected).
  Two consecutive probe failures in a row trip ``_handle_link_loss``,
  which drops the stale association, kicks the state machine back to
  ``connecting``, and -- if all candidates fail, which they will if
  the AP is genuinely gone -- lands us in ``ap_mode`` (per
  ``start_policy``). A single probe failure is tolerated silently
  because brief outages on real networks are common.
"""
import json
import time
import network


_DEFAULT_AP = {
    "config": {"essid": "MicroPython-AP", "password": "micropython"},
    "start_policy": "never",
    "enables_webrepl": False,
}

# Active connectivity probe while ``connected``. See module docstring.
# Interval is long enough to be cheap and short enough that "phone hotspot
# just died" is caught within ~20s (two probes, then ``_handle_link_loss``).
_PROBE_INTERVAL_MS = 10000
# Short socket timeout: if the gateway is alive, connect usually completes
# in well under 100ms on the same subnet. 800ms keeps the worst-case
# main-loop pause brief while still surviving a transiently slow AP.
_PROBE_TIMEOUT_S = 0.8
_PROBE_FAIL_THRESHOLD = 2


class WifiManager:
    def __init__(self, config_path="/networks.json", per_attempt_ms=6000):
        self.config_path = config_path
        self.per_attempt_ms = per_attempt_ms

        self.state = "booting"
        self.info = {}

        # True once we've attempted at least one configured network in the
        # current "cycle" and none of them joined. The operator UX layer uses
        # this to distinguish "AP mode because no config" from "AP mode
        # because your passwords are wrong" -- the latter gets an extra
        # red pip in the status indicator so typos stand out.
        self.had_auth_failure = False

        self._candidates = []
        self._current_idx = 0
        self._attempt_started_ms = 0
        self._ap_policy = "never"
        self._ap_config = dict(_DEFAULT_AP)
        self._reload_pending = False

        # Active-probe state; both zeroed whenever we (re)enter ``connected``.
        self._last_probe_ms = 0
        self._probe_failures = 0

        self._load_config()
        self._sta = network.WLAN(network.STA_IF)
        self._ap = network.WLAN(network.AP_IF)

    # --- Public API -----------------------------------------------------

    @property
    def ap_essid(self):
        return self._ap_config.get("config", {}).get("essid", "")

    @property
    def ap_password(self):
        return self._ap_config.get("config", {}).get("password", "")

    def tick(self, now_ms):
        """Advance the state machine by one step.

        Safe to call every main-loop iteration. A pending reload (scheduled
        by reload_and_reconnect()) is applied here rather than inline so that
        any HTTP response that triggered the reload has time to flush before
        we tear down the network.
        """
        if self._reload_pending:
            self._reload_pending = False
            self._apply_reload()

        try:
            if self.state == "booting":
                self._enter_from_boot(now_ms)
            elif self.state == "connecting":
                self._poll_connecting(now_ms)
            elif self.state == "connected":
                self._poll_connected(now_ms)
            elif self.state == "ap_mode":
                pass
        except Exception as e:
            print("wifi_manager: tick error:", e)

    def reload_and_reconnect(self):
        """Schedule a reload on the next tick.

        Deferred so that callers mid-HTTP-response (ConfigServer's POST
        handler) can finish sending their response before the AP interface
        is torn down.
        """
        self._reload_pending = True

    def force_ap_mode(self):
        """Operator override: drop STA and come up as AP right now.

        Triggered from the main loop by a BOOT-button click. Lets the
        operator re-enter the config page without power-cycling or
        editing ``known_networks``. Synchronous -- we're already on the
        main thread and there's no in-flight HTTP response to preserve
        (the server only runs in ap_mode, which this call is about to
        enter).

        Leaves the STA interface active (disconnected) so the validator
        path still works immediately. Deliberately clears
        ``had_auth_failure`` so the status indicator doesn't flash the
        red "wrong password" pip for a manual entry -- the passwords
        didn't necessarily fail; the operator just wants to reconfigure.
        """
        print("wifi_manager: force_ap_mode requested")
        try:
            self._sta.disconnect()
        except Exception:
            pass
        self.info.pop("ssid", None)
        self.info.pop("ip", None)
        self.had_auth_failure = False
        self._start_ap()
        self.state = "ap_mode"

    def validate_credentials(self, ssid, password, timeout_ms=7000):
        """Try to join ``ssid`` with ``password`` and report the outcome.

        Blocking by design: called synchronously from the HTTP handler that
        answers ``POST /validate``, so the user's browser simply waits on the
        response. The state machine is paused while this runs (we're in
        ap_mode, whose tick is a no-op), which is exactly what we want.

        Returns ``(ok: bool, message: str)``. On failure ``message`` is a
        short human-readable reason. On success we explicitly disconnect
        the STA again so the device stays in ap_mode until the operator
        actually saves the config -- the test is non-destructive.

        Note on radio sharing: ESP32 runs STA and AP on the same channel,
        so a successful join to a network on a different channel will
        momentarily re-tune the AP and may drop the operator's browser
        session. The response is queued before the disconnect, but if the
        browser hangs the validation result is still visible by reloading
        the config page.
        """
        if not ssid:
            return (False, "ssid is empty")
        try:
            self._sta.active(True)
        except Exception as e:
            return (False, "STA activate failed: {}".format(e))
        try:
            self._sta.disconnect()
        except Exception:
            pass
        time.sleep_ms(100)
        try:
            self._sta.connect(ssid, password)
        except OSError as e:
            return (False, "connect() raised: {}".format(e))

        deadline = time.ticks_add(time.ticks_ms(), int(timeout_ms))
        ok = False
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            try:
                if self._sta.isconnected():
                    ok = True
                    break
            except Exception:
                pass
            time.sleep_ms(100)

        try:
            self._sta.disconnect()
        except Exception:
            pass

        if ok:
            return (True, "joined '{}'".format(ssid))
        return (False, "timed out after {} ms (wrong password or out of range)"
                .format(timeout_ms))

    # --- State-machine internals ----------------------------------------

    def _apply_reload(self):
        try:
            self._load_config()
        except Exception as e:
            print("wifi_manager: reload failed:", e)
            return
        try:
            self._ap.active(False)
        except Exception as e:
            print("wifi_manager: failed to stop AP:", e)
        try:
            self._sta.disconnect()
        except Exception:
            pass
        self.info.pop("ssid", None)
        self.info.pop("ip", None)
        self.info.pop("ap_essid", None)
        self.info.pop("ap_ip", None)
        # Fresh config -> fresh verdict on whether its passwords work.
        self.had_auth_failure = False
        self.state = "booting"

    def _load_config(self):
        try:
            with open(self.config_path, "r") as f:
                cfg = json.loads(f.read())
        except Exception as e:
            print("wifi_manager: could not load", self.config_path, ":", e)
            cfg = {"known_networks": [], "access_point": dict(_DEFAULT_AP)}
        self._candidates = list(cfg.get("known_networks", []))
        self._ap_config = cfg.get("access_point", dict(_DEFAULT_AP))
        self._ap_policy = self._ap_config.get("start_policy", "never")
        self.info["cfg_networks"] = [c.get("ssid") for c in self._candidates]

    def _enter_from_boot(self, now_ms):
        try:
            self._sta.active(True)
        except Exception as e:
            print("wifi_manager: STA active failed:", e)

        self._current_idx = 0
        if self._candidates:
            self._start_attempt(self._current_idx, now_ms)
            self.state = "connecting"
        else:
            self._maybe_start_ap_or_retry(now_ms)

    def _start_attempt(self, idx, now_ms):
        c = self._candidates[idx]
        ssid = c.get("ssid", "")
        pwd = c.get("password", "")
        print("wifi_manager: attempting '{}'".format(ssid))
        try:
            self._sta.connect(ssid, pwd)
        except OSError as e:
            print("wifi_manager: connect() failed for", ssid, ":", e)
        self._attempt_started_ms = now_ms
        self.info["attempting_ssid"] = ssid

    def _poll_connecting(self, now_ms):
        try:
            if self._sta.isconnected():
                self._on_joined()
                return
        except Exception as e:
            print("wifi_manager: isconnected error:", e)

        if time.ticks_diff(now_ms, self._attempt_started_ms) < self.per_attempt_ms:
            return

        self._current_idx += 1
        if self._current_idx < len(self._candidates):
            self._start_attempt(self._current_idx, now_ms)
        else:
            self._maybe_start_ap_or_retry(now_ms)

    def _on_joined(self):
        try:
            ip = self._sta.ifconfig()[0]
        except Exception:
            ip = "?"
        ssid = self.info.get("attempting_ssid", "?")
        print("wifi_manager: connected to '{}' at {}".format(ssid, ip))
        self.info["ssid"] = ssid
        self.info["ip"] = ip
        self.had_auth_failure = False
        # Reset probe accounting so a fresh link gets a full grace window
        # before we start second-guessing it.
        self._last_probe_ms = time.ticks_ms()
        self._probe_failures = 0
        self.state = "connected"

    def _poll_connected(self, now_ms):
        """Fast path + slow active probe.

        Fast path: ``wlan.isconnected()`` every tick. Catches clean
        disconnects instantly.

        Slow path: every ``_PROBE_INTERVAL_MS`` do a short TCP connect to
        the default gateway. This is the only reliable way to catch an
        AP that went away without sending deauth frames (the mobile-
        hotspot-switched-off case). Two failures in a row trips
        ``_handle_link_loss``, which hands us back to the connect/AP
        flow. Single failures are swallowed because real networks drop
        the occasional packet and we don't want to flap on every blip.
        """
        try:
            linked = self._sta.isconnected()
        except Exception as e:
            # Don't tear down on a transient API hiccup; just skip this
            # tick and re-check next loop iteration.
            print("wifi_manager: isconnected error:", e)
            return

        if not linked:
            print("wifi_manager: link lost (isconnected False)")
            self._handle_link_loss(now_ms)
            return

        if time.ticks_diff(now_ms, self._last_probe_ms) < _PROBE_INTERVAL_MS:
            return
        self._last_probe_ms = now_ms

        if self._probe_gateway():
            self._probe_failures = 0
            return

        self._probe_failures += 1
        print("wifi_manager: probe failed ({}/{})".format(
            self._probe_failures, _PROBE_FAIL_THRESHOLD))
        if self._probe_failures >= _PROBE_FAIL_THRESHOLD:
            print("wifi_manager: link dead (probe threshold reached)")
            self._handle_link_loss(now_ms)

    def _handle_link_loss(self, now_ms):
        """Common tear-down for both isconnected-False and probe-dead.

        Drops STA association, clears per-connection info, and reroutes
        the state machine back through the connect path. If every
        configured network then fails, ``_maybe_start_ap_or_retry`` will
        bring up the soft-AP per the operator's ``start_policy`` -- which
        is how we fulfil the "revert to AP mode on wifi loss" contract
        without forcing it on brief, recoverable blips.
        """
        self.info.pop("ssid", None)
        self.info.pop("ip", None)
        self._probe_failures = 0
        try:
            self._sta.disconnect()
        except Exception:
            pass
        if self._candidates:
            self._current_idx = 0
            self._start_attempt(self._current_idx, now_ms)
            self.state = "connecting"
        else:
            self._maybe_start_ap_or_retry(now_ms)

    def _probe_gateway(self):
        """Short TCP probe to the default gateway. Returns True iff alive.

        A successful ``connect()`` obviously counts. A ``connect()`` that
        raises ECONNREFUSED / ECONNRESET also counts -- the gateway saw
        our SYN and answered with a RST, which proves the link is
        forwarding packets end-to-end; the fact that port 80 isn't
        serving anything is irrelevant. Only socket timeouts (and
        everything else) are treated as "link is dead".
        """
        try:
            ifc = self._sta.ifconfig()
        except Exception as e:
            print("wifi_manager: ifconfig failed during probe:", e)
            return False
        gw = ifc[2] if len(ifc) >= 3 else "0.0.0.0"
        if not gw or gw == "0.0.0.0":
            return False
        try:
            import socket
        except Exception:
            # No socket module means we can't probe; err on the side of
            # "link is fine" rather than tearing down unnecessarily.
            return True
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(_PROBE_TIMEOUT_S)
            s.connect((gw, 80))
            return True
        except OSError as e:
            errno = e.args[0] if e.args else 0
            # 104 = ECONNRESET, 111 = ECONNREFUSED on Linux-compatible
            # builds (MicroPython follows Linux errno numbering). Both
            # require the peer to have seen our packet, which is exactly
            # the proof-of-life we need.
            if errno in (104, 111):
                return True
            return False
        except Exception:
            # socket.timeout on some builds is a distinct exception
            # class; treat it (and anything else unexpected) as dead.
            return False
        finally:
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass

    def _maybe_start_ap_or_retry(self, now_ms):
        # We only reach this helper after a connecting cycle has been
        # exhausted, so if there were any candidates at all, at least one
        # just failed -- that's our signal for "your passwords are wrong".
        if self._candidates:
            self.had_auth_failure = True

        if self._ap_policy == "always":
            wants_ap = True
        elif self._ap_policy == "fallback":
            try:
                wants_ap = not self._sta.isconnected()
            except Exception:
                wants_ap = True
        else:
            wants_ap = False

        if wants_ap:
            self._start_ap()
            self.state = "ap_mode"
            return

        # No AP allowed. If we have candidates, loop through them again;
        # otherwise sit in "booting" so the indicator stays honest.
        if self._candidates:
            self._current_idx = 0
            self._start_attempt(self._current_idx, now_ms)
            self.state = "connecting"
        else:
            self.state = "booting"

    def _start_ap(self):
        # Work on a copy so the stored config isn't mutated by the
        # authmode auto-fill below.
        ap_cfg = dict(self._ap_config.get("config", {}))
        # An empty or missing password means "open Wi-Fi". MicroPython
        # won't infer that from the absence of a password -- it will
        # try to bring up WPA2 with a zero-length key and refuse -- so
        # we explicitly set AUTH_OPEN and drop the password field. If
        # the runtime happens to omit network.AUTH_OPEN we fall back
        # to the numeric 0, which is its value on every ESP32 port.
        pwd = ap_cfg.get("password", "")
        if not pwd:
            ap_cfg.pop("password", None)
            try:
                ap_cfg["authmode"] = network.AUTH_OPEN
            except AttributeError:
                ap_cfg["authmode"] = 0
        try:
            self._ap.active(True)
            self._ap.config(**ap_cfg)
            self.info["ap_essid"] = ap_cfg.get("essid", "")
            try:
                self.info["ap_ip"] = self._ap.ifconfig()[0]
            except Exception:
                self.info["ap_ip"] = "?"
            print("wifi_manager: AP '{}' up at {} ({})".format(
                self.info["ap_essid"], self.info["ap_ip"],
                "open" if not pwd else "secured"))
        except OSError as e:
            print("wifi_manager: AP start failed:", e)

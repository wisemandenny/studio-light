"""Pure-function LED indicator for the Wi-Fi state machine.

One method matters: ``render(state, now_ms)``. Each call is a pure function
of the current state and the current time, so the caller can drive it at
whatever cadence its main loop happens to run at; there is no internal
timer, no task, no cooperative scheduler.

State -> visual map:

  * ``booting``               -- slow white pulse (1s period)
  * ``connecting``            -- blue breathe (1.6s period)
  * ``ap_mode``               -- AI-driven 8x8 snake game: a green snake
                                 follows a Hamiltonian cycle (with BFS
                                 shortcuts to the food while short) until
                                 it either fills the board (brief white
                                 "win" flash, then resets) or traps itself
                                 (brief dim-red hold, then resets). Far
                                 less grating than the old amber flash,
                                 and still unmistakably "I need config".
  * ``ap_mode_auth_failed``   -- amber-amber-red warning flash (~1s loop)
                                 used when the operator is actively using
                                 the config page but every known password
                                 has failed -- the bright red pip is a
                                 clue to double-check what was typed.
  * ``connected``             -- Pokeball "capture complete" animation: a
                                 circular red/white ball with a black band
                                 fades in, does the classic three-wobble
                                 shake, then the center button flashes
                                 yellow-white -- the visual rhyme is
                                 "you caught it!" for a successful join.
                                 Held by the caller for a few seconds
                                 before handing off to light_off/light_on.
  * ``light_off``             -- off (resting; waiting for Ableton)
  * ``light_on``              -- solid ``light_on_color`` (defaults to red;
                                 overwritten per-datagram by the Ableton
                                 plugin via ``LightController.light_color``
                                 so the user can "pick" a colour inside
                                 Ableton by changing their master track).
  * anything else             -- off

``pixel_order`` defaults to ``GRB`` to match common WS2812/WS2812B strips.
If your strip is RGB-ordered, pass ``pixel_order="RGB"``.

For 8x8 matrix layouts we assume a simple row-major wiring (index = y*8+x,
origin top-left). Many matrices are serpentine; if yours is, pass
``serpentine=True`` and every odd row will be reversed on write. This is
purely a display-side concern -- the animation math runs in matrix
coordinates.

Operator-triggered ripples:
  ``start_ripple(now_ms)`` schedules a vivid concentric-ring animation
  that expands from the centre of the matrix over ``_RIPPLE_DURATION_MS``.
  Unlike the state renders, ripples are *stateful*: up to
  ``_MAX_CONCURRENT_RIPPLES`` can be live at once, each with its own
  start time and random hue. While any ripple is live, the next
  ``render()`` call draws their combined frame instead of the state
  animation -- it's a transient override, not a new state. The ripple
  animation is completely non-blocking; it advances one frame per
  ``render()`` call just like every other animation.
"""
import time


_RIPPLE_DURATION_MS = 600
_MAX_CONCURRENT_RIPPLES = 3
_RIPPLE_WAVE_WIDTH = 1.2   # half-width of the bright ring, in pixels

# --- Per-channel gamma + gain (colour correction) ------------------------
# WS2812 pixels drive linear PWM, but neither the LEDs nor the human eye
# are linear. Two effects compound:
#
#   1. sRGB gamma: perceived brightness roughly tracks value^2.2, so a
#      PWM of 128 looks a lot brighter than half of 255.
#   2. Channel imbalance: the green die in a WS2812B is physically
#      brighter than the red die at equal PWM (~2x), so an equal-PWM
#      mix of R and G reads as greenish-yellow, not yellow. A mild blue
#      imbalance is also present but we haven't seen it matter yet.
#
# These coefficients were calibrated empirically against a real strip:
# a raw PWM sweep (R=40 constant, G stepping 0..40) was shown to the
# operator, and the G values that read as "pure orange" and "pure
# yellow" were fit with a power curve:
#
#   * Green: gamma 1.75 and gain 0.50 -- so (255, 255, 0) -> PWM (40, 20)
#     ("H" swatch, read as yellow) and (255, 128, 0) -> PWM (40, 6)
#     ("D" swatch, read as orange).
#   * Red and Blue: standard sRGB gamma 2.2, unit gain. The operator
#     rated these as already correct in the initial test, so they're
#     left alone; the only regression is that "pure white" (255,255,255)
#     takes a slight warm tint, which is acceptable because nobody
#     picks white as a recording colour.
#
# The LUTs are applied as the *first* step inside ``_scale``, so the
# subsequent brightness multiply happens in linear light space and
# perceptual colour ratios survive it. Animations that write pixels
# directly (trippy rainbow, ripples, snake) deliberately bypass this
# path -- they were tuned linear and still look correct.
_R_GAMMA, _R_GAIN = 2.2, 1.0
_G_GAMMA, _G_GAIN = 1.75, 0.50
_B_GAMMA, _B_GAIN = 2.2, 1.0


def _build_channel_lut(gamma, gain):
    return bytes(
        max(0, min(255, int(((i / 255.0) ** gamma) * gain * 255 + 0.5)))
        for i in range(256)
    )


_GAMMA_LUT_R = _build_channel_lut(_R_GAMMA, _R_GAIN)
_GAMMA_LUT_G = _build_channel_lut(_G_GAMMA, _G_GAIN)
_GAMMA_LUT_B = _build_channel_lut(_B_GAMMA, _B_GAIN)

# --- Pokeball animation (connected) ---------------------------------------
# Canonical capture cadence from the anime: wobble-wobble-wobble ... pause
# ... wobble-wobble-wobble ... pause ... wobble-wobble-wobble ... CLICK!
# So each "shake" is a short burst of three decaying oscillations, then a
# dead-still pause, and the ball rotates as a rigid body rather than just
# flexing its band. Tuned so one full cycle fits comfortably inside
# main.py's _CONNECT_CELEBRATION_MS (4000ms); leftover time repeats the
# cycle, which reads fine because each episode is seamless.
_POKEBALL_FADE_IN_MS = 250
_POKEBALL_SHAKE_WOBBLE_MS = 750    # active shaking inside the episode
_POKEBALL_SHAKE_PAUSE_MS = 500     # dead-still settle after each burst
_POKEBALL_SHAKE_EPISODE_MS = _POKEBALL_SHAKE_WOBBLE_MS + _POKEBALL_SHAKE_PAUSE_MS
_POKEBALL_SHAKE_OSCILLATIONS = 4   # mini-wobbles inside one burst
_POKEBALL_SHAKES = 3
# Extra breath between the last wobble and the capture-flash. Gives the
# "is it going to stay shut?" beat from the anime before the sparkle.
_POKEBALL_PRE_FLASH_MS = 450
_POKEBALL_BUTTON_FLASH_MS = 600
# Peak tilt of the rigid-body rotation, in radians (~20 deg).
_POKEBALL_SHAKE_TILT = 0.35
# Lateral translation (in matrix pixels) applied in the direction of the
# tilt. Exaggerates the wobble so the whole ball clearly shifts left/
# right, not just rotates in place.
_POKEBALL_SHAKE_SHIFT = 0.75
# Ball geometry: radius 3.0 gives a clean 6x6 silhouette centred on the
# matrix with a 1-pixel cardinal margin, leaving room for the shoulder
# sparkle pixels along the diagonals.
_POKEBALL_CX = 3.5
_POKEBALL_CY = 3.5
_POKEBALL_R = 3.0
_POKEBALL_R2 = _POKEBALL_R * _POKEBALL_R
_POKEBALL_BAND_HALF = 0.55         # half-thickness of the black band (px)
_POKEBALL_BUTTON_R = 0.9           # radius of the white center button (px)
_POKEBALL_BUTTON_R2 = _POKEBALL_BUTTON_R * _POKEBALL_BUTTON_R
# Sparkle: only the pixels just outside the 6x6 silhouette along the
# diagonals (dist^2 in (9, 12.5]) -- eight pixels max. Pixels inside the
# ball stay normal on flash peaks; only the button lifts to yellow.
_POKEBALL_SPARKLE_R2_MAX = 12.5    # covers the 8 diagonal shoulder pixels

# --- Snake animation (ap_mode) --------------------------------------------
# One grid step every this many ms. 140ms reads as "lively but legible" on
# an 8x8 matrix; much faster and the gradient blurs, much slower and it
# feels like a screensaver crashed.
_SNAKE_STEP_MS = 140
_SNAKE_INITIAL_LEN = 3
# Held briefly on special outcomes so the operator registers them before
# we reset the game.
_SNAKE_WIN_HOLD_MS = 800      # full-white flash when the snake fills 8x8
_SNAKE_TRAP_HOLD_MS = 500     # dim-red hold when the AI paints itself in


class StatusIndicator:
    def __init__(self, pin, num_pixels, pixel_order="GRB", max_brightness=40,
                 width=8, height=8, serpentine=False):
        self.pin = pin
        self.num_pixels = num_pixels
        self.pixel_order = pixel_order
        self.max_brightness = max(0, min(255, int(max_brightness)))
        self.width = width
        self.height = height
        self.serpentine = serpentine

        self._np = None
        # For solid-color frames we dedupe writes with _last_solid; for
        # per-pixel animations we always write and clear _last_solid so the
        # next solid frame forces a refresh.
        self._last_solid = None

        # Active operator-triggered ripples. Each entry is
        # (start_ms, (r, g, b)) in the 0..255 display range. See
        # start_ripple() and _render_ripples().
        self._ripples = []

        # Colour used for the ``light_on`` state. The caller (main loop)
        # overwrites this from ``LightController.light_color`` each tick
        # so Ableton-provided colours take effect immediately; the red
        # default covers the brief window before any datagram arrives.
        self.light_on_color = (255, 0, 0)

        # Snake animation state (lazy-initialised on first ap_mode render
        # so we pay nothing for the Hamiltonian path + index dict when
        # the device joins Wi-Fi directly and never enters ap_mode).
        self._snake_path = None          # list[(x, y)] cycle
        self._snake_cell_to_idx = None   # {(x, y): int} reverse lookup
        self._snake_body = None          # list[(x, y)] head first
        self._snake_body_set = None      # set of body cells for O(1) hits
        self._snake_food = None          # (x, y)
        self._snake_last_step_ms = 0
        self._snake_won_at_ms = None
        self._snake_trap_at_ms = None

        # Pre-compute the matrix centre and max radius once. Both are
        # pure geometry, so there's no reason to redo them in every
        # ripple frame.
        self._cx = (self.width - 1) / 2.0
        self._cy = (self.height - 1) / 2.0
        self._max_radius = ((self._cx * self._cx + self._cy * self._cy)
                            ** 0.5) + _RIPPLE_WAVE_WIDTH + 0.5

        try:
            import machine
            import neopixel
            self._np = neopixel.NeoPixel(machine.Pin(pin), num_pixels)
            self._write_solid((0, 0, 0))
        except Exception as e:
            print("status_indicator: neopixel init failed:", e)

    # --- Operator-triggered ripple --------------------------------------

    def start_ripple(self, now_ms):
        """Schedule a concentric-ring ripple starting at ``now_ms``.

        Non-blocking: this just records a (start_ms, color) entry; the
        ripple is drawn frame-by-frame inside render() until it expires.
        Repeated calls stack -- up to ``_MAX_CONCURRENT_RIPPLES`` can be
        live at once, and a fresh random hue is drawn per call, so
        mashing the BOOT button looks like dropping several differently
        coloured stones in a pond (not a restarting-from-center
        flicker).

        When the cap is reached we drop the oldest ripple, which in
        practice is nearly finished and contributing little light
        anyway.
        """
        try:
            import random as _random
        except Exception:
            return
        # Fresh vivid hue per call: full saturation floor so we never
        # get pastels, full value so the ring is bright.
        hue = _random.random()
        sat = 0.85 + _random.random() * 0.15
        base = _hsv_to_rgb(hue, sat, 1.0)

        if len(self._ripples) >= _MAX_CONCURRENT_RIPPLES:
            self._ripples.pop(0)
        self._ripples.append((now_ms, base))

    def _ripples_active(self, now_ms):
        """Prune expired ripples in-place and return True if any remain."""
        if not self._ripples:
            return False
        keep = []
        for start_ms, color in self._ripples:
            if time.ticks_diff(now_ms, start_ms) < _RIPPLE_DURATION_MS:
                keep.append((start_ms, color))
        self._ripples = keep
        return bool(keep)

    def _render_ripples(self, now_ms):
        """Draw the current combined-ripple frame.

        Each live ripple contributes a bell-shaped intensity profile
        around a radius that grows linearly from 0 to just past the
        matrix corner. A per-ripple fade concentrates energy near the
        centre-out origin so the ring visibly weakens as it expands --
        the "ripple on a lake" visual cue. Contributions sum in RGB
        and are then clamped to 255.
        """
        if self._np is None:
            return
        self._last_solid = None
        cx = self._cx
        cy = self._cy
        max_radius = self._max_radius
        wave_width = _RIPPLE_WAVE_WIDTH
        duration = _RIPPLE_DURATION_MS
        ripples = self._ripples
        # Pre-compute each live ripple's current wavefront and fade
        # coefficient once per frame rather than per pixel.
        wave_cache = []
        for start_ms, color in ripples:
            elapsed = time.ticks_diff(now_ms, start_ms)
            if elapsed < 0:
                elapsed = 0
            progress = elapsed / duration
            if progress > 1.0:
                progress = 1.0
            # Wave travels from r=0 outward; fade accelerates toward the
            # end so the outer ring dims rather than hard-cuts.
            front = progress * max_radius
            fade = (1.0 - progress) ** 1.3
            wave_cache.append((front, fade, color))

        for y in range(self.height):
            for x in range(self.width):
                dx = x - cx
                dy = y - cy
                dist = (dx * dx + dy * dy) ** 0.5
                rs = 0.0
                gs = 0.0
                bs = 0.0
                for front, fade, color in wave_cache:
                    delta = dist - front
                    if delta < 0:
                        delta = -delta
                    bell = 1.0 - delta / wave_width
                    if bell <= 0.0:
                        continue
                    intensity = bell * fade
                    rs += color[0] * intensity
                    gs += color[1] * intensity
                    bs += color[2] * intensity
                r = 255 if rs > 255 else int(rs)
                g = 255 if gs > 255 else int(gs)
                b = 255 if bs > 255 else int(bs)
                self._np[self._xy_to_index(x, y)] = self._reorder(r, g, b)
        try:
            self._np.write()
        except Exception as e:
            print("status_indicator: write failed:", e)

    # --- Render dispatch ------------------------------------------------

    def render(self, state, now_ms):
        # Ripples take precedence over whatever the state would draw.
        # They're deliberately transient (max _RIPPLE_DURATION_MS each),
        # so this override is brief by construction.
        if self._ripples_active(now_ms):
            self._render_ripples(now_ms)
            return

        if state == "booting":
            self._render_solid(self._pulse((255, 255, 255), now_ms, 1000, 0.10))
        elif state == "connecting":
            self._render_solid(self._pulse((0, 60, 255), now_ms, 1600, 0.15))
        elif state == "ap_mode":
            self._render_snake(now_ms)
        elif state == "ap_mode_auth_failed":
            self._render_solid(self._auth_failed_color(now_ms))
        elif state == "connected":
            self._render_pokeball(now_ms)
        elif state == "light_on":
            self._render_solid(self._scale(self.light_on_color, 1.0))
        elif state == "light_off":
            self._render_solid((0, 0, 0))
        else:
            self._render_solid((0, 0, 0))

    # --- solid vs per-pixel write paths ---------------------------------

    def _render_solid(self, rgb):
        if rgb != self._last_solid:
            self._write_solid(rgb)
            self._last_solid = rgb

    def _write_solid(self, rgb):
        if self._np is None:
            return
        ordered = self._reorder(*rgb)
        for i in range(self.num_pixels):
            self._np[i] = ordered
        try:
            self._np.write()
        except Exception as e:
            print("status_indicator: write failed:", e)

    def _render_perpixel(self, rgb_at):
        """Call ``rgb_at(x, y)`` for every pixel and write the result.

        ``rgb_at`` returns an (r, g, b) tuple already in the 0..255 display
        range (we apply max_brightness scaling and channel reorder here).
        """
        if self._np is None:
            return
        self._last_solid = None
        k = self.max_brightness / 255.0
        w = self.width
        for y in range(self.height):
            for x in range(w):
                r, g, b = rgb_at(x, y)
                r = int(r * k)
                g = int(g * k)
                b = int(b * k)
                self._np[self._xy_to_index(x, y)] = self._reorder(r, g, b)
        try:
            self._np.write()
        except Exception as e:
            print("status_indicator: write failed:", e)

    def _xy_to_index(self, x, y):
        if self.serpentine and (y & 1):
            x = self.width - 1 - x
        return y * self.width + x

    # --- frame generators -----------------------------------------------

    def _pulse(self, base_rgb, now_ms, period_ms, floor=0.0):
        """Triangle-wave brightness in [floor, 1.0] over ``period_ms``.

        Triangle rather than sine because MicroPython's math import isn't
        guaranteed on all builds and the visual difference is imperceptible
        at breathe rates.
        """
        phase = (now_ms % period_ms) / period_ms
        tri = 1.0 - abs(phase * 2.0 - 1.0)
        level = floor + (1.0 - floor) * tri
        return self._scale(base_rgb, level)

    def _auth_failed_color(self, now_ms):
        """1s loop: amber flash, amber flash, red flash.

        Each of the three 333ms phases is bright for the first ~150ms then
        dark for the remainder, so the pattern reads as three distinct
        pips and the red one stands out clearly.
        """
        phase_ms = now_ms % 1000
        if phase_ms < 333:
            slot_start = 0
            color = (255, 140, 0)
        elif phase_ms < 666:
            slot_start = 333
            color = (255, 140, 0)
        else:
            slot_start = 666
            color = (255, 0, 0)
        on = (phase_ms - slot_start) < 150
        return self._scale(color, 1.0) if on else (0, 0, 0)

    def _render_pokeball(self, now_ms):
        """Pokeball "capture complete" animation for the connected state.

        Cycle (loops if the state outlives it):

          1. Fade-in (``_POKEBALL_FADE_IN_MS``): ball ramps from black.
          2. Shake x ``_POKEBALL_SHAKES`` (``_POKEBALL_SHAKE_EPISODE_MS``
             each): burst of ``_POKEBALL_SHAKE_OSCILLATIONS`` decaying
             mini-wobbles followed by a dead-still pause, matching the
             anime's "wobble-wobble-wobble ... pause" cadence.
          3. Capture flash (``_POKEBALL_BUTTON_FLASH_MS``): the ball
             interior whitens toward a warm yellow-white on two quick
             pulses, and a halo of sparkle pixels lights up just outside
             the ball silhouette so the "click!" beat is clearly visible.

        The ball tilts as a rigid body (inverse-rotate each sample into
        ball-local coordinates), not by shearing the band, so the motion
        reads as the whole ball rocking rather than the ball deforming.
        The band and button geometry stay static in the ball frame, which
        is how rocking actually looks.
        """
        ball_cx = _POKEBALL_CX
        ball_cy = _POKEBALL_CY
        ball_r2 = _POKEBALL_R2
        band_half = _POKEBALL_BAND_HALF
        button_r2 = _POKEBALL_BUTTON_R2
        sparkle_r2_max = _POKEBALL_SPARKLE_R2_MAX

        shakes_total_ms = _POKEBALL_SHAKE_EPISODE_MS * _POKEBALL_SHAKES
        cycle_total = (_POKEBALL_FADE_IN_MS
                       + shakes_total_ms
                       + _POKEBALL_PRE_FLASH_MS
                       + _POKEBALL_BUTTON_FLASH_MS)
        phase_ms = now_ms % cycle_total

        shake_end = _POKEBALL_FADE_IN_MS + shakes_total_ms
        flash_start = shake_end + _POKEBALL_PRE_FLASH_MS
        if phase_ms < _POKEBALL_FADE_IN_MS:
            level = phase_ms / float(_POKEBALL_FADE_IN_MS)
            tilt_theta = 0.0
            flash = 0.0
        elif phase_ms < shake_end:
            level = 1.0
            # Position within current shake episode.
            ep_phase = (phase_ms - _POKEBALL_FADE_IN_MS) % _POKEBALL_SHAKE_EPISODE_MS
            if ep_phase < _POKEBALL_SHAKE_WOBBLE_MS:
                # Wobble burst: decaying triangle wave. Envelope eases
                # out (sqrt shape) so the first hit is crisp and the
                # tail is a gentler jitter, which reads more naturally
                # than a linear decay.
                wob_t = ep_phase / float(_POKEBALL_SHAKE_WOBBLE_MS)
                envelope = (1.0 - wob_t)
                envelope = envelope * envelope ** 0.5 if envelope > 0 else 0.0
                osc_phase = (wob_t * _POKEBALL_SHAKE_OSCILLATIONS) - int(
                    wob_t * _POKEBALL_SHAKE_OSCILLATIONS)
                tilt_theta = (_POKEBALL_SHAKE_TILT
                              * envelope
                              * _tri_bipolar(osc_phase))
            else:
                # Pause between bursts -- ball dead still and centred.
                tilt_theta = 0.0
            flash = 0.0
        elif phase_ms < flash_start:
            # Pre-flash settle: ball centred, "is it going to stay?" beat
            # before the capture sparkle.
            level = 1.0
            tilt_theta = 0.0
            flash = 0.0
        else:
            level = 1.0
            tilt_theta = 0.0
            local = (phase_ms - flash_start) / float(_POKEBALL_BUTTON_FLASH_MS)
            # Two quick pulses; peaks at local = 0.25 and 0.75.
            bi = local * 2.0
            bi_frac = bi - int(bi)
            flash = 1.0 - abs(bi_frac * 2.0 - 1.0)

        # Cheap Taylor-series trig (math module not guaranteed on MPY).
        # Accurate to ~4 decimals for |theta| <= 0.5.
        t2 = tilt_theta * tilt_theta
        cos_t = 1.0 - t2 * 0.5 + t2 * t2 * (1.0 / 24.0)
        sin_t = tilt_theta - tilt_theta * t2 * (1.0 / 6.0)

        # Lateral translation in the direction of the tilt -- slides the
        # whole ball left/right in sync with the rock, exaggerating the
        # wobble so it reads as "the ball is about to tip" rather than
        # just rotating about a fixed centre. tilt_theta is positive for
        # right-tilt, so positive shift moves the ball visually rightward.
        cx_shift = tilt_theta * (_POKEBALL_SHAKE_SHIFT / _POKEBALL_SHAKE_TILT)
        ball_cx_now = ball_cx + cx_shift

        # Palette, scaled by fade-in level.
        lvl255 = int(255 * level)
        lvl235 = int(235 * level)
        lvl190 = int(190 * level)
        red_top = (lvl255, 0, 0)
        white_bot = (lvl235, lvl235, lvl235)
        band = (0, 0, 0)
        # On flash peaks the button lifts to bright yellow-white; when
        # not flashing it's the same off-white as the lower half but
        # tinged warmer so it reads as a distinct element.
        if flash > 0.0:
            br = int((235 + 20 * flash) * level)
            bg = int((235 + 20 * flash) * level)
            bb = int((190 - 110 * flash) * level)
            button_base = (br if br <= 255 else 255,
                           bg if bg <= 255 else 255,
                           bb if bb >= 0 else 0)
        else:
            button_base = (lvl235, lvl235, lvl190)

        # Sparkle shoulder pixels: dim warm yellow so they read as
        # "little sparks" rather than a blinding halo.
        sparkle_on = flash > 0.0
        if sparkle_on:
            sr = int(200 * flash * level)
            sg = int(200 * flash * level)
            sb = int(80 * flash * level)
            sparkle_rgb = (sr, sg, sb)
        else:
            sparkle_rgb = (0, 0, 0)

        lut_r = _GAMMA_LUT_R
        lut_g = _GAMMA_LUT_G
        lut_b = _GAMMA_LUT_B

        def rgb_at(x, y):
            dx = x - ball_cx_now
            dy = y - ball_cy
            dist2 = dx * dx + dy * dy
            if dist2 > ball_r2:
                # Sparkle only on the eight cardinal-aligned "shoulder"
                # pixels (top/bottom middle two and left/right middle
                # two), which are the pixels directly outside the 6x6
                # ball on the horizontal and vertical axes. Diagonal
                # corner pixels stay dark so the sparkle reads as four
                # discrete "pings" of light rather than a halo.
                if (sparkle_on
                        and dist2 <= sparkle_r2_max
                        and (abs(x - ball_cx) < 1.0
                             or abs(y - ball_cy) < 1.0)):
                    r, g, b = sparkle_rgb
                    return (lut_r[r], lut_g[g], lut_b[b])
                return (0, 0, 0)

            # Inside silhouette: inverse-rotate the sample into the
            # ball's local frame so the whole body appears to rock.
            lx = dx * cos_t + dy * sin_t
            ly = -dx * sin_t + dy * cos_t

            if (lx * lx + ly * ly) <= button_r2:
                base = button_base
            elif abs(ly) <= band_half:
                base = band
            elif ly < 0:
                base = red_top
            else:
                base = white_bot
            return (lut_r[base[0]], lut_g[base[1]], lut_b[base[2]])

        self._render_perpixel(rgb_at)

    # --- Snake animation (ap_mode) --------------------------------------
    #
    # The AI is a short-path BFS that also knows about a pre-computed
    # Hamiltonian cycle over the full grid, used as a safe fallback when
    # BFS can't find food (because the body is in the way).
    #
    # Why Hamiltonian-as-fallback rather than pure Hamiltonian? Pure
    # Hamiltonian is provably safe (and will fill the board eventually)
    # but visually boring for the first N foods, because the snake just
    # traces the same spiral every time. BFS picks the actual shortest
    # path and produces varied, obviously-intelligent movement for the
    # dynamic phase; falling back to the cycle when cornered keeps us
    # from trapping ourselves unnecessarily on short/medium bodies.
    #
    # When the BFS does fail AND there's no safe neighbour to retreat
    # to, we accept the trap, hold a dim red frame for a moment, then
    # reset. That's both honest ("the AI hit its limit") and a visual
    # cue that the animation is alive rather than stalled.

    def _render_snake(self, now_ms):
        if self._snake_path is None:
            self._snake_init(now_ms)

        # Terminal-state holds: render the "win" or "trap" frame until
        # the hold expires, then reset. Intentionally checked before
        # stepping so we don't advance the simulation during the hold.
        if self._snake_won_at_ms is not None:
            if time.ticks_diff(now_ms, self._snake_won_at_ms) < _SNAKE_WIN_HOLD_MS:
                self._render_solid(self._scale((255, 255, 255), 1.0))
                return
            self._snake_reset(now_ms)
        if self._snake_trap_at_ms is not None:
            if time.ticks_diff(now_ms, self._snake_trap_at_ms) < _SNAKE_TRAP_HOLD_MS:
                self._render_solid(self._scale((140, 0, 0), 1.0))
                return
            self._snake_reset(now_ms)

        # Advance as many steps as have elapsed since the last step,
        # with a small per-frame cap so a long pause upstream (e.g. the
        # wifi probe briefly blocking the loop) doesn't burst-step the
        # snake and make it look teleporty.
        elapsed = time.ticks_diff(now_ms, self._snake_last_step_ms)
        if elapsed > _SNAKE_STEP_MS * 4:
            self._snake_last_step_ms = now_ms
            elapsed = 0
        steps = 0
        while elapsed >= _SNAKE_STEP_MS and steps < 3:
            self._snake_step(now_ms)
            self._snake_last_step_ms = time.ticks_add(
                self._snake_last_step_ms, _SNAKE_STEP_MS)
            elapsed -= _SNAKE_STEP_MS
            steps += 1
            if (self._snake_won_at_ms is not None
                    or self._snake_trap_at_ms is not None):
                # Re-enter terminal-state path on the next render() call.
                return

        # Draw current frame.
        body = self._snake_body
        food = self._snake_food
        body_len = len(body)
        idx_of_cell = {}
        for i, cell in enumerate(body):
            idx_of_cell[cell] = i

        # Food pulses so it reads as "target" rather than "another body
        # cell that happens to be red somewhere" -- useful at low
        # max_brightness where the colour separation is subtle.
        phase = (now_ms % 500) / 500.0
        tri = 1.0 - abs(phase * 2.0 - 1.0)
        food_level = 0.55 + 0.45 * tri

        def rgb_at(x, y):
            p = (x, y)
            if p == food:
                return (int(255 * food_level), 0, 0)
            i = idx_of_cell.get(p)
            if i is not None:
                if i == 0:
                    return (0, 255, 120)
                # Body fades from bright green at the neck down to a
                # quarter-brightness tail, so the direction of motion
                # is visible without an arrow.
                t = i / max(1, body_len - 1)
                level = 1.0 - t * 0.75
                return (0, int(200 * level), 0)
            return (0, 0, 0)

        self._render_perpixel(rgb_at)

    def _snake_init(self, now_ms):
        self._snake_path = _build_hamiltonian_path(self.width, self.height)
        self._snake_cell_to_idx = {}
        for i, cell in enumerate(self._snake_path):
            self._snake_cell_to_idx[cell] = i
        self._snake_reset(now_ms)

    def _snake_reset(self, now_ms):
        # Seed the body along the first few cells of the Hamiltonian
        # cycle so its starting shape is guaranteed valid (and lines up
        # with the cycle's direction, which makes early shortcuts
        # predictable). Head is body[0].
        init_len = min(_SNAKE_INITIAL_LEN, self.width * self.height - 1)
        self._snake_body = [
            self._snake_path[init_len - 1 - k] for k in range(init_len)
        ]
        self._snake_body_set = set(self._snake_body)
        self._snake_food = self._snake_spawn_food()
        self._snake_last_step_ms = now_ms
        self._snake_won_at_ms = None
        self._snake_trap_at_ms = None

    def _snake_spawn_food(self):
        w = self.width
        h = self.height
        total = w * h
        body_set = self._snake_body_set
        if len(body_set) >= total:
            return self._snake_body[-1]
        try:
            import random as _random
        except Exception:
            _random = None
        if _random is not None:
            # Random draws are bounded to avoid pathological looping on
            # very full boards; if we can't find a cell quickly we fall
            # through to a deterministic scan.
            for _ in range(200):
                x = _random.randint(0, w - 1)
                y = _random.randint(0, h - 1)
                if (x, y) not in body_set:
                    return (x, y)
        for y in range(h):
            for x in range(w):
                if (x, y) not in body_set:
                    return (x, y)
        return (0, 0)

    def _snake_step(self, now_ms):
        next_pos = self._snake_next_move()
        if next_pos is None:
            # Fully trapped -- no safe neighbour. Hold a frame, reset.
            self._snake_trap_at_ms = now_ms
            return
        body = self._snake_body
        body_set = self._snake_body_set
        body.insert(0, next_pos)
        body_set.add(next_pos)
        total = self.width * self.height
        if next_pos == self._snake_food:
            if len(body) >= total:
                self._snake_won_at_ms = now_ms
                return
            self._snake_food = self._snake_spawn_food()
        else:
            tail = body.pop()
            body_set.discard(tail)

    def _snake_next_move(self):
        """Pick the next cell for the head.

        Strategy:
          1. BFS from head to food treating body (minus the tail, which
             is about to vacate) as obstacles. If a path exists, return
             its first step -- this is the shortest legal route.
          2. Otherwise, step along the Hamiltonian cycle if that cell is
             clear. The cycle is guaranteed to visit every cell exactly
             once, so when the body is shorter than the cycle it is
             always a safe long-term plan.
          3. Otherwise, take any safe neighbour. This is the "look
             scattered, buy time" fallback for corner cases where the
             cycle's next cell is covered by body we created via past
             shortcuts.
          4. Otherwise return None -- the caller treats this as "trapped"
             and will reset.
        """
        body = self._snake_body
        head = body[0]
        # Tail will move on a non-eating step; treating it as passable
        # unlocks tight hallway chases without adding real risk.
        if len(body) > 1:
            obstacles = set(body)
            obstacles.discard(body[-1])
        else:
            obstacles = set()
        target = self._snake_food
        w = self.width
        h = self.height

        # --- 1: BFS to food -----------------------------------------
        parent = {head: None}
        queue = [head]
        qi = 0
        found = False
        while qi < len(queue):
            cur = queue[qi]
            qi += 1
            if cur == target:
                found = True
                break
            cx, cy = cur
            for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
                nx = cx + dx
                ny = cy + dy
                if nx < 0 or nx >= w or ny < 0 or ny >= h:
                    continue
                n = (nx, ny)
                if n in parent:
                    continue
                if n in obstacles:
                    continue
                parent[n] = cur
                queue.append(n)
        if found:
            cur = target
            while parent[cur] != head:
                cur = parent[cur]
            return cur

        # --- 2: Hamiltonian cycle fallback ---------------------------
        head_idx = self._snake_cell_to_idx.get(head)
        if head_idx is not None:
            total = w * h
            cycle_next = self._snake_path[(head_idx + 1) % total]
            if cycle_next not in obstacles:
                return cycle_next

        # --- 3: any safe neighbour -----------------------------------
        hx, hy = head
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            nx = hx + dx
            ny = hy + dy
            if nx < 0 or nx >= w or ny < 0 or ny >= h:
                continue
            n = (nx, ny)
            if n in obstacles:
                continue
            return n

        # --- 4: genuinely trapped ------------------------------------
        return None

    # --- primitive helpers ----------------------------------------------

    def _scale(self, rgb, level):
        r, g, b = rgb
        # Gamma-encode per-channel first (sRGB -> linear PWM, with green
        # attenuated to compensate for WS2812B's brighter green die).
        # The subsequent brightness multiply then happens in linear
        # light space. See the _GAMMA_LUT_* block for calibration
        # details.
        r = _GAMMA_LUT_R[int(r) & 0xFF]
        g = _GAMMA_LUT_G[int(g) & 0xFF]
        b = _GAMMA_LUT_B[int(b) & 0xFF]
        k = max(0.0, min(1.0, level)) * (self.max_brightness / 255.0)
        return (int(r * k), int(g * k), int(b * k))

    def _reorder(self, r, g, b):
        channels = {"R": int(r), "G": int(g), "B": int(b)}
        return (channels[self.pixel_order[0]],
                channels[self.pixel_order[1]],
                channels[self.pixel_order[2]])


def _tri_bipolar(phase01):
    """Triangle wave with period 1 and range [-1, +1].

    Shape on [0, 1]:  0 -> +1 at 0.25 -> 0 at 0.5 -> -1 at 0.75 -> 0.
    Used by the Pokeball shake so a tilt "0 -> right -> 0 -> left -> 0"
    fits naturally in one cycle, no trig needed.
    """
    if phase01 < 0.25:
        return phase01 * 4.0
    if phase01 < 0.5:
        return (0.5 - phase01) * 4.0
    if phase01 < 0.75:
        return -(phase01 - 0.5) * 4.0
    return -(1.0 - phase01) * 4.0


def _build_hamiltonian_path(w, h):
    """Return a Hamiltonian cycle on a ``w x h`` grid.

    The returned list has length ``w * h`` and is ordered so that every
    consecutive pair of cells is orthogonally adjacent, and the first
    and last cells are also adjacent (closing the cycle).

    Construction (requires at least one of ``w``, ``h`` to be even; we
    assert on that rather than silently producing a broken path):

        * descend column 0 top-to-bottom;
        * serpentine through columns 1..w-1, skipping row 0 on each,
          alternating down/up so each column's start is adjacent to
          the previous column's end;
        * walk row 0 from right to left back to (0, 0) to close.

    For a 2x2 grid this still works -- the serpentine and return rows
    degenerate cleanly -- so the Snake animation is well-defined on
    any matrix the rest of the code supports.
    """
    assert w >= 2 and h >= 2, "hamiltonian cycle needs w,h >= 2"
    assert (w % 2 == 0) or (h % 2 == 0), \
        "hamiltonian cycle needs at least one even dimension"
    path = []
    for y in range(h):
        path.append((0, y))
    for x in range(1, w):
        if x % 2 == 1:
            for y in range(h - 1, 0, -1):
                path.append((x, y))
        else:
            for y in range(1, h):
                path.append((x, y))
    for x in range(w - 1, 0, -1):
        path.append((x, 0))
    return path


def _hsv_to_rgb(h, s, v):
    """HSV -> RGB with h,s,v in [0,1]; returns ints in [0,255].

    Standard 6-sector piecewise implementation; no math module required.
    """
    if s <= 0.0:
        c = int(v * 255)
        return (c, c, c)
    h6 = (h - int(h)) * 6.0
    sector = int(h6)
    f = h6 - sector
    p = v * (1.0 - s)
    q = v * (1.0 - s * f)
    t = v * (1.0 - s * (1.0 - f))
    if sector == 0:
        r, g, b = v, t, p
    elif sector == 1:
        r, g, b = q, v, p
    elif sector == 2:
        r, g, b = p, v, t
    elif sector == 3:
        r, g, b = p, q, v
    elif sector == 4:
        r, g, b = t, p, v
    else:
        r, g, b = v, p, q
    return (int(r * 255), int(g * 255), int(b * 255))

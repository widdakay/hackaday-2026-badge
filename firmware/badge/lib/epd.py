"""E-Paper Display Driver for Billboard Badge

Ported from Wiring in https://github.com/PervasiveDisplays/EPD_Driver_GU_large/blob/e408154652802a82e520cc25ba04adeb347d0528/src/EPD_Driver.cpp

--COPYRIGHT--
Brief The drivers and functions of development board
Copyright (c) 2012-2021 Pervasive Displays Inc. All rights reserved.
 Authors: Pervasive Displays Inc.
 Redistribution and use in source and binary forms, with or without
 modification, are permitted provided that the following conditions
 are met:
 1. Redistributions of source code must retain the above copyright
    notice, this list of conditions and the following disclaimer.
 2. Redistributions in binary form must reproduce the above copyright
    notice, this list of conditions and the following disclaimer in
    the documentation and/or other materials provided with the
    distribution.
 THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
 "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
 LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
 A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
 OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
 SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
 LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
 DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
 THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
 (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
 OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""

import asyncio as aio
import btree
import framebuf
import gc
from machine import Pin, SPI, SoftSPI
import micropython
from micropython import const
import time

import lvgl

from lib.large_png import show_png

EPD_CS1_PIN = Pin(13, mode=Pin.OUT)
EPD_CS2_PIN = Pin(3, mode=Pin.OUT)
EPD_BUSY1_PIN = Pin(12, mode=Pin.IN)
EPD_BUSY2_PIN = Pin(8, mode=Pin.IN)
EPD_DC_PIN = Pin(10, mode=Pin.OUT)  # Data High Command Low
EPD_RST_PIN = Pin(11, mode=Pin.OUT)
EPD_SCL_PIN = Pin(21, mode=Pin.OUT)
EPD_SDA_PIN = Pin(47, mode=Pin.OUT)
EPD_SPI_MISO_PIN = Pin(45, mode=Pin.IN)  # Not connected, signal not needed
EPD_PWR_EN_PIN = 48


pdi_cp = 0x9608
pdi_size = 0x96  # 9.69"
display_width = const(960)
half_width = const(display_width // 2)
display_height = const(672)
# Frame-buffer size = width * height / 8 * depth, uint32_t
# image_data_size is really only half the panel pixels, of one color
half_frame_one_color_size = const(display_height * display_width // (8 * 2))
# The width of rows in the full-frame and half-frame framebuffers
# Counting bytes, not pixels, since each byte is 8 packed pixels
display_width_bytes = const(display_width // 8)
display_half_width_bytes = const(display_width_bytes // 2)


# Two displays use different bit orders
def reverse_byte(byte: int) -> int:
    byte = (byte & 0xF0) >> 4 | (byte & 0x0F) << 4
    byte = (byte & 0xCC) >> 2 | (byte & 0x33) << 2
    byte = (byte & 0xAA) >> 1 | (byte & 0x55) << 1
    return byte


# Hardware SPI for the whole bus. The old SoftSPI was bitbanged and nowhere near
# its nominal 8 MHz. 4 MHz is the plane-transfer rate the iTC97 driver reference
# validates; set USE_HW_SPI = False to fall back to SoftSPI if anything misbehaves.
USE_HW_SPI = True
EPD_SPI_BAUD = 4_000_000

# Driver B (E2969JS0B1) only: the iTC97 reference (errata 9) says a B1 that never
# receives 0x70 <- 71, 36 draws about 2 A during refresh. The original PDI port
# never sent it, so it is off by default to keep the slow path unchanged.
# Measure supply current before and after enabling it.
B1_SEND_BOOSTER_CONFIG = False

# Print a timed line for every refresh stage to the serial console.
EPD_TIMING_LOG = True

# Keep the panel's DC/DC booster running between refreshes ("kept-open session"
# in the iTC97 reference). A later image in the same mode then costs only the
# plane transfer and the trigger: no reset, no register sequence, no soft start,
# no shutdown (about 2.5 s saved per update). Changeable at runtime through
# display.keep_powered. Off by default: the booster's idle draw is unmeasured.
KEEP_POWERED = False
# Waveform passes per fast refresh on driver B (E2969JS0B1). Each pass runs
# for a fixed ~1070 ms whatever the table, so the dose is delivered as several
# passes of the latched table. update(fast=True, passes=1) trades contrast and
# ghosting for speed; a later 3-pass refresh cleans the ghosts up.
FAST_PASSES = 3
# Close the session after this long without a refresh.
KEEP_POWERED_IDLE_MS = 60_000
# The reference tears the session down every 20 images regardless.
SESSION_MAX_IMAGES = 20

# Lookup table replacing the per-byte reverse_byte() generator.
_REVERSE_TABLE = bytes(reverse_byte(i) for i in range(256))


@micropython.viper
def _translate_inplace(buf: ptr8, n: int, table: ptr8):
    i = 0
    while i < n:
        buf[i] = table[buf[i]]
        i += 1


@micropython.viper
def _split_halves(src: ptr8, left: ptr8, right: ptr8):
    """Split a full-width 1-bpp plane into the left and right COG halves.

    Each 120-byte row gives its first 60 bytes to left and the rest to right.
    Replaces 2688 Python slice copies per refresh, which took about 3.3 s.
    """
    hw = int(display_half_width_bytes)
    fw = int(display_width_bytes)
    row = 0
    while row < int(display_height):
        s = row * fw
        d = row * hw
        i = 0
        while i < hw:
            left[d + i] = src[s + i]
            right[d + i] = src[s + hw + i]
            i += 1
        row += 1


@micropython.viper
def _any_set(buf: ptr8, n: int) -> int:
    i = 0
    while i < n:
        if buf[i]:
            return 1
        i += 1
    return 0


@micropython.viper
def _fold_red_into_black(black: ptr8, red: ptr8, n: int):
    """Draw red as ink and clear the red plane (fast refresh is black/white)."""
    i = 0
    while i < n:
        black[i] = black[i] | red[i]
        red[i] = 0
        i += 1


@micropython.viper
def _invert_inplace(buf: ptr8, n: int):
    i = 0
    while i < n:
        buf[i] = buf[i] ^ 0xFF
        i += 1


def _build_lutb_diff() -> bytes:
    """Differential short waveform for driver B (E2969JS0B1), 400 bytes.

    The COG picks a table from (red bit << 1) | bw bit. A differential
    refresh sends the previous frame in the red plane, so the four tables
    become the four transitions:

        table 0  white -> white   GND throughout: static pixels get no drive
        table 1  white -> black   shake, then VDH chopped 1:1 with GND
        table 2  black -> white   shake, then VDL chopped 1:1 with GND
        table 3  black -> black   GND throughout
        table 4  VCOM             GND

    Measured on this COG: one frame is 29.5 ms, and a group ends when the
    shortest table in it ends (erratum 4); ph2 adds no frames. So every table
    carries the same counts here, and with on = off = 1, repeat = 1 a group
    plays its level byte literally, one frame per phase: 4 frames. A group
    cannot be shorter (a zero group runs ~4 s), so 10 groups = 40 frames =
    1.18 s is the shortest pass possible.

        g0      shake: VDL VDH VDL VDH (or mirrored). Balanced.
        g1-g6   VDH GND VDH GND (or VDL GND ...): 12 drive frames, 354 ms
        g7-g9   GND: rest, so the panel is static for the last ~0.35 s

    DC: a pixel gets +12 frames when it turns black and -12 when it turns
    white, so its net charge is bounded by one transition and never builds
    up, whatever is shown for however long. The short fast table (lutb_base)
    instead gives every static paper pixel -16 and every static ink pixel +8
    frames per pass, which is what leaves red ghosts.
    """
    shake = {1: 0x99, 2: 0x66}   # VDL VDH VDL VDH / VDH VDL VDH VDL
    drive = {1: 0x44, 2: 0x88}   # VDH GND VDH GND / VDL GND VDL GND
    out = bytearray()
    for table in range(5):
        for group in range(10):
            if table in (1, 2) and group == 0:
                level = shake[table]
            elif table in (1, 2) and group <= 6:
                level = drive[table]
            else:
                level = 0x00
            # level, (on, off, ph2) x 2, repeat. Same counts in every table.
            out += bytes((level, 1, 1, 1, 1, 1, 1, 1))
    return bytes(out)


@micropython.viper
def _rgb565_to_planes(
    src: ptr8, stride: int, w: int, h: int, x1: int, y1: int, black: ptr8, red: ptr8
):
    """Threshold an LVGL RGB565 area into the black and red 1-bpp planes.

    Same thresholds as before: red if R high and G, B low; black if all low;
    white otherwise. Top bits of R, G, B are bits 15, 10 and 4 of the pixel.
    """
    row = 0
    while row < h:
        base = (y1 + row) * display_width + x1
        so = row * stride
        col = 0
        while col < w:
            i = so + col * 2
            c = src[i] | (src[i + 1] << 8)
            idx = base + col
            byte = idx >> 3
            mask = 1 << (7 - (idx & 7))
            clear = mask ^ 0xFF
            r_hi = c & 0x8000
            g_hi = c & 0x0400
            b_hi = c & 0x0010
            if r_hi and not g_hi and not b_hi:
                red[byte] = red[byte] | mask
                black[byte] = black[byte] & clear
            elif not r_hi and not g_hi and not b_hi:
                black[byte] = black[byte] | mask
                red[byte] = red[byte] & clear
            else:
                black[byte] = black[byte] & clear
                red[byte] = red[byte] & clear
            col += 1
        row += 1


class _StageLog:
    """Prints one line per refresh stage: time since start and stage duration."""

    def __init__(self):
        self.t0 = self.t = time.ticks_ms()

    def mark(self, stage: str):
        if not EPD_TIMING_LOG:
            return
        now = time.ticks_ms()
        print(
            "[epd t={:6d} ms] {:>6d} ms  {}".format(
                time.ticks_diff(now, self.t0), time.ticks_diff(now, self.t), stage
            )
        )
        self.t = now

    def total(self):
        if EPD_TIMING_LOG:
            print("[epd] total {} ms".format(time.ticks_diff(time.ticks_ms(), self.t0)))


class EPaperDisplay:
    width = display_width
    height = display_height

    def __init__(self) -> None:
        print("Initializing E-Paper Display")
        EPD_DC_PIN.value(True)
        EPD_RST_PIN.value(True)
        EPD_CS1_PIN.value(True)
        EPD_CS2_PIN.value(True)
        if USE_HW_SPI:
            self.spi = SPI(
                1,
                baudrate=EPD_SPI_BAUD,
                sck=EPD_SCL_PIN,
                mosi=EPD_SDA_PIN,
                miso=EPD_SPI_MISO_PIN,
            )
        else:
            self.spi = SoftSPI(
                baudrate=8_000_000,
                sck=EPD_SCL_PIN,
                mosi=EPD_SDA_PIN,
                miso=EPD_SPI_MISO_PIN,
            )
            self.spi.init()

        display_pn = b"B1"
        try:
            with open("/data/hwconf", "rb") as f:
                hwconf = btree.open(f)
                display_pn = hwconf.get(b"display_pn", b"B1")
        except OSError:
            pass
        # E2969JS084 has COG "driver 8", E2969JS0B1 has COG "driver B".
        # They take different 0xB0 waveform formats (see data/rendered_luts.py).
        self._driver_b = display_pn != b"84"
        self._reverse_fb_bits = self._driver_b

        # The framebuffers are set and display should be updated.
        self._framebuffer_ready = aio.Event()
        # Fast (short-waveform, black/white) refresh requested for the pending update.
        self._fast_requested = False
        # Waveform passes for the pending fast refresh (None = FAST_PASSES).
        self._passes_requested = None
        # Differential refresh (previous frame sent in the red plane) requested,
        # and a balanced flash (inverted frame, then the real one) with it.
        self._diff_requested = False
        self._flash_requested = False
        self._diff_lut = None
        # The black halves hold the frame last sent to the panel; it is
        # bit-reversed in place on driver B once sent.
        self._halves_valid = False
        self._halves_reversed = False
        # Red pigment may be on the film. A fast refresh cannot remove it, so the
        # first fast refresh after red runs on the panel's own waveform instead.
        # Unknown at boot, so assume yes.
        self._red_on_panel = True
        self._fast_lut = None
        # Half-frame copies sent to the panel, allocated on first refresh.
        self._halves = None
        # Keep the COG booster running between refreshes (see KEEP_POWERED).
        self.keep_powered = KEEP_POWERED
        self._session_mode = None  # None = closed, else "fast" / "slow"
        self._session_images = 0
        self._session_last_ms = 0

        """Each byte is a mini-row of 8 bits for pixels of one color (1 sets the color, 0 is white)
        Bytes are in rows across the screen, as seen on the badge
        0 ------------------------> 59 [60 bytes per row]
        60 ----------------------> 119
        ...                        ...
        [672 rows of 60 bytes per row]
        """
        self._framebuffer_black = bytearray(half_frame_one_color_size * 2)
        self._framebuffer_red = bytearray(half_frame_one_color_size * 2)
        # Framebuffers that can be drawn to directly
        self.black = framebuf.FrameBuffer(
            self._framebuffer_black, display_width, display_height, framebuf.MONO_HLSB
        )
        self.red = framebuf.FrameBuffer(
            self._framebuffer_red, display_width, display_height, framebuf.MONO_HLSB
        )

        # Is the display done updating? Start set since the display is not updating before boot.
        self._display_updated = aio.Event()
        self._display_updated.set()
        gc.collect()

        # Set up lvgl
        lvgl.init()
        # Give LVGL a buffer 2-bytes-per-pixel for RGB565 format
        self._lv_draw_buf = bytearray(display_width * display_height * 2)

        self.lv_display = lvgl.display_create(display_width, display_height)
        self.lv_display.set_color_format(lvgl.COLOR_FORMAT.RGB565)
        self.lv_display.set_buffers(
            self._lv_draw_buf, None, len(self._lv_draw_buf),
            lvgl.DISPLAY_RENDER_MODE.PARTIAL,
        )
        self.lv_display.set_flush_cb(self._lv_flush)
        self.lv_screen = lvgl.screen_active()
        self.lv_screen.set_size(display_width, display_height)

    def _lv_flush(self, disp, area, color_p):
        print("Calling EPaperDisplay._lv_flush")
        x1, y1 = area.x1, area.y1
        w = area.x2 - x1 + 1
        h = area.y2 - y1 + 1
        try:
            stride = lvgl.draw_buf_width_to_stride(w, lvgl.COLOR_FORMAT.RGB565)
        except AttributeError:
            stride = w * 2
        src = color_p.__dereference__(stride * h)
        _rgb565_to_planes(
            src, stride, w, h, x1, y1, self._framebuffer_black, self._framebuffer_red
        )
        disp.flush_ready()

    def update(self, fast: bool = False, passes: int | None = None,
               differential: bool = False, flash: bool = False):
        """Done updating the framebuffer, update the display with the contents

        fast=True uses the short waveform from data/rendered_luts.py: about 3.3 s
        of waveform instead of about 34-51 s, black and white only. Red in the
        framebuffer is drawn as black. If red is still on the panel from an
        earlier update, that refresh runs the slow waveform once to clear it.
        If several updates are queued before the display picks them up, the
        refresh is fast only if every one of them asked for fast.

        passes (driver B, fast only): waveform passes, default FAST_PASSES (3).
        passes=1 is about 1.1 s of waveform instead of 3.2 s, at lower contrast,
        and cannot fully clear the previous frame, so ghosts build up until a
        refresh with the full FAST_PASSES. Pair it with keep_powered = True to
        also skip the ~2.5 s of reset, init and power-up per frame. Queued
        updates use the largest pass count asked for.

        differential=True (driver B, fast): one pass of the differential table
        (_build_lutb_diff): only pixels that changed since the last frame are
        driven, with a DC-balanced drive, so static content gets no charge and
        nothing builds up. 1.18 s of waveform. flash=True first sends the
        inverted frame and then the real one: every pixel gets an equal push
        both ways and ends firmly at its rail, the balanced way to wipe the
        light ghosts single passes leave. Falls back to a normal fast refresh
        if the previous frame on the panel is not known or red may be on it.
        """
        want = FAST_PASSES if passes is None else passes
        if self._framebuffer_ready.is_set():
            self._fast_requested = self._fast_requested and fast
            have = FAST_PASSES if self._passes_requested is None else self._passes_requested
            self._passes_requested = max(have, want)
            self._diff_requested = self._diff_requested and differential
            self._flash_requested = self._flash_requested or flash
        else:
            self._fast_requested = fast
            self._passes_requested = want
            self._diff_requested = differential
            self._flash_requested = flash
        self._framebuffer_ready.set()

    def clear(self):
        """Clear framebuffers to full white"""
        self.black.fill(0)
        self.red.fill(0)

    def is_done_updating(self):
        """Is the display done updating?"""
        return self._display_updated.is_set()

    def draw_image(self, image, height: int, width: int, row: int = 0, col: int = 0):
        for img_r in range(height):
            for img_c in range(width):
                color = image[img_r][img_c]
                red = color[0] > 127
                black = color[0] < 128 and color[1] < 128 and color[2] < 128
                self.black.pixel((col + img_c), (row + img_r), int(black))
                self.red.pixel((col + img_c), (row + img_r), int(red))

    def draw_png(self, filename: str, row: int = 0, col: int = 0):

        gc.collect()
        print(f"Loading {filename} This will take a while... {time.time()}")
        show_png(filename, self._framebuffer_black, self._framebuffer_red, row, col)
        print("Done loading png")
        gc.collect()

    async def run(self):
        while True:
            if self._framebuffer_ready.is_set():
                await self.refresh_now(fast=self._fast_requested, passes=self._passes_requested,
                                       differential=self._diff_requested,
                                       flash=self._flash_requested)
                self._framebuffer_ready.clear()
            elif (
                self._session_mode is not None
                and time.ticks_diff(time.ticks_ms(), self._session_last_ms)
                > KEEP_POWERED_IDLE_MS
            ):
                self._display_updated.clear()
                await self.close_session(reason=f"idle {KEEP_POWERED_IDLE_MS} ms")
                self._display_updated.set()
            await aio.sleep_ms(100)

    async def refresh_now(self, fast: bool = False, passes: int | None = None,
                          differential: bool = False, flash: bool = False):
        """Split the framebuffers into halves and refresh the panel once."""
        if self._halves is None:
            self._halves = [bytearray(half_frame_one_color_size) for _ in range(4)]
        if differential and fast and self._driver_b and self._halves_valid and not self._red_on_panel:
            await self._refresh_differential(flash)
            return
        black_left, black_right, red_left, red_right = self._halves
        self._display_updated.clear()
        log = _StageLog()
        _split_halves(self._framebuffer_black, black_left, black_right)
        _split_halves(self._framebuffer_red, red_left, red_right)
        log.mark("split framebuffers into halves")
        await self.global_update(
            black_left, red_left, black_right, red_right, fast=fast, log=log, passes=passes
        )
        self._halves_valid = True
        self._halves_reversed = self._reverse_fb_bits
        gc.collect()
        self._display_updated.set()
        log.total()

    async def _refresh_differential(self, flash: bool):
        """Refresh with the previous frame in the red plane (see update()).

        The black halves still hold the frame last sent (bit-reversed on
        driver B), so they become this frame's red plane and the new frame is
        split into the other two buffers: no extra 80 KB of memory.
        """
        self._display_updated.clear()
        log = _StageLog()
        n = half_frame_one_color_size
        old_l, old_r, new_l, new_r = self._halves
        if self._halves_reversed:
            _translate_inplace(old_l, n, _REVERSE_TABLE)
            _translate_inplace(old_r, n, _REVERSE_TABLE)
        _split_halves(self._framebuffer_black, new_l, new_r)
        log.mark("split new frame; previous frame as red plane")
        if flash:
            # Inverted frame first: every pixel is driven away from its target.
            _invert_inplace(new_l, n)
            _invert_inplace(new_r, n)
            await self.global_update(new_l, old_l, new_r, old_r, log=log, differential=True)
            # The inverted frame is now what the panel shows: it becomes the
            # previous frame, and the real one goes into the freed buffers.
            if self._reverse_fb_bits:
                _translate_inplace(new_l, n, _REVERSE_TABLE)
                _translate_inplace(new_r, n, _REVERSE_TABLE)
            old_l, old_r, new_l, new_r = new_l, new_r, old_l, old_r
            _split_halves(self._framebuffer_black, new_l, new_r)
            log.mark("flash: inverted frame done, now the real one")
        await self.global_update(new_l, old_l, new_r, old_r, log=log, differential=True)
        self._halves = [new_l, new_r, old_l, old_r]
        self._halves_reversed = self._reverse_fb_bits
        gc.collect()
        self._display_updated.set()
        log.total()

    async def global_update(
        self,
        black_left: bytearray | bytes | memoryview[int],
        red_left: bytearray | bytes | memoryview[int],
        black_right: bytearray | bytes | memoryview[int],
        red_right: bytearray | bytes | memoryview[int],
        fast: bool = False,
        log=None,
        passes: int | None = None,
        differential: bool = False,
    ):
        """Push both halves to the panel and refresh.

        The half-frame buffers are modified in place (bit reversal, red folding),
        so pass copies, as run() does.
        """
        if log is None:
            log = _StageLog()
        n = half_frame_one_color_size
        if differential:
            # The red planes carry the previous frame, not red: no folding, no
            # red scan, one pass of the differential table.
            has_red = False
            fast = True
            passes = 1
        else:
            has_red = _any_set(red_left, n) or _any_set(red_right, n)
        use_lut = fast
        if fast and not differential:
            # Fast refresh is black/white: red is drawn as ink, never dropped.
            if has_red:
                _fold_red_into_black(black_left, red_left, n)
                _fold_red_into_black(black_right, red_right, n)
            # A short waveform cannot lift red already on the film. Promote this
            # one refresh to the panel's own waveform; red stays suppressed.
            if self._red_on_panel:
                print("Fast refresh requested but red may be on the panel; using slow waveform once")
                use_lut = False
        drew_red = has_red and not fast
        log.mark(f"scan red (has_red={has_red}, folded={fast and has_red})")

        mode = "diff" if differential else ("fast" if use_lut else "slow")
        print(f"Updating display to new image ({mode}, driver {'B' if self._driver_b else '8'})")

        # Kept-open session: the panel latches one waveform per session, so a
        # mode change has to close it first (rewriting 0xB0 in an open session
        # drops the panel back to its own waveform rather than re-latching).
        if self._session_mode is not None:
            if not self.keep_powered:
                await self.close_session(log, "keep_powered turned off")
            elif self._session_mode != mode:
                await self.close_session(log, f"mode change {self._session_mode} -> {mode}")
            elif self._session_images >= SESSION_MAX_IMAGES:
                await self.close_session(log, f"{SESSION_MAX_IMAGES} images in session")

        if self._session_mode == mode:
            # Warm: booster is still up and the waveform is still latched.
            self._send_images(black_left, red_left, black_right, red_right, log)
            await self._trigger(use_lut, log, passes)
            self._session_images += 1
            self._session_last_ms = time.ticks_ms()
            self._red_on_panel = drew_red
            log.mark(f"session kept open ({self._session_images} images)")
            return

        await self._reset()
        log.mark("hard reset")
        if self._driver_b and B1_SEND_BOOSTER_CONFIG:
            self._send_index_data(0x70, [0x71, 0x36])  # booster config, both COGs
        self._send_images(black_left, red_left, black_right, red_right, log)

        await self.cog_initial()
        log.mark("COG init register sequence")
        if use_lut:
            # The only window for 0xB0: after the last waveform-load strobe in
            # cog_initial (which loads the OTP waveform into these registers) and
            # before the DC/DC soft start. Both CS low, so both COGs latch it.
            lut = self._get_diff_lut() if differential else self._get_fast_lut()
            self._send_index_data(0xB0, lut)
            log.mark(f"write 0xB0 waveform ({len(lut)} B)")
        await self._dcdc_soft_start()
        log.mark("DC/DC soft start")
        await self._trigger(use_lut, log, passes)
        self._red_on_panel = drew_red
        if self.keep_powered:
            self._session_mode = mode
            self._session_images = 1
            self._session_last_ms = time.ticks_ms()
            log.mark("session left open (booster on)")
        else:
            await self._dcdc_soft_shutdown()
            log.mark("DC/DC soft shutdown")

    async def _trigger(self, use_lut: bool, log, passes: int | None = None):
        if use_lut and self._driver_b:
            # Driver B runs a fixed ~1070 ms window per trigger whatever the table
            # length, so the dose is delivered as several pulls of the latched
            # table (FAST_PASSES by default, fewer for low-contrast frames).
            n = FAST_PASSES if passes is None else max(1, min(int(passes), 6))
            for pull in range(n):
                await self._display_refresh(log, f"pull {pull + 1}/{n}: ")
        else:
            await self._display_refresh(log)

    async def close_session(self, log=None, reason: str = ""):
        """Shut the booster down if a kept-open session is running."""
        if self._session_mode is None:
            return
        print(f"Closing EPD session ({reason})")
        await self._dcdc_soft_shutdown()
        self._session_mode = None
        self._session_images = 0
        if log:
            log.mark("DC/DC soft shutdown (session closed)")

    def _get_diff_lut(self) -> bytes:
        if self._diff_lut is None:
            self._diff_lut = _build_lutb_diff()
        return self._diff_lut

    def _get_fast_lut(self) -> bytes:
        """The short waveform for this COG revision, loaded on first use."""
        if self._fast_lut is None:
            from data import rendered_luts

            if self._driver_b:
                lut = rendered_luts.lutb_base
                expected = 400
            else:
                # Base table: no red in this frame or the last. Red frames never
                # take the fast path, so the red variants are not needed here.
                lut = rendered_luts.lut8_nored_noclear
                expected = 180
            if len(lut) != expected:
                raise ValueError(f"0xB0 table is {len(lut)} bytes, expected {expected}")
            self._fast_lut = bytes(lut)
            del rendered_luts
            gc.collect()
        return self._fast_lut


    async def cog_initial(self):
        self._send_index_data(0x05, [0x7D])
        await aio.sleep_ms(200)
        self._send_index_data(0x05, [0x00])
        await aio.sleep_ms(10)
        self._send_index_data(0xD8, [0x80])  # MS_SYNC)
        self._send_index_data(0xD6, [0x00])  # BVSS
        self._send_index_data(0xA7, [0x10])
        await aio.sleep_ms(100)
        self._send_index_data(0xA7, [0x00])
        await aio.sleep_ms(100)
        self._send_index_data(0x03, [0x00, 0x11])  # OSC

        # Display 1
        self._send_index_data_left(0x44, [0x00])
        self._send_index_data_left(0x45, [0x80])
        self._send_index_data_left(0xA7, [0x10])
        await aio.sleep_ms(100)
        self._send_index_data_left(0xA7, [0x00])
        await aio.sleep_ms(100)
        self._send_index_data_left(0x44, [0x06])
        self._send_index_data_left(0x45, [0x82])  # Temperature 0x82@25C
        self._send_index_data_left(0xA7, [0x10])
        await aio.sleep_ms(100)
        self._send_index_data_left(0xA7, [0x00])
        await aio.sleep_ms(100)

        # Display 2
        self._send_index_data_right(0x44, [0x00])
        self._send_index_data_right(0x45, [0x80])
        self._send_index_data_right(0xA7, [0x10])
        await aio.sleep_ms(100)
        self._send_index_data_right(0xA7, [0x00])
        await aio.sleep_ms(100)
        self._send_index_data_right(0x44, [0x06])
        self._send_index_data_right(0x45, [0x82])  # Temperature 0x82@25C
        self._send_index_data_right(0xA7, [0x10])
        await aio.sleep_ms(100)
        self._send_index_data_right(0xA7, [0x00])
        await aio.sleep_ms(100)

        self._send_index_data(0x60, [0x25])  # TCON
        self._send_index_data_left(0x61, [0x01])  # STV_DIR for Display 1
        self._send_index_data(0x02, [0x00])  # VCOM

    async def cog_poweroff(self):
        """CoG shutdown function
        Shuts down the CoG and DC/DC circuit after all update functions
        """
        self._send_index_data(0x09, [0x7F, 0x7D, 0x00])
        await aio.sleep_ms(200)
        while not EPD_BUSY1_PIN.value() or not EPD_BUSY2_PIN.value():
            await aio.sleep_ms(1)
        EPD_DC_PIN.value(False)
        EPD_CS1_PIN.value(False)
        await aio.sleep_ms(150)
        EPD_RST_PIN.value(False)

    @staticmethod
    def _as_buffer(data):
        # Avoid copying the 40 KB planes; only lists need converting.
        if isinstance(data, (bytes, bytearray, memoryview)):
            return data
        return bytes(data)

    def _send_index_data(self, index: int, data: bytes | list):
        data = self._as_buffer(data)
        EPD_DC_PIN.value(False)
        EPD_CS1_PIN.value(False)
        EPD_CS2_PIN.value(False)
        self.spi.write(bytes([index]))
        EPD_CS1_PIN.value(True)
        EPD_CS2_PIN.value(True)
        EPD_DC_PIN.value(True)
        EPD_CS1_PIN.value(False)
        EPD_CS2_PIN.value(False)
        self.spi.write(data)
        EPD_CS1_PIN.value(True)
        EPD_CS2_PIN.value(True)

    def _send_index_data_left(self, index: int, data: bytes | list | memoryview[int]):
        data = self._as_buffer(data)
        EPD_DC_PIN.value(False)
        EPD_CS1_PIN.value(False)
        EPD_CS2_PIN.value(True)
        self.spi.write(bytes([index]))
        EPD_CS1_PIN.value(True)
        EPD_DC_PIN.value(True)
        EPD_CS1_PIN.value(False)
        self.spi.write(data)
        EPD_CS1_PIN.value(True)

    def _send_index_data_right(self, index: int, data: bytes | list | memoryview[int]):
        data = self._as_buffer(data)
        EPD_DC_PIN.value(False)
        EPD_CS1_PIN.value(True)
        EPD_CS2_PIN.value(False)
        self.spi.write(bytes([index]))
        EPD_CS2_PIN.value(True)
        EPD_DC_PIN.value(True)
        EPD_CS2_PIN.value(False)
        self.spi.write(data)
        EPD_CS2_PIN.value(True)

    async def _display_refresh(self, log=None, label: str = ""):
        """Wait for the booster to be ready, trigger an update, and wait for it to finish.

        Waiting for BUSY to drop after 0x15 matters when triggers are chained
        (driver B pulls the fast table three times): without it the next
        trigger, or the shutdown, could see BUSY still high and go early.
        """
        while not EPD_BUSY1_PIN.value() or not EPD_BUSY2_PIN.value():
            await aio.sleep_ms(10)
        if log:
            log.mark(label + "wait BUSY ready")
        self._send_index_data(0x15, [0x3C])
        start = time.ticks_ms()
        while EPD_BUSY1_PIN.value() and EPD_BUSY2_PIN.value():
            if time.ticks_diff(time.ticks_ms(), start) > 2000:
                print("BUSY never dropped after 0x15")
                break
            await aio.sleep_ms(5)
        if log:
            log.mark(label + "0x15 trigger until BUSY low")
        # Waveform runs while BUSY is low. Poll finely so the timing is accurate.
        while not EPD_BUSY1_PIN.value() or not EPD_BUSY2_PIN.value():
            await aio.sleep_ms(10)
        if log:
            log.mark(label + "waveform (BUSY low)")

    async def _reset(self):
        """CoG driver power-on hard reset"""
        await aio.sleep_ms(200)
        EPD_RST_PIN.value(True)
        await aio.sleep_ms(20)
        EPD_RST_PIN.value(False)
        await aio.sleep_ms(200)
        EPD_RST_PIN.value(True)
        await aio.sleep_ms(200)
        EPD_CS1_PIN.value(True)
        EPD_CS2_PIN.value(True)
        await aio.sleep_ms(5)

    def _send_images(
        self,
        black_left: bytearray | bytes | memoryview[int],
        red_left: bytearray | bytes | memoryview[int],
        black_right: bytearray | bytes | memoryview[int],
        red_right: bytearray | bytes | memoryview[int],
        log=None,
    ):
        assert len(black_left) == half_frame_one_color_size
        assert len(red_left) == half_frame_one_color_size
        assert len(black_right) == half_frame_one_color_size
        assert len(red_right) == half_frame_one_color_size

        if self._reverse_fb_bits:
            n = half_frame_one_color_size
            planes = []
            for plane in (black_left, red_left, black_right, red_right):
                if not isinstance(plane, bytearray):
                    plane = bytearray(plane)
                _translate_inplace(plane, n, _REVERSE_TABLE)
                planes.append(plane)
            black_left, red_left, black_right, red_right = planes
            if log:
                log.mark("bit-reverse 4 planes")

        # DUW for both left and right halves
        self._send_index_data(0x13, [0x00, 0x3B, 0x00, 0x00, 0x9F, 0x02])
        # DRFW for Both left and right halves
        self._send_index_data(0x90, [0x00, 0x3B, 0x00, 0xA9])
        ram_rw_data = [0x3B, 0x00, 0x14]
        self._send_index_data_left(0x12, ram_rw_data)  # RAM_RW for left half
        self._send_index_data_left(0x10, black_left)  # Black frame for left half
        self._send_index_data_left(0x12, ram_rw_data)  # RAM_RW for left half
        self._send_index_data_left(0x11, red_left)  # Red frame for left half
        self._send_index_data_right(0x12, ram_rw_data)  # RAM_RW for right half
        self._send_index_data_right(0x10, black_right)  # black frame for right half
        self._send_index_data_right(0x12, ram_rw_data)  # RAM_RW for right half
        self._send_index_data_right(0x11, red_right)  # Red frame for right half
        if log:
            log.mark(f"SPI send 4 planes ({4 * half_frame_one_color_size} B)")


    async def _dcdc_soft_start(self):
        """DC-DC soft-start command
        Implemented after image data are uploaded to CoG
        Specific to mid-sized EPDs only
        """
        self._send_index_data(0x51, [0x50, 0x01])

        for val in range(1, 5):
            self._send_index_data(0x09, [0x1F])
            self._send_index_data(0x51, [0x50, val])
            self._send_index_data(0x09, [0x9F])
            await aio.sleep_ms(2)

        for val in range(1, 11):
            self._send_index_data(0x09, [0x1F])
            self._send_index_data(0x51, [0x0A, val])
            self._send_index_data(0x09, [0x9F])
            await aio.sleep_ms(2)

        for val in range(3, 11):
            self._send_index_data(0x09, [0x7F])
            self._send_index_data(0x51, [0x0A, val])
            self._send_index_data(0x09, [0xFF])
            await aio.sleep_ms(2)

        for val in range(9, 1, -1):
            self._send_index_data(0x09, [0x7F])
            self._send_index_data(0x51, [val, 0x0A])
            self._send_index_data(0x09, [0xFF])
            await aio.sleep_ms(2)

        self._send_index_data(0x09, [0xFF])
        await aio.sleep_ms(10)

    async def _dcdc_soft_shutdown(self):
        """DC-DC soft-shutdown command
        Implemented after image data are uploaded to CoG
        Specific to mid-sized EPDs only
        """
        while not EPD_BUSY1_PIN.value() or not EPD_BUSY2_PIN.value():
            await aio.sleep_ms(100)
        self._send_index_data(0x09, [0x7F])
        self._send_index_data(0x05, [0x7D])
        self._send_index_data(0x09, [0x00])
        await aio.sleep_ms(200)
        while not EPD_BUSY1_PIN.value() or not EPD_BUSY2_PIN.value():
            await aio.sleep_ms(100)
        EPD_DC_PIN.value(False)
        EPD_CS1_PIN.value(False)
        EPD_RST_PIN.value(False)
        EPD_CS1_PIN.value(True)


display = EPaperDisplay()

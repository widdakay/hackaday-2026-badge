""" Supercon Schedule App """
import lvgl

from apps.base_app import BaseApp
from lib.epd import display, display_height, display_width
from lib.led_bar import leds

# Schedule ink. Swap these two to invert the page.
_BG = lvgl.color_hex(0xFFFFFF)
_FG = lvgl.color_hex(0x000000)
_FONT = lvgl.font_montserrat_14
_SPEAKER_FONT = lvgl.font_montserrat_16
_TIME_FONT = lvgl.font_montserrat_24

# Enabled sizes step 14, 16, 24. Title stays 14, speaker is one step up,
# time is two steps up.
_TITLE_H = 16
_SPEAKER_H = 18
_TIME_H = 27
PAD = 4
_FONTS = {
    "time": (_TIME_FONT, _TIME_H),
    "speaker": (_SPEAKER_FONT, _SPEAKER_H),
    "title": (_FONT, _TITLE_H),
}
_SCHEDULE = "/data/schedule.csv"
_TALK_STAGES = ("Lamarr", "Hopper")
_WORKSHOP_ROOMS = ("HQ 2nd Floor", "Room A", "Room B")
_DAY_ORDER = ("Fri", "Sat", "Sun")
# Taken from Saturday's band so its rows sit closer and the days separate.
_DAY_GAP = 16
# Montserrat 14 advance widths for U+0020..U+007F, in pixels.
_ADV = (
    4,4,6,10,9,12,10,3,5,5,6,8,3,5,3,5,9,5,8,8,9,8,9,8,9,9,3,3,8,8,8,8,
    15,10,11,10,12,9,9,11,11,4,7,10,8,13,11,12,10,12,10,9,8,11,10,16,9,9,9,5,5,5,8,
    7,8,8,10,8,10,9,5,10,10,4,4,9,4,15,10,9,10,10,6,7,6,10,8,13,8,8,7,5,4,5,8,6,
)

def _parse_csv(text):
    """Parse CSV text, including quoted fields."""
    rows = []
    row = []
    field = []
    quoted = False
    i = 0
    length = len(text)
    while i < length:
        char = text[i]
        if quoted:
            if char == '"':
                if i + 1 < length and text[i + 1] == '"':
                    field.append('"')
                    i += 1
                else:
                    quoted = False
            else:
                field.append(char)
        elif char == '"':
            quoted = True
        elif char == ",":
            row.append("".join(field))
            field = []
        elif char == "\n":
            row.append("".join(field))
            field = []
            if row != [""]:
                rows.append(row)
            row = []
        elif char != "\r":
            field.append(char)
        i += 1
    if field or row:
        row.append("".join(field))
        if row != [""]:
            rows.append(row)
    return rows

def _load_rows():
    """Every schedule row, in file order."""
    try:
        with open(_SCHEDULE, "rb") as handle:
            text = handle.read().decode()
    except OSError:
        print(f"Could not read {_SCHEDULE}")
        return []
    rows = _parse_csv(text)
    if not rows:
        return []
    header = {name: index for index, name in enumerate(rows[0])}
    talks = []
    for row in rows[1:]:
        if len(row) <= header["speaker"]:
            continue
        talks.append(
            {
                "day": row[header["day"]],
                "time": row[header["time"]],
                "stage": row[header["stage"]],
                "talk": row[header["talk"]],
                "speaker": row[header["speaker"]],
            }
        )
    return talks

def load_talks(day):
    """Return one day's stage talks, in schedule order. Workshops are skipped."""
    return [
        talk
        for talk in _load_rows()
        if talk["day"] == day and talk["stage"] in _TALK_STAGES
    ]

def load_workshops():
    """Return every workshop, Friday then Saturday then Sunday."""
    order = {day: index for index, day in enumerate(_DAY_ORDER)}
    workshops = [talk for talk in _load_rows() if talk["stage"] in _WORKSHOP_ROOMS]
    ranked = [(order[talk["day"]], index, talk) for index, talk in enumerate(workshops)]
    ranked.sort()
    return [talk for _, _, talk in ranked]

def _stages(talks):
    """Stages in the order they first appear. Lamarr is above Hopper."""
    order = []
    grouped = {}
    for talk in talks:
        stage = talk["stage"]
        if stage not in grouped:
            order.append(stage)
            grouped[stage] = []
        grouped[stage].append(talk)
    return [(stage, grouped[stage]) for stage in order]

def _talk_lines(talk, text_w, box_h, show_room=False):
    """Time, speaker, then the title. None when the box is too small.

    Each entry is (role, text). Role selects the font size. Workshops put the
    room on the time line.
    """
    time_text = talk["time"]
    if show_room:
        time_text = f"{talk['time']}  {talk['stage']}"
    if _text_width(time_text, 24) > text_w:
        return None
    body = box_h - 2 * PAD
    speaker_budget = (body - _TIME_H - _TITLE_H) // _SPEAKER_H
    if speaker_budget < 1:
        return None
    speaker = _wrap(talk["speaker"], text_w, speaker_budget, 16)
    if " ".join(speaker) != talk["speaker"]:
        return None
    used = _TIME_H + len(speaker) * _SPEAKER_H
    title_budget = (body - used) // _TITLE_H
    if title_budget < 1:
        return None
    # A long title is cut off. Fitting every word forced the columns too narrow.
    title = _wrap(talk["talk"], text_w, title_budget, 14)
    lines = [("time", time_text)]
    lines.extend(("speaker", line) for line in speaker)
    lines.extend(("title", line) for line in title)
    return lines

def _box_grid(count, content_w, band_h, talks, show_room=False):
    """Equal boxes for `count` slots. Fewest columns that fit every talk."""
    for cols in range(1, count + 1):
        rows = (count + cols - 1) // cols
        box_h = band_h // rows
        box_w = content_w // cols
        text_w = box_w - 2 * PAD
        if text_w < 40:
            continue
        if all(
            _talk_lines(talk, text_w, box_h, show_room) is not None for talk in talks
        ):
            return cols, rows, box_w, box_h
    rows = count
    return 1, rows, content_w, band_h // rows

def _stage_image(stage):
    """On-device path for a stage picture. `badge/data` is mounted at `/data`."""
    return f"/data/graphics/{stage.lower()}.png"

def _day_image(day):
    """On-device path for a day picture. `badge/data` is mounted at `/data`."""
    name = {"Fri": "friday.png", "Sat": "saturday.png", "Sun": "sunday.png"}[day]
    return f"/data/graphics/{name}"

def _png_size(path):
    """Return (width, height) from a PNG header, or None if it cannot be read."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(24)
    except OSError:
        print(f"Could not read {path}")
        return None
    if len(header) < 24 or header[12:16] != b"IHDR":
        print(f"Not a png: {path}")
        return None
    return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")

def _place_band(talks, x, y, stage_w, cols, rows, box_w, box_h, show_room=False):
    """Lay talks to the right of the stage picture. Fill a row, then the next."""
    content_x = x + stage_w
    text_w = box_w - 2 * PAD
    placed = []
    for index, talk in enumerate(talks):
        col = index % cols
        row = index // cols
        if col >= cols:
            break
        box_x = content_x + col * box_w
        box_y = y + row * box_h
        line_y = box_y + PAD
        for role, text in _talk_lines(talk, text_w, box_h, show_room):
            placed.append((box_x + PAD, line_y, text_w, text, role))
            line_y += _FONTS[role][1]
    return placed

def layout_day(talks, width=display_width, height=display_height, day="Sat"):
    """Split the screen into a top stage and a bottom stage.

    Both halves use the same talk-box size. The left column is as wide as the
    wider stage picture. The day picture sits in the bottom-right box of the
    bottom stage, flush with that box's right and bottom edges.
    """
    content_h = height
    stages = _stages(talks)
    if not stages:
        return {"images": [], "lines": []}
    if len(stages) == 1:
        bands = [(stages[0][0], stages[0][1], 0, content_h)]
        band_h = content_h
    else:
        band_h = content_h // 2
        bands = [
            (stages[0][0], stages[0][1], 0, band_h),
            (stages[1][0], stages[1][1], band_h, band_h),
        ]
    sizes = [_png_size(_stage_image(stage)) for stage, _, _, _ in bands]
    stage_w = max((size[0] for size in sizes if size), default=0)
    content_w = width - stage_w
    every = [talk for _, stage_talks, _, _ in bands for talk in stage_talks]
    slot_count = max(len(stage_talks) for _, stage_talks, _, _ in bands)
    cols, rows, box_w, box_h = _box_grid(slot_count, content_w, band_h, every)
    images = []
    lines = []
    for (stage, stage_talks, band_y, this_h), size in zip(bands, sizes):
        if size:
            img_w, img_h = size
            col = max(0, (stage_w - img_w) // 2)
            # Taller than the half: hang from the top of the band. A shorter
            # picture is centered in the band.
            if img_h <= this_h:
                row = band_y + (this_h - img_h) // 2
            else:
                row = band_y
            images.append((_stage_image(stage), col, row, img_w, img_h))
        lines.extend(
            _place_band(stage_talks, 0, band_y, stage_w, cols, rows, box_w, box_h)
        )
    # Bottom-right cell of the bottom stage. Right- and bottom-align the day
    # label in that free box. The file is present before the app runs.
    with open(_day_image(day), "rb") as handle:
        header = handle.read(24)
    img_w = int.from_bytes(header[16:20], "big")
    img_h = int.from_bytes(header[20:24], "big")
    cell_x = stage_w + (cols - 1) * box_w
    cell_y = bands[-1][2] + (rows - 1) * box_h
    images.append(
        (_day_image(day), cell_x + box_w - img_w, cell_y + box_h - img_h, img_w, img_h)
    )
    return {"images": images, "lines": lines}

def _split_bands(total, counts):
    """Split `total` pixels across bands in proportion to `counts`.

    Leftover pixels go to the busiest band.
    """
    weight = sum(counts)
    heights = [total * count // weight for count in counts]
    busiest = max(range(len(counts)), key=lambda index: counts[index])
    heights[busiest] += total - sum(heights)
    return heights

def layout_workshops(talks, width=display_width, height=display_height):
    """Three day bands, Friday on top, then Saturday, then Sunday.

    Saturday is taller because it has more workshops. workshops.png is the
    full-height strip on the left, and each day picture sits just to its right.
    The files are present before the app runs.
    """
    grouped = {day: [] for day in _DAY_ORDER}
    for talk in talks:
        if talk["day"] in grouped:
            grouped[talk["day"]].append(talk)
    days = [(day, grouped[day]) for day in _DAY_ORDER if grouped[day]]
    if not days:
        return {"images": [], "lines": []}
    heights = _split_bands(height, [len(items) for _, items in days])
    with open("/data/graphics/workshops.png", "rb") as handle:
        header = handle.read(24)
    strip_w = int.from_bytes(header[16:20], "big")
    strip_h = int.from_bytes(header[20:24], "big")
    images = [("/data/graphics/workshops.png", 0, 0, strip_w, strip_h)]
    lines = []
    band_y = 0
    for index, ((day, items), band_h) in enumerate(zip(days, heights)):
        with open(_day_image(day), "rb") as handle:
            header = handle.read(24)
        img_w = int.from_bytes(header[16:20], "big")
        img_h = int.from_bytes(header[20:24], "big")
        # Saturday gives a gap to each neighboring day and packs its rows
        # into the shorter space that remains.
        gap_before = _DAY_GAP if day == "Sat" and index > 0 else 0
        gap_after = _DAY_GAP if day == "Sat" and index < len(days) - 1 else 0
        content_y = band_y + gap_before
        content_h = band_h - gap_before - gap_after
        if img_h <= content_h:
            row = content_y + (content_h - img_h) // 2
        else:
            row = content_y
        images.append((_day_image(day), strip_w, row, img_w, img_h))
        gutter = strip_w + img_w
        content_w = width - gutter
        cols, rows, box_w, box_h = _box_grid(
            len(items), content_w, content_h, items, show_room=True
        )
        lines.extend(
            _place_band(
                items, 0, content_y, gutter, cols, rows, box_w, box_h, show_room=True
            )
        )
        band_y += band_h
    return {"images": images, "lines": lines}

def _text_width(text, scale=14):
    width = 0
    for char in text:
        code = ord(char)
        px = _ADV[code - 32] if 32 <= code <= 127 else 8
        width += (px * scale + 7) // 14
    return width

def _wrap(text, max_width, max_lines, scale=14):
    """Break `text` into at most `max_lines` lines that fit `max_width`."""
    words = text.split(" ")
    lines = []
    current = ""
    index = 0
    while index < len(words) and len(lines) < max_lines:
        word = words[index]
        trial = word if not current else current + " " + word
        if _text_width(trial, scale) <= max_width:
            current = trial
            index += 1
            continue
        if current:
            lines.append(current)
            current = ""
            continue
        lines.append(word)
        index += 1
    if current and len(lines) < max_lines:
        lines.append(current)
    return lines

def _add_line(parent, x, y, width, text, role="title"):
    """One single-line label in the foreground color. `role` picks the font."""
    font, line_h = _FONTS[role]
    label = lvgl.label(parent)
    label.remove_style_all()
    label.set_pos(x, y)
    label.set_size(width, line_h)
    label.set_style_text_font(font, 0)
    label.set_style_text_color(_FG, 0)
    label.set_style_bg_opa(lvgl.OPA.TRANSP, 0)
    label.set_text(text)
    return y + line_h

class ScheduleApp(BaseApp):
    """Schedule table for the day named by the `day` class attribute."""
    _abstract = True
    day = ""

    def __init__(self, name: str | None = None) -> None:
        super().__init__(name or self.day)
        self.name = self.day + " Talks" 
        self._built = False
        self._scr = None
        self._images = []

    def run(self):
        if self.launched:
            self._draw()

    def _load(self):
        return load_talks(self.day)

    def _layout(self, talks):
        return layout_day(talks, day=self.day)

    def _draw(self):
        leds.fill((0,1,1))
        talks = self._load()
        if not self._built:
            self._build(talks)
        display.clear()
        lvgl.screen_load(self._scr)
        self._scr.update_layout()
        # One refresh of the opaque screen. Invalidating every label overflows
        # LVGL's area list and the transparent fallback flushes as solid black.
        lvgl.refr_now(display.lv_display)
        # The lower picture first. A taller picture above keeps the ink that
        # hangs into the next band.
        for path, col, row, _img_w, _img_h in sorted(self._images, key=lambda item: -item[2]):
            display.draw_png(path, row, col)
        # Black and white only (text plus the day/stage pictures), so the short
        # waveform loses nothing. The first one after a red screen runs slow once.
        display.update(fast=True)
        leds.fill((0,1,0))

    def _build(self, talks):
        placed = self._layout(talks)
        scr = lvgl.obj()
        scr.remove_style_all()
        scr.set_size(display_width, display_height)
        scr.set_style_bg_color(_BG, 0)
        scr.set_style_bg_opa(lvgl.OPA.COVER, 0)

        if not talks:
            _add_line(scr, PAD, PAD, display_width - 2 * PAD, f"No talks for {self.day}")

        for x, y, width, text, role in placed["lines"]:
            _add_line(scr, x, y, width, text, role)
        self._images = placed["images"]
        self._scr = scr
        self._built = True

class SatSchedule(ScheduleApp):
    """Saturday talks."""

    _abstract = False
    index = 0
    day = "Sat"

class SunSchedule(ScheduleApp):
    """Sunday talks."""

    _abstract = False
    index = 1
    day = "Sun"

class Workshops(ScheduleApp):
    """Friday, Saturday, and Sunday workshops."""

    _abstract = False
    index = 2

    def __init__(self) -> None:
        super().__init__("Workshops")
        self.name = "Workshops"

    def _load(self):
        return load_workshops()

    def _layout(self, talks):
        return layout_workshops(talks)

"""Grow the MicroPython heap at boot, so garbage collections are rare.

The heap lives in PSRAM and starts at what the imports need (~1.8 MB), with
only ~100 KB free. MicroPython takes more RAM only when a collection cannot
free enough, and every collection frees a few hundred KB, so the heap never
grows: instead the badge collects every ~100 KB of garbage. Each collection
takes ~250-330 ms (it scans ~1.5 MB of live objects in PSRAM) and stops
everything, the LED animation included.

grow() makes the heap take a big new area: one large allocation creates it,
and a small "anchor" object placed inside it keeps it alive (MicroPython
hands an area back once it is empty). The anchor is kept forever.
"""

import gc

import uctypes

_anchor = None


def grow(nbytes=4 * 1024 * 1024):
    """Add a heap area of about nbytes. Returns the new free byte count."""
    global _anchor
    if _anchor is not None:
        return gc.mem_free()
    gc.collect()
    try:
        big = bytearray(nbytes)  # does not fit the current area: a new one is added
    except MemoryError:
        return gc.mem_free()
    lo = uctypes.addressof(big)
    hi = lo + nbytes + nbytes // 32 + 65536  # the area is sized to the block plus a margin
    # Use up the old area's free space, so a small allocation lands in the new one.
    filler = []
    for _ in range(20000):
        b = bytearray(1024)
        if lo <= uctypes.addressof(b) < hi:
            _anchor = b
            break
        filler.append(b)
    del filler, big
    gc.collect()
    return gc.mem_free()

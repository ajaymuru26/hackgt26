"""
Keep the robot's writing off the ink that's already on the board.

The board is tracked as a coarse grid of cells. Anything already written (the
person's strokes, ink in an uploaded picture, answers placed earlier in the same
response) marks its cells as taken, with a small clear margin around it. New
text tries its preferred spots in order and takes the first one that is clear.
"""
import io
import math
from collections import namedtuple

from PIL import Image, ImageFilter

CELL = 4     # grid cell size, in board pixels
MARGIN = 6   # clear space kept around existing ink
EDGE = 10    # keep this far from the edge of the board
GAP = MARGIN + 2 * CELL  # smallest gap that always clears the margin (cells round outward)

Placed = namedtuple("Placed", "strokes where size clean")


def bounds(strokes):
    pts = [p for s in strokes for p in s]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def shift(strokes, dx, dy):
    return [[[round(p[0] + dx, 1), round(p[1] + dy, 1)] for p in s] for s in strokes]


class Board:
    def __init__(self, w, h, strokes=(), png=None, image_box=None, image_is_board=False):
        self.w, self.h = w, h
        self.cols, self.rows = math.ceil(w / CELL), math.ceil(h / CELL)
        self.grid = bytearray(self.cols * self.rows)
        self._sums = None
        self._strokes = []
        self._image = (png, image_box, image_is_board)
        self.add(strokes)
        if image_box and len(image_box) >= 4:
            if image_is_board and png:
                self._add_image_ink(png, image_box)
            else:
                # A picture beside the writing counts as writing, like the rest of the layout.
                x1, y1, x2, y2 = image_box[:4]
                self._mark_box(x1 - MARGIN, y1 - MARGIN, x2 + MARGIN, y2 + MARGIN)

    # ----- marking ink -----

    def _mark_box(self, x1, y1, x2, y2):
        c1, c2 = max(0, int(x1 // CELL)), min(self.cols - 1, int(x2 // CELL))
        r1, r2 = max(0, int(y1 // CELL)), min(self.rows - 1, int(y2 // CELL))
        for r in range(r1, r2 + 1):
            row = r * self.cols
            for c in range(c1, c2 + 1):
                self.grid[row + c] = 1
        self._sums = None

    def without(self, skip):
        """A copy of this board that ignores some strokes (like the blank line an answer is written on)."""
        skip = {id(s) for s in skip}
        return Board(self.w, self.h, [s for s in self._strokes if id(s) not in skip], *self._image)

    def add(self, strokes):
        """Mark strokes (and a margin around them) as taken."""
        for s in strokes or []:
            self._strokes.append(s)
            pts = [p for p in s if len(p) >= 2]
            for a, b in zip(pts, pts[1:] or pts):
                n = max(1, int(math.dist(a, b) / CELL))
                for i in range(n + 1):
                    x = a[0] + (b[0] - a[0]) * i / n
                    y = a[1] + (b[1] - a[1]) * i / n
                    self._mark_box(x - MARGIN, y - MARGIN, x + MARGIN, y + MARGIN)

    def _add_image_ink(self, png, box):
        """Mark the dark (or light) marks inside an uploaded picture, not its blank background."""
        img = Image.open(io.BytesIO(png)).convert("L")
        x1, y1 = max(0, int(box[0])), max(0, int(box[1]))
        x2, y2 = min(img.width, int(box[2])), min(img.height, int(box[3]))
        if x2 - x1 < CELL or y2 - y1 < CELL:
            return
        crop = img.crop((x1, y1, x2, y2))
        hist = crop.histogram()
        half, seen, bg = crop.width * crop.height / 2, 0, 255
        for level, count in enumerate(hist):  # the most common brightness is the background
            seen += count
            if seen >= half:
                bg = level
                break
        mask = crop.point([255 if abs(i - bg) > 36 else 0 for i in range(256)])
        mask = mask.filter(ImageFilter.MaxFilter(2 * (MARGIN // 2) + 1))
        c1, r1 = x1 // CELL, y1 // CELL
        cells = mask.resize((max(1, (x2 - x1) // CELL), max(1, (y2 - y1) // CELL)), Image.BOX)
        for r in range(cells.height):
            for c in range(cells.width):
                if cells.getpixel((c, r)) > 20:  # at least ~8% of the cell is ink
                    x, y = (c1 + c) * CELL, (r1 + r) * CELL
                    self._mark_box(x - MARGIN, y - MARGIN, x + CELL + MARGIN, y + CELL + MARGIN)

    # ----- checking space -----

    def _table(self):
        if self._sums is None:
            cols = self.cols + 1
            sums = [0] * (cols * (self.rows + 1))
            for r in range(self.rows):
                run = 0
                for c in range(self.cols):
                    run += self.grid[r * self.cols + c]
                    sums[(r + 1) * cols + c + 1] = sums[r * cols + c + 1] + run
            self._sums = sums
        return self._sums

    def inside(self, box):
        x1, y1, x2, y2 = box
        return x1 >= EDGE and y1 >= EDGE and x2 <= self.w - EDGE and y2 <= self.h - EDGE

    def overlap(self, box):
        """How many taken cells the box touches."""
        c1, c2 = max(0, int(box[0] // CELL)), min(self.cols - 1, int(box[2] // CELL))
        r1, r2 = max(0, int(box[1] // CELL)), min(self.rows - 1, int(box[3] // CELL))
        if c1 > c2 or r1 > r2:
            return 0
        s, cols = self._table(), self.cols + 1
        return (s[(r2 + 1) * cols + c2 + 1] - s[r1 * cols + c2 + 1]
                - s[(r2 + 1) * cols + c1] + s[r1 * cols + c1])

    def free(self, box):
        return self.inside(box) and self.overlap(box) == 0

    def nearest_free(self, bw, bh, near, step=CELL * 2):
        """Top-left corner of the clear bw x bh space closest to `near`, or None."""
        spots = [(x, y) for y in range(EDGE, int(self.h - EDGE - bh) + 1, step)
                 for x in range(EDGE, int(self.w - EDGE - bw) + 1, step)]
        spots.sort(key=lambda p: math.dist(p, near))
        for x, y in spots:
            if self.overlap((x, y, x + bw, y + bh)) == 0:
                return x, y
        return None


def place(board, render, spots, sizes, spot_first=False):
    """
    Put text (or a mark) somewhere clear.

    render(size) -> strokes drawn at the origin.
    spots(size, width, height) -> [(x, y, where), ...] origins to try, best first.
        A spot can add a 4th value: extra clear space needed after it (to the right),
        so text on the same line as other writing doesn't squeeze into a gap and run
        into the next thing (5+4=9 7+8= reading as "97").
    sizes: text sizes to try, largest first.
    spot_first: try every size at the first spot before moving on to the next spot
                (for answers that should stay on the problem's line).

    Order: the preferred spots, then smaller sizes, then the nearest clear space,
    and only if the board is full, the spot with the least overlap.
    """
    shapes = []
    for size in sizes:
        shape = render(size)
        if shape:
            ox1, oy1, ox2, oy2 = bounds(shape)
            shapes.append((size, shape, ox1, oy1, ox2 - ox1, oy2 - oy1))
    if not shapes:
        return Placed([], "", sizes[0] if sizes else 0, False)

    tries, pads = [], {}
    for size, shape, ox, oy, bw, bh in shapes:
        for x, y, where, *pad in spots(size, bw, bh):
            tries.append((size, shape, ox, oy, bw, bh, x, y, where))
            pads[id(tries[-1])] = pad[0] if pad else 0
    if spot_first:
        order = {}
        for t in tries:
            order.setdefault(t[8], len(order))
        tries.sort(key=lambda t: order[t[8]])  # stable: keeps the size order within each spot

    def accept(t, clean, where=None):
        size, shape, ox, oy, bw, bh, x, y, label = t
        placed = shift(shape, x, y)
        board.add(placed)
        return Placed(placed, where or label, size, clean)

    for t in tries:
        size, shape, ox, oy, bw, bh, x, y, _ = t
        box = (x + ox, y + oy, x + ox + bw, y + oy + bh)
        pad = pads[id(t)]
        if board.inside(box) and board.overlap((box[0], box[1], box[2] + pad, box[3])) == 0:
            return accept(t, True)

    # Nowhere natural is clear: find the nearest clear space to the first choice.
    first = tries[0] if tries else None
    if first:
        near = (first[6] + first[2], first[7] + first[3])
        for size, shape, ox, oy, bw, bh in (shapes[0], shapes[-1]):
            spot = board.nearest_free(bw, bh, near)
            if spot:
                return accept((size, shape, ox, oy, bw, bh, spot[0] - ox, spot[1] - oy, ""), True,
                              "in the nearest clear space (the usual spots already have writing)")

    # The board is full: least overlap wins, kept on the board.
    def clamp(t):
        size, shape, ox, oy, bw, bh, x, y, where = t
        x = min(max(x + ox, EDGE), board.w - EDGE - bw) - ox
        y = min(max(y + oy, EDGE), board.h - EDGE - bh) - oy
        return (size, shape, ox, oy, bw, bh, x, y, where)

    options = [clamp(t) for t in tries] or [clamp((*shapes[-1], EDGE, EDGE, "at the top of the board"))]
    best = min(options, key=lambda t: board.overlap((t[6] + t[2], t[7] + t[3],
                                                     t[6] + t[2] + t[4], t[7] + t[3] + t[5])))
    placed = accept(best, False)
    return placed._replace(where=f"{placed.where} (the board is crowded, so it may touch other writing)")


def shrinking(size, smallest=22, factor=0.85, steps=None):
    """Sizes to try, largest first: size, size*factor, ... down to `smallest`."""
    sizes = [size]
    while sizes[-1] * factor >= smallest and (steps is None or len(sizes) < steps):
        sizes.append(sizes[-1] * factor)
    return sizes

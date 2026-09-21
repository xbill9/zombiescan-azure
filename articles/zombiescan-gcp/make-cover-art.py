#!/usr/bin/env python3
"""Draw this article's cover: house typography on the left, a penguin standing
on a cloud while dollar signs drift past on the right.

The palette, the fonts, the 2x draw-then-downsample and the content-addressed
filename all come from the publishing kit's make-cover.py, imported rather than
copied so a change there reaches this cover too. Only the illustration is local.

    python3 make-cover-art.py --out devto-cover.jpg --content-address \
        --url-base https://raw.githubusercontent.com/xbill9/zombiescan-gcp/main/articles/zombiescan-gcp
"""

import argparse
import importlib.util
import os
import pathlib
import sys

from PIL import Image, ImageDraw, ImageFont

KIT = pathlib.Path.home() / "publishing-kit/skills/publishing/scripts/make-cover.py"
spec = importlib.util.spec_from_file_location("make_cover", KIT)
mc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mc)

W, H = mc.MODES["devto"]
S = 2  # draw at 2x, downsample, or type goes soft

SURFACE = mc.SURFACE
INK, INK_2, INK_3 = mc.INK, mc.INK_2, mc.INK_3
BLUE, ORANGE = mc.COLOURS["blue"], mc.COLOURS["orange"]

CLOUD = (68, 68, 64)
BODY = (34, 34, 36)  # a penguin's black, lifted off the surface
EDGE = (86, 86, 82)  # silhouette stroke, or the body vanishes
BELLY = (246, 245, 238)
BEAK = (241, 170, 46)
FOOT = (226, 146, 38)


def px(v):
    return int(round(v * S))


def font(path, size):
    return ImageFont.truetype(path, px(size))


def text(d, xy, s, fnt, fill):
    d.text((px(xy[0]), px(xy[1])), s, font=fnt, fill=fill, anchor="la")


def capsule(d, p0, p1, w, fill):
    """A line with round caps, so a limb can sit at any angle."""
    x0, y0, x1, y1 = px(p0[0]), px(p0[1]), px(p1[0]), px(p1[1])
    r = px(w) // 2
    d.line((x0, y0, x1, y1), fill=fill, width=px(w))
    for x, y in ((x0, y0), (x1, y1)):
        d.ellipse((x - r, y - r, x + r, y + r), fill=fill)


def oval(d, cx, cy, w, h, fill, grow=0.0):
    d.ellipse(
        (
            px(cx - w / 2 - grow),
            px(cy - h / 2 - grow),
            px(cx + w / 2 + grow),
            px(cy + h / 2 + grow),
        ),
        fill=fill,
    )


def cloud(d, cx, cy, w, fill):
    """Flat cloud silhouette: a row of circles over a capsule base."""
    h = w * 0.42
    d.rounded_rectangle(
        (px(cx - w / 2), px(cy - h * 0.32), px(cx + w / 2), px(cy + h * 0.32)),
        radius=px(h * 0.32),
        fill=fill,
    )
    for dx, r in ((-0.27, 0.30), (0.01, 0.42), (0.28, 0.27)):
        rr = w * r / 2
        oval(d, cx + w * dx, cy - h * 0.18, rr * 2, rr * 2, fill)


def penguin(d, cx, fy, h):
    """A penguin, drawn as flat shapes from the feet up.

    The black silhouette is laid down twice — once grown by a few units in EDGE,
    then at size in BODY — because a union of overlapping shapes has no outline
    of its own, and without one the bird disappears into a dark cover.
    """
    u = h / 100.0
    g = 1.6 * u  # stroke width, in body units

    def silhouette(fill, grow):
        capsule(
            d, (cx - 26 * u, fy - 60 * u), (cx - 39 * u, fy - 26 * u), 15 * u + 2 * grow, fill
        )  # flippers, behind the body
        capsule(d, (cx + 26 * u, fy - 60 * u), (cx + 39 * u, fy - 26 * u), 15 * u + 2 * grow, fill)
        oval(d, cx, fy - 38 * u, 62 * u, 74 * u, fill, grow)  # body
        oval(d, cx, fy - 85 * u, 49 * u, 48 * u, fill, grow)  # head

    silhouette(EDGE, g)

    for sx in (-1, 1):  # webbed feet
        oval(d, cx + sx * 17 * u, fy - 4 * u, 34 * u, 14 * u, FOOT)
        oval(d, cx + sx * 27 * u, fy - 3 * u, 20 * u, 11 * u, FOOT)

    silhouette(BODY, 0)

    oval(d, cx, fy - 33 * u, 45 * u, 56 * u, BELLY)  # belly
    oval(d, cx, fy - 80 * u, 38 * u, 30 * u, BELLY)  # face

    for sx in (-1, 1):  # eyes
        oval(d, cx + sx * 8.5 * u, fy - 87 * u, 7.5 * u, 9 * u, BODY)
        oval(d, cx + sx * 9.5 * u, fy - 88.5 * u, 2.6 * u, 2.6 * u, BELLY)

    oval(d, cx, fy - 76 * u, 19 * u, 11 * u, BEAK)  # beak
    d.line(
        (px(cx - 9 * u), px(fy - 76 * u), px(cx + 9 * u), px(fy - 76 * u)),
        fill=(196, 118, 28),
        width=max(1, px(0.9 * u)),
    )


def dollar(base, cx, cy, size, fill, angle):
    """A floating $, drawn on its own layer so it can tilt."""
    fnt = font(mc.SANS_B, size)
    pad = px(size)
    layer = Image.new("RGBA", (pad * 2, pad * 2), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((pad, pad), "$", font=fnt, fill=fill + (255,), anchor="mm")
    layer = layer.rotate(angle, resample=Image.BICUBIC)
    base.alpha_composite(layer, (px(cx) - pad, px(cy) - pad))


def render(a):
    base = Image.new("RGBA", (W * S, H * S), SURFACE + (255,))
    d = ImageDraw.Draw(base)

    # ---- illustration, right of the type -------------------------------
    # A cloud big enough to read as one has to sit BEHIND the bird: a cloud he
    # stands on top of has to end where his feet are, and at that size it reads
    # as a rock. The cloud goes down first, then the penguin over it.
    cloud(d, 1116, 430, 452, CLOUD)
    penguin(d, 1116, 472, 272)

    for cx, cy, size, fill, ang in (
        (1288, 158, 78, ORANGE, -14),
        (886, 248, 62, BLUE, 11),
        (944, 146, 40, ORANGE, -7),
        (1330, 268, 44, BLUE, 9),
        (1012, 112, 32, BLUE, 16),
        (1218, 108, 36, ORANGE, 12),
    ):
        dollar(base, cx, cy, size, fill, ang)

    # ---- type, house style ---------------------------------------------
    pad = 72
    text(d, (pad, 166), "GOOGLE CLOUD  ·  READ-ONLY SCAN", font(mc.MONO, 18), INK_3)
    text(d, (pad, 204), "What Nobody Is Using", font(mc.SANS_B, 58), INK)
    text(d, (pad, 272), "and What It Costs", font(mc.SANS_B, 58), INK)
    text(d, (pad, 356), "20 checks, one call per project,", font(mc.SANS, 27), INK_2)
    text(d, (pad, 392), "every rate from the Billing Catalog API", font(mc.SANS, 27), INK_2)

    d.rectangle((px(pad), px(516), px(W - pad), px(516) + 1), fill=mc.RULE)
    text(
        d,
        (pad, 534),
        "zombiescan  ·  github.com/xbill9/zombiescan-gcp  ·  MIT",
        font(mc.MONO, 18),
        INK_3,
    )

    out = pathlib.Path(a.out)
    base.convert("RGB").resize((W, H), Image.LANCZOS).save(out, quality=92, subsampling=0)
    if a.content_address:
        out = mc.content_address(out)
    print(f"wrote {out}  {W}x{H}  {os.path.getsize(out) // 1024} KB")
    if a.url_base:
        print(f"cover_image: {a.url_base.rstrip('/')}/{out.name}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", required=True)
    p.add_argument("--content-address", action="store_true")
    p.add_argument("--url-base")
    sys.exit(render(p.parse_args()))

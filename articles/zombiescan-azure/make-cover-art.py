#!/usr/bin/env python3
"""Draw this article's cover: a graveyard of dead server racks sinking into a
cloud bank, every cabinet dark except the one still lit.

No figures anywhere on the cover. The numbers live in the article.

Two geometries, one picture. dev.to displays a 2.381:1 crop, so its cover is a
wide band with the type beside the scene. LinkedIn's card is 1200x627, nearly
half a ratio narrower, and the same side-by-side layout squeezes both halves --
so there the type stacks above a scene that runs the full width. Fitting the
dev.to image into LinkedIn's frame instead letterboxes it, which is what
make-linkedin.py does when nothing better is supplied.

The palette, the fonts, the 2x draw-then-downsample and the content-addressed
filename all come from the publishing kit's make-cover.py, imported rather than
copied so a change there reaches this cover too. Only the illustration is local.

    python3 make-cover-art.py --out devto-cover.jpg --content-address \
        --url-base https://raw.githubusercontent.com/xbill9/zombiescan-azure/main/articles/zombiescan-azure
    python3 make-cover-art.py --out linkedin-cover.jpg --mode linkedin
"""

import argparse
import importlib.util
import os
import pathlib
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

KIT = pathlib.Path.home() / "publishing-kit/skills/publishing/scripts/make-cover.py"
spec = importlib.util.spec_from_file_location("make_cover", KIT)
mc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mc)

MODES = {"devto": mc.MODES["devto"], "linkedin": (1200, 627)}
S = 2  # draw at 2x, downsample, or type goes soft

SURFACE = mc.SURFACE
INK, INK_2, INK_3 = mc.INK, mc.INK_2, mc.INK_3
BLUE, ORANGE = mc.COLOURS["blue"], mc.COLOURS["orange"]

# Three fog depths. Each tier of racks is drawn against the band behind it, so
# the contrast between rack and fog is what carries the distance, not scale
# alone -- scale alone reads as "small racks", not "far racks".
FOG_FAR = (63, 65, 70)
FOG_MID = (52, 54, 58)
FOG_NEAR = (41, 43, 47)

RACK_FAR = (57, 58, 62)
RACK_MID = (44, 45, 49)
RACK_NEAR = (23, 24, 27)
RACK_EDGE = (96, 100, 107)
UNIT_LINE = (72, 75, 81)
DEAD_LED = (78, 81, 87)

# Rack heights per tier, as fractions of the tier's nominal height. Fixed rather
# than random so the same call always draws the same cover -- a content-addressed
# filename is meaningless if the bytes move on their own.
FAR_H = (0.78, 1.06, 0.70, 0.96, 0.75, 1.01, 0.73, 0.93, 0.78, 1.01, 0.73, 0.91, 0.75, 0.83)
MID_H = (0.84, 1.04, 0.78, 1.00, 0.83, 1.03, 0.80, 0.97, 0.86)
NEAR_H = (0.83, 1.00, 0.79, 0.86, 0.98, 0.80)


def px(v):
    return int(round(v * S))


def font(path, size):
    return ImageFont.truetype(path, px(size))


def text(d, xy, s, fnt, fill):
    d.text((px(xy[0]), px(xy[1])), s, font=fnt, fill=fill, anchor="la")


def oval(d, cx, cy, w, h, fill):
    d.ellipse((px(cx - w / 2), px(cy - h / 2), px(cx + w / 2), px(cy + h / 2)), fill=fill)


def glow(base, cx, cy, r, colour, strength=92):
    """A soft halo, composited so the lit rack reads as the only live thing."""
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).ellipse(
        (px(cx - r), px(cy - r), px(cx + r), px(cy + r)), fill=colour + (strength,)
    )
    base.alpha_composite(layer.filter(ImageFilter.GaussianBlur(px(r * 0.55))))


def cloud(base, cy, cx, w, h, fill, alpha, blur):
    """A cloud bank: a row of soft ovals, blurred until it has no edge.

    An earlier version laid the ovals on a rounded rectangle, which gave the bank
    a flat bottom that read as a grey bar across the cover rather than as cloud.
    Ovals only, and the blur does the rest.
    """
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for dx, r, dy in (
        (-0.46, 0.30, 0.10), (-0.30, 0.46, -0.06), (-0.14, 0.34, 0.12),
        (0.02, 0.50, -0.10), (0.18, 0.36, 0.08), (0.33, 0.46, -0.04),
        (0.48, 0.30, 0.12),
    ):
        oval(d, cx + w * dx, cy + h * dy, w * r, h * r * 2.1, fill + (alpha,))
    base.alpha_composite(layer.filter(ImageFilter.GaussianBlur(px(blur))))


def rack(d, x, ybase, w, h, body, units=True, lamp=None):
    """One cabinet: a rounded box divided into server units.

    The horizontal divisions are what stop a row of these reading as a city
    skyline -- without them the silhouettes are just towers.
    """
    top = ybase - h
    d.rounded_rectangle(
        (px(x - w / 2 - 1.5), px(top - 1.5), px(x + w / 2 + 1.5), px(ybase)),
        radius=px(4), fill=RACK_EDGE,
    )
    d.rounded_rectangle(
        (px(x - w / 2), px(top), px(x + w / 2), px(ybase)), radius=px(4), fill=body
    )
    if not units:
        return
    n = max(3, int(h / 17))
    gap = (h - 10) / n
    dot = max(1.6, w * (0.058 if lamp and lamp is not dead else 0.045))
    for i in range(n):
        y = top + 7 + i * gap
        d.rectangle(
            (px(x - w / 2 + 4), px(y + gap * 0.72), px(x + w / 2 - 4), px(y + gap * 0.72) + 1),
            fill=UNIT_LINE,
        )
        for k in (0, 1):
            cx = x - w / 2 + 9 + k * dot * 2.6
            oval(d, cx, y + gap * 0.30, dot, dot, lamp(i, k) if lamp else DEAD_LED)


def dead(i, k):
    return DEAD_LED


def alive(i, k):
    return (255, 170, 84) if (i + k) % 3 else ORANGE


def spread(x0, x1, n):
    """n rack centres across a span, overshooting both edges so the row bleeds."""
    step = (x1 - x0) / (n - 1)
    return [x0 + i * step for i in range(n)]


def scene(base, x0, x1, baseline, k, lit_at=0.52):
    """The rack graveyard, laid out across a span and scaled by k.

    lit_at is where the one live cabinet sits, as a fraction of the span. It is
    kept off centre: dead centre reads as a diagram, off centre as a photograph.
    """
    d = ImageDraw.Draw(base)
    cx, w = (x0 + x1) / 2, x1 - x0

    # ---- sky: cold moonlight behind the bank, the only light up there -------
    glow(base, x0 + w * 0.80, baseline - 320 * k, 170 * k, (86, 102, 122), 40)
    glow(base, x0 + w * 0.80, baseline - 320 * k, 78 * k, (116, 134, 156), 34)
    d = ImageDraw.Draw(base)

    # ---- far tier: silhouettes, no detail, lost in the haze ----------------
    far_base = baseline - 118 * k
    for x, hf in zip(spread(x0 - 20, x1 + 20, len(FAR_H)), FAR_H):
        rack(d, x, far_base, 32 * k, 74 * k * hf, RACK_FAR, units=False)
    cloud(base, far_base - 2, cx, w * 1.16, 48 * k, FOG_FAR, 175, 12 * k)
    d = ImageDraw.Draw(base)

    # ---- mid tier ----------------------------------------------------------
    mid_base = baseline - 62 * k
    for x, hf in zip(spread(x0 - 30, x1 + 30, len(MID_H)), MID_H):
        rack(d, x, mid_base, 54 * k, 128 * k * hf, RACK_MID)
    cloud(base, mid_base - 2, cx, w * 1.20, 54 * k, FOG_MID, 195, 13 * k)
    d = ImageDraw.Draw(base)

    # ---- near tier: the dead cabinets sink into the bank -------------------
    for x, hf in zip(spread(x0 - 40, x1 + 40, len(NEAR_H)), NEAR_H):
        rack(d, x, baseline, 78 * k, 190 * k * hf, RACK_NEAR, lamp=dead)
    cloud(base, baseline - 2, cx, w * 1.26, 60 * k, FOG_NEAR, 215, 14 * k)

    # ---- the one still lit, drawn in FRONT of the bank ---------------------
    # Behind it, the fog washes the halo out to a brown smear; in front, it is
    # the only thing on the cover with any colour in it, which is the point.
    lx, lh = x0 + w * lit_at, 204 * k
    glow(base, lx, baseline - lh * 0.50, lh * 0.60, ORANGE, 96)
    glow(base, lx, baseline - 12 * k, 76 * k, ORANGE, 64)
    d = ImageDraw.Draw(base)
    rack(d, lx, baseline, 78 * k, lh, RACK_NEAR, lamp=alive)
    cloud(base, baseline + 4 * k, lx, 300 * k, 34 * k, FOG_NEAR, 120, 12 * k)


def scrim_left(base, W, H, upto, power=0.80):
    """Hold the left side dark so the type over it stays readable."""
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for i in range(px(upto)):
        a = int(255 * max(0.0, 1.0 - (i / px(upto)) ** power))
        d.line((i, 0, i, H * S), fill=SURFACE + (a,))
    base.alpha_composite(layer)


def scrim_top(base, W, H, upto, power=0.90):
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for i in range(px(upto)):
        a = int(255 * max(0.0, 1.0 - (i / px(upto)) ** power))
        d.line((0, i, W * S, i), fill=SURFACE + (a,))
    base.alpha_composite(layer)


EYEBROW = "AZURE  ·  READ-ONLY SCAN"
HEAD = ("What Nobody Is Using", "and What It Costs")
SUB = ("Find the resources still billing,", "priced from the Azure Retail Prices API")
FOOT = "zombiescan  ·  github.com/xbill9/zombiescan-azure  ·  MIT"


def scene_devto(base):
    """The dev.to scene, at the exact positions its published cover was drawn from.

    The same hand-placed layout as the Google Cloud article's cover, so the
    series reads as one set; only the words differ.
    """
    d = ImageDraw.Draw(base)
    glow(base, 1228, 150, 170, (86, 102, 122), 40)
    glow(base, 1228, 150, 78, (116, 134, 156), 34)
    d = ImageDraw.Draw(base)

    for x, h in ((706, 60), (752, 82), (800, 54), (850, 74), (902, 58), (956, 78),
                 (1010, 56), (1064, 72), (1118, 60), (1174, 78), (1230, 56),
                 (1286, 70), (1342, 58), (1396, 64)):
        rack(d, x, 352, 32, h, RACK_FAR, units=False)
    cloud(base, 350, 1060, 860, 48, FOG_FAR, 175, 12)
    d = ImageDraw.Draw(base)

    for x, h in ((688, 108), (772, 134), (860, 100), (950, 128), (1042, 106),
                 (1134, 132), (1228, 102), (1322, 124), (1404, 110)):
        rack(d, x, 408, 54, h, RACK_MID)
    cloud(base, 406, 1060, 900, 54, FOG_MID, 195, 13)
    d = ImageDraw.Draw(base)

    for x, h in ((684, 158), (790, 190), (906, 150),
                 (1150, 164), (1274, 186), (1392, 152)):
        rack(d, x, 470, 78, h, RACK_NEAR, lamp=dead)
    cloud(base, 468, 1060, 960, 60, FOG_NEAR, 215, 14)

    lx, lh = 1026, 204
    glow(base, lx, 470 - lh * 0.50, lh * 0.60, ORANGE, 96)
    glow(base, lx, 458, 76, ORANGE, 64)
    d = ImageDraw.Draw(base)
    rack(d, lx, 470, 78, lh, RACK_NEAR, lamp=alive)
    cloud(base, 474, lx, 300, 34, FOG_NEAR, 120, 12)


def render_devto(base, W, H):
    scene_devto(base)
    scrim_left(base, W, H, 780)
    d = ImageDraw.Draw(base)
    pad = 72
    text(d, (pad, 166), EYEBROW, font(mc.MONO, 18), INK_3)
    text(d, (pad, 204), HEAD[0], font(mc.SANS_B, 58), INK)
    text(d, (pad, 272), HEAD[1], font(mc.SANS_B, 58), INK)
    text(d, (pad, 356), SUB[0], font(mc.SANS, 27), INK_2)
    text(d, (pad, 392), SUB[1], font(mc.SANS, 27), INK_2)
    d.rectangle((px(pad), px(516), px(W - pad), px(516) + 1), fill=mc.RULE)
    text(d, (pad, 534), FOOT, font(mc.MONO, 18), INK_3)


def render_linkedin(base, W, H):
    """Type stacked over a scene that runs the full width.

    The card is 1.914:1 against dev.to's 2.381:1. Side by side at this width the
    headline wraps to three lines and the scene loses half its depth, so the
    layout turns rather than being squeezed.
    """
    scene(base, -40, W + 40, 566, 0.95, lit_at=0.60)
    scrim_top(base, W, H, 400, 1.05)
    d = ImageDraw.Draw(base)
    pad = 66
    text(d, (pad, 74), EYEBROW, font(mc.MONO, 19), INK_3)
    text(d, (pad, 116), HEAD[0], font(mc.SANS_B, 60), INK)
    text(d, (pad, 186), HEAD[1], font(mc.SANS_B, 60), INK)
    text(d, (pad, 274), SUB[0], font(mc.SANS, 27), INK_2)
    text(d, (pad, 310), SUB[1], font(mc.SANS, 27), INK_2)


def render(a):
    W, H = MODES[a.mode]
    base = Image.new("RGBA", (W * S, H * S), SURFACE + (255,))
    (render_devto if a.mode == "devto" else render_linkedin)(base, W, H)

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
    p.add_argument("--mode", choices=sorted(MODES), default="devto")
    p.add_argument("--content-address", action="store_true")
    p.add_argument("--url-base")
    sys.exit(render(p.parse_args()))

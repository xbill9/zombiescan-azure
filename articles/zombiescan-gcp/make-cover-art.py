#!/usr/bin/env python3
"""Draw this article's cover: house typography on the left, a graveyard of dead
server racks sinking into a cloud bank on the right.

Racks stand like headstones in three depth tiers, each tier paler and smaller
than the one in front so the fog reads as distance. Every rack but one has dark
status lights. The one still lit is the point of the article: the resource
nobody uses and everybody pays for.

No figures anywhere on the cover. The numbers live in the article.

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

from PIL import Image, ImageDraw, ImageFilter, ImageFont

KIT = pathlib.Path.home() / "publishing-kit/skills/publishing/scripts/make-cover.py"
spec = importlib.util.spec_from_file_location("make_cover", KIT)
mc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mc)

W, H = mc.MODES["devto"]
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
DEAD_LED = (78, 81, 87)
UNIT_LINE = (72, 75, 81)


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


def render(a):
    base = Image.new("RGBA", (W * S, H * S), SURFACE + (255,))

    # ---- sky: cold moonlight behind the bank, the only light up there -------
    glow(base, 1228, 150, 170, (86, 102, 122), 40)
    glow(base, 1228, 150, 78, (116, 134, 156), 34)
    d = ImageDraw.Draw(base)

    # ---- far tier: silhouettes, no detail, lost in the haze ----------------
    for x, h in ((706, 60), (752, 82), (800, 54), (850, 74), (902, 58), (956, 78),
                 (1010, 56), (1064, 72), (1118, 60), (1174, 78), (1230, 56),
                 (1286, 70), (1342, 58), (1396, 64)):
        rack(d, x, 352, 32, h, RACK_FAR, units=False)
    cloud(base, 350, 1060, 860, 48, FOG_FAR, 175, 12)
    d = ImageDraw.Draw(base)

    # ---- mid tier ----------------------------------------------------------
    for x, h in ((688, 108), (772, 134), (860, 100), (950, 128),
                 (1042, 106), (1134, 132), (1228, 102), (1322, 124), (1404, 110)):
        rack(d, x, 408, 54, h, RACK_MID)
    cloud(base, 406, 1060, 900, 54, FOG_MID, 195, 13)
    d = ImageDraw.Draw(base)

    # ---- near tier: the dead cabinets sink into the bank ------------------
    near = ((684, 158), (790, 190), (906, 150), (1150, 164), (1274, 186), (1392, 152))
    for x, h in near:
        rack(d, x, 470, 78, h, RACK_NEAR, lamp=dead)
    cloud(base, 468, 1060, 960, 60, FOG_NEAR, 215, 14)

    # ---- the one still lit, drawn in FRONT of the bank ---------------------
    # Behind it, the fog washes the halo out to a brown smear; in front, it is
    # the only thing on the cover with any colour in it, which is the point.
    lx, lh = 1026, 204
    glow(base, lx, 470 - lh * 0.50, lh * 0.60, ORANGE, 96)
    glow(base, lx, 458, 76, ORANGE, 64)
    d = ImageDraw.Draw(base)
    rack(d, lx, 470, 78, lh, RACK_NEAR, lamp=alive)
    cloud(base, 474, lx, 300, 34, FOG_NEAR, 120, 12)
    d = ImageDraw.Draw(base)

    # ---- scrim: hold the type side dark so the headline stays readable ------
    scrim = Image.new("RGBA", base.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(scrim)
    for i in range(px(780)):
        alpha = int(255 * max(0.0, 1.0 - (i / px(780)) ** 0.80))
        sd.line((i, 0, i, H * S), fill=SURFACE + (alpha,))
    base.alpha_composite(scrim)
    d = ImageDraw.Draw(base)

    # ---- type, house style -------------------------------------------------
    pad = 72
    text(d, (pad, 166), "GOOGLE CLOUD  \u00b7  READ-ONLY SCAN", font(mc.MONO, 18), INK_3)
    text(d, (pad, 204), "What Nobody Is Using", font(mc.SANS_B, 58), INK)
    text(d, (pad, 272), "and What It Costs", font(mc.SANS_B, 58), INK)
    text(d, (pad, 356), "Find the resources still billing,", font(mc.SANS, 27), INK_2)
    text(d, (pad, 392), "priced from the Billing Catalog API", font(mc.SANS, 27), INK_2)

    d.rectangle((px(pad), px(516), px(W - pad), px(516) + 1), fill=mc.RULE)
    text(d, (pad, 534), "zombiescan  \u00b7  github.com/xbill9/zombiescan-gcp  \u00b7  MIT",
         font(mc.MONO, 18), INK_3)

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

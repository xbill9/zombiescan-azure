#!/usr/bin/env python3
"""Draw this article's cover: house typography on the left, zombie hands
reaching out of cloud shapes for dollar signs on the right.

The palette, the fonts, the 2x draw-then-downsample and the content-addressed
filename all come from the publishing kit's make-cover.py, imported rather than
copied so a change there reaches this cover too. Only the illustration is local.

    python3 make-zombie-cover.py --out devto-cover.jpg --content-address \
        --url-base https://raw.githubusercontent.com/xbill9/zombiescan-gcp/main/articles/zombiescan-gcp
"""

import argparse
import importlib.util
import os
import pathlib
import sys

from PIL import Image, ImageDraw

KIT = pathlib.Path.home() / "publishing-kit/skills/publishing/scripts/make-cover.py"
spec = importlib.util.spec_from_file_location("make_cover", KIT)
mc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mc)

W, H = mc.MODES["devto"]
S = 2  # draw at 2x, downsample, or type goes soft

SURFACE = mc.SURFACE
INK, INK_2, INK_3 = mc.INK, mc.INK_2, mc.INK_3
BLUE, ORANGE = mc.COLOURS["blue"], mc.COLOURS["orange"]

CLOUD = (52, 52, 49)  # far cloud, one step off the rule
CLOUD_HI = (72, 72, 68)  # near cloud, drawn last
SKIN = (124, 148, 106)  # muted zombie green
SKIN_HI = (146, 170, 124)
SKIN_LO = (92, 112, 78)
SLEEVE = (44, 44, 48)


def px(v):
    return int(round(v * S))


def font(path, size):
    from PIL import ImageFont

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


def rrect(d, box, radius, fill):
    d.rounded_rectangle([px(v) for v in box], radius=px(radius), fill=fill)


def cloud(d, cx, cy, w, fill, hi=None):
    """Flat cloud silhouette: a row of circles over a capsule base."""
    h = w * 0.42
    d.rounded_rectangle(
        (px(cx - w / 2), px(cy - h * 0.32), px(cx + w / 2), px(cy + h * 0.32)),
        radius=px(h * 0.32),
        fill=fill,
    )
    for dx, r in ((-0.26, 0.30), (0.02, 0.42), (0.28, 0.26)):
        rr = w * r / 2
        d.ellipse(
            (
                px(cx + w * dx - rr),
                px(cy - h * 0.18 - rr),
                px(cx + w * dx + rr),
                px(cy - h * 0.18 + rr),
            ),
            fill=fill,
        )
    if hi:
        d.rounded_rectangle(
            (px(cx - w * 0.40), px(cy + h * 0.20), px(cx + w * 0.40), px(cy + h * 0.30)),
            radius=px(h * 0.08),
            fill=hi,
        )


def hand(d, cx, wrist, k, reach=1.0, lean=0.0):
    """A cartoon hand and forearm rising, grasping upward.

    cx/wrist locate the wrist, k scales the whole thing, reach lengthens the
    fingers and lean tilts the arm so a row of them does not read as a comb.
    """

    def P(dx, dy):
        return (cx + dx * k + dy * k * lean * -0.35, wrist + dy * k)

    capsule(d, P(0, 86), P(0, 6), 26 * k, SKIN)  # forearm
    rrect(
        d, (P(-19, 44)[0], P(0, 44)[1], P(19, 80)[0], P(0, 82)[1]), 6 * k, SLEEVE
    )  # tattered cuff
    for i in range(4):  # ragged cuff edge
        x = P(-19 + i * 12.6, 44)[0]
        d.polygon(
            [
                (px(x), px(P(0, 44)[1])),
                (px(x + 6.3 * k), px(P(0, 44)[1] - 7 * k)),
                (px(x + 12.6 * k), px(P(0, 44)[1])),
            ],
            fill=SLEEVE,
        )

    rrect(d, (P(-24, -30)[0], P(0, -30)[1], P(24, 10)[0], P(0, 10)[1]), 11 * k, SKIN)  # palm

    fingers = ((-17, -24, -23, -74), (-6, -26, -8, -90), (6, -26, 10, -82), (17, -22, 24, -60))
    for fx, fy, tx, ty in fingers:
        capsule(d, P(fx, fy), P(tx, fy + (ty - fy) * reach), 12 * k, SKIN)
        capsule(
            d, P(tx, fy + (ty - fy) * reach - 1), P(tx, fy + (ty - fy) * reach - 2), 12 * k, SKIN_HI
        )
    capsule(d, P(-21, -4), P(-43, -32), 13 * k, SKIN)  # thumb

    for sy in (18, 34):  # stitches
        capsule(d, P(-9, sy), P(9, sy), 3 * k, SKIN_LO)
        for sx in (-7, 0, 7):
            capsule(d, P(sx, sy - 4), P(sx, sy + 4), 2.5 * k, SKIN_LO)


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
    # Arms first, clouds over them: a hand has to come OUT of the cloud, and
    # z-order is the only thing saying so. Every forearm ends above y=505, so
    # nothing reaches the footer rule even where a cloud does not cover it.
    hand(d, 866, 390, 1.05, reach=1.00, lean=-0.35)
    hand(d, 1080, 360, 1.25, reach=1.10, lean=0.10)
    hand(d, 1286, 394, 0.95, reach=0.92, lean=0.45)

    # Three separate clouds, not one grey mass: a gap between them and the
    # nearer one a shade lighter is what keeps them reading as cloud.
    cloud(d, 860, 472, 198, CLOUD)
    cloud(d, 1292, 470, 200, CLOUD)
    cloud(d, 1080, 462, 250, CLOUD_HI)

    for cx, cy, size, fill, ang in (
        (1088, 176, 92, ORANGE, -12),
        (958, 246, 58, BLUE, 14),
        (1216, 232, 66, BLUE, -20),
        (1300, 132, 42, ORANGE, 8),
        (886, 150, 38, BLUE, -6),
        (1148, 300, 34, ORANGE, 18),
    ):
        dollar(base, cx, cy, size, fill, ang)

    # ---- type, house style ---------------------------------------------
    pad = 72
    text(d, (pad, 46), "GOOGLE CLOUD  ·  READ-ONLY SCAN", font(mc.MONO, 18), INK_3)
    text(d, (pad, 92), "What Nobody Is Using", font(mc.SANS_B, 54), INK)
    text(d, (pad, 156), "and What It Costs", font(mc.SANS_B, 54), INK)
    text(d, (pad, 236), "20 checks, one call per project,", font(mc.SANS, 27), INK_2)
    text(d, (pad, 272), "every rate from the Billing Catalog API", font(mc.SANS, 27), INK_2)

    # two figures, carried over from the stat-tile cover this replaces
    y = 352
    d.rectangle((px(pad), px(y), px(pad + 4), px(y + 86)), fill=BLUE)
    text(d, (pad + 22, y - 2), "713", font(mc.SANS_B, 52), INK)
    text(d, (pad + 22, y + 60), "findings, 75 projects", font(mc.SANS, 21), INK_3)

    x2 = pad + 250
    d.rectangle((px(x2), px(y), px(x2 + 4), px(y + 86)), fill=ORANGE)
    text(d, (x2 + 22, y - 2), "$387.74", font(mc.SANS_B, 52), INK)
    text(d, (x2 + 22, y + 60), "per month, list price", font(mc.SANS, 21), INK_3)

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

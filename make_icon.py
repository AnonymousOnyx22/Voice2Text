"""Draw the Voice2Text mark and write icon.ico / logo.png.

The mark is the same thin microphone the panel draws, on the panel's own
rounded background, so the taskbar icon and the app read as one thing.
"""
from PIL import Image, ImageDraw

BG = (38, 36, 31)          # panel background
RING = (59, 56, 49)        # panel border
MIC = (222, 217, 209)      # primary text
ACCENT = (217, 112, 90)    # recording red

SIZES = [16, 24, 32, 48, 64, 128, 256]
SS = 8                     # supersample factor for smooth edges


def draw(size):
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # rounded panel
    r = int(s * 0.22)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=r, fill=BG,
                        outline=RING, width=max(1, int(s * 0.012)))

    cx = s / 2
    w = max(1, int(s * 0.042))          # stroke weight

    # capsule
    cap_w, cap_h = s * 0.175, s * 0.30
    cap_top = s * 0.26
    d.rounded_rectangle([cx - cap_w / 2, cap_top,
                         cx + cap_w / 2, cap_top + cap_h],
                        radius=cap_w / 2, fill=MIC)

    # cradle arc, hugging the capsule
    ar = s * 0.155
    arc_cy = cap_top + cap_h * 0.72
    d.arc([cx - ar, arc_cy - ar, cx + ar, arc_cy + ar],
          start=20, end=160, fill=MIC, width=w)

    # stem and base
    stem_top = arc_cy + ar - w / 2
    base_y = stem_top + s * 0.085
    d.line([cx, stem_top, cx, base_y], fill=MIC, width=w)
    d.line([cx - s * 0.07, base_y, cx + s * 0.07, base_y], fill=MIC, width=w)

    # live dot in the corner, so the mark reads as "recording"
    if size >= 32:
        dr = s * 0.042
        dx, dy = s * 0.775, s * 0.225
        d.ellipse([dx - dr, dy - dr, dx + dr, dy + dr], fill=ACCENT)

    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    frames = [draw(n) for n in SIZES]
    frames[-1].save("icon.ico", sizes=[(n, n) for n in SIZES])
    draw(256).save("logo.png")

    banner = Image.new("RGBA", (1280, 420), BG)
    b = ImageDraw.Draw(banner)
    b.rounded_rectangle([0, 0, 1279, 419], radius=28, fill=BG, outline=RING,
                        width=2)
    mark = draw(256).resize((150, 150), Image.LANCZOS)
    banner.alpha_composite(mark, (100, 135))
    banner.save("banner.png")
    print("wrote icon.ico, logo.png, banner.png")

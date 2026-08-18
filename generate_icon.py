#!/usr/bin/env python3
"""
Generate PNG icons from icon.svg — required for iOS home screen.
iOS does NOT support SVG for apple-touch-icon; it needs a real PNG.

Usage (run on VPS):
    pip install cairosvg --break-system-packages
    python generate_icon.py

Outputs:
    static/icon.png      (180×180 — apple-touch-icon)
    static/icon-192.png  (192×192 — Android / Chrome PWA)
    static/icon-512.png  (512×512 — Splash screen)
"""
import os, sys

BASE = os.path.dirname(__file__)
SVG  = os.path.join(BASE, 'static', 'icon.svg')

def gen_with_cairosvg():
    import cairosvg
    for size, name in [(180, 'icon.png'), (192, 'icon-192.png'), (512, 'icon-512.png')]:
        out = os.path.join(BASE, 'static', name)
        cairosvg.svg2png(url=SVG, write_to=out, output_width=size, output_height=size)
        print(f'  ✓ {out}')

def gen_fallback():
    """
    Pillow fallback — draws a gold chart-line on dark background.
    Not pixel-perfect but good enough for a home screen icon.
    """
    from PIL import Image, ImageDraw

    BG    = (3, 6, 16)        # #030610 dark navy
    GOLD  = (240, 165, 0)     # #F0A500
    LIGHT = (255, 209, 102)   # #FFD166

    for size, name in [(180, 'icon.png'), (192, 'icon-192.png'), (512, 'icon-512.png')]:
        img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        d   = ImageDraw.Draw(img)

        r   = size * 0.218  # corner radius ratio from SVG (112/512)
        d.rounded_rectangle([(0, 0), (size, size)], radius=r, fill=BG)

        # Orbit ring (dashed circle approximation — just draw the arc)
        margin = size * 0.07
        d.arc([margin, margin, size - margin, size - margin],
              start=-90, end=90, fill=GOLD, width=max(2, size // 50))

        # Chart trend line: 4 points (92,378 168,256 252,308 392,148) normalized to 512
        scale = size / 512
        pts   = [(92, 378), (168, 256), (252, 308), (392, 148)]
        pts_s = [(int(x * scale), int(y * scale)) for x, y in pts]
        lw    = max(3, size // 25)
        d.line(pts_s, fill=GOLD, width=lw, joint='curve')

        # Peak dot
        cx, cy = pts_s[-1]
        r2 = max(4, size // 18)
        d.ellipse([cx - r2, cy - r2, cx + r2, cy + r2], fill=LIGHT)

        out = os.path.join(BASE, 'static', name)
        img.save(out, 'PNG')
        print(f'  ✓ {out}  (Pillow fallback)')

print('Generating AutoCycle PNG icons...')
try:
    gen_with_cairosvg()
    print('Done — using cairosvg (pixel-perfect SVG render).')
except ImportError:
    print('cairosvg not found — trying Pillow...')
    try:
        gen_fallback()
        print('Done — using Pillow (approximate render).')
        print('For pixel-perfect: pip install cairosvg --break-system-packages && python generate_icon.py')
    except ImportError:
        print('Neither cairosvg nor Pillow found.')
        print('Run: pip install cairosvg --break-system-packages')
        sys.exit(1)

#!/usr/bin/python3
"""Render a clean, uniform badge tile from an original photo.

cut-out (rembg) -> trim to the badge -> center on the site's dark tile with a soft shadow.
Usage: render_badge.py ORIGINAL OUT.jpg [--model isnet-general-use] [--size 720]
"""
import argparse, io
from PIL import Image, ImageFilter, ImageOps, ImageDraw
from rembg import remove, new_session

BG = (23, 26, 30)       # matches --garage-2 tile colour on the site
_sessions = {}

def cutout(img, model):
    if model not in _sessions:
        _sessions[model] = new_session(model)
    small = img.copy(); small.thumbnail((1600, 1600))
    return remove(small, session=_sessions[model], post_process_mask=True).convert("RGBA")

def tile(rgba, size=720, fill=0.80):
    a = rgba.getchannel("A").point(lambda v: 255 if v > 40 else 0)
    box = a.getbbox()
    if not box:
        raise ValueError("empty mask")
    obj = rgba.crop(box)
    w, h = obj.size; s = fill * size / max(w, h)
    obj = obj.resize((max(1, round(w * s)), max(1, round(h * s))), Image.LANCZOS)
    # background: subtle radial vignette so tiles feel lit, not flat
    bg = Image.new("RGB", (size, size), BG)
    glow = Image.new("L", (size, size), 0); ImageDraw.Draw(glow).ellipse((size*.12, size*.08, size*.88, size*.84), fill=34)
    glow = glow.filter(ImageFilter.GaussianBlur(size * .12))
    bg = Image.composite(Image.new("RGB", (size, size), (70, 78, 88)), bg, glow)
    x, y = (size - obj.width) // 2, (size - obj.height) // 2
    shadow = Image.new("L", (size, size), 0); shadow.paste(obj.getchannel("A"), (x, y + size // 40))
    shadow = shadow.filter(ImageFilter.GaussianBlur(size // 45)).point(lambda v: v * 0.55)
    bg.paste((0, 0, 0), (0, 0), shadow)
    bg.paste(obj, (x, y), obj)
    return bg

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("src"); ap.add_argument("out")
    ap.add_argument("--model", default="isnet-general-use"); ap.add_argument("--size", type=int, default=720)
    a = ap.parse_args()
    img = ImageOps.exif_transpose(Image.open(a.src)).convert("RGB")
    tile(cutout(img, a.model), a.size).save(a.out, quality=86, optimize=True, progressive=True)

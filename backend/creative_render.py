"""Deterministic 3:4 Xiaohongshu artwork renderer.

The model supplies a constrained design system and page content.  This module
turns that specification into real PNG assets so the preview and download are
the same deliverable instead of a decorative placeholder.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageColor, ImageDraw, ImageFont

from . import db

WIDTH, HEIGHT = 1080, 1440
SAFE_X = 76


def _font(size: int, bold: bool = False):
    candidates = [
        Path("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf" if bold else "C:/Windows/Fonts/simsun.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _color(value, fallback):
    try:
        value = str(value or "").strip()
        if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            return ImageColor.getrgb(value)
    except (TypeError, ValueError):
        pass
    return ImageColor.getrgb(fallback)


def _mix(a, b, amount):
    return tuple(round(a[i] * (1 - amount) + b[i] * amount) for i in range(3))


def _contrast(rgb):
    luminance = sum(channel * weight for channel, weight in zip(rgb, (.299, .587, .114)))
    return (26, 29, 38) if luminance > 165 else (250, 251, 255)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int, max_lines: int | None = None):
    text = str(text or "").strip()
    if not text:
        return []
    lines, current = [], ""
    for char in text:
        if char=='\n':
            if current:lines.append(current);current=""
            if max_lines and len(lines)>=max_lines:break
            continue
        candidate = current + char
        if current and draw.textlength(candidate, font=font) > max_width:
            lines.append(current)
            current = char
            if max_lines and len(lines) >= max_lines:
                break
        else:
            current = candidate
    if current and (not max_lines or len(lines) < max_lines):
        lines.append(current)
    if max_lines and len(lines) == max_lines and sum(len(x) for x in lines) < len(text):
        lines[-1] = lines[-1][:-1] + "…"
    return lines


def _text(draw, xy, text, font, fill, max_width, line_gap=12, max_lines=None):
    x, y = xy
    lines = _wrap(draw, text, font, max_width, max_lines)
    box = font.getbbox("国Ag")
    line_height = box[3] - box[1]
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += line_height + line_gap
    return y


def _rounded(draw, box, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _gradient(background, secondary):
    image = Image.new("RGB", (WIDTH, HEIGHT), background)
    pixels = image.load()
    for y in range(HEIGHT):
        color = _mix(background, secondary, y / max(HEIGHT - 1, 1))
        for x in range(WIDTH):
            pixels[x, y] = color
    return image


def _brand(draw, palette, page_no, total):
    primary, text = palette["primary"], palette["text"]
    _rounded(draw, (SAFE_X, 62, SAFE_X + 192, 112), 25, primary)
    draw.text((SAFE_X + 20, 75), "向阳AI · 实战", font=_font(22, True), fill=_contrast(primary))
    draw.text((WIDTH - SAFE_X - 98, 76), f"{page_no:02d}/{total:02d}", font=_font(24, True), fill=text)


def _decorations(draw, palette, seed):
    accent, primary = palette["accent"], palette["primary"]
    draw.ellipse((WIDTH - 260, -120, WIDTH + 110, 250), fill=(*accent, 42))
    draw.ellipse((-160, HEIGHT - 250, 220, HEIGHT + 130), fill=(*primary, 32))
    if seed % 2:
        for x in range(70, WIDTH, 88):
            draw.line((x, 0, x, HEIGHT), fill=(*primary, 12), width=1)
        for y in range(160, HEIGHT, 88):
            draw.line((0, y, WIDTH, y), fill=(*primary, 12), width=1)


def _draw_cover(draw, page, palette):
    text, muted, primary, accent, surface = (palette[k] for k in ("text", "muted", "primary", "accent", "surface"))
    eyebrow = page.get("eyebrow") or "一篇讲透"
    draw.text((SAFE_X, 190), eyebrow, font=_font(28, True), fill=primary)
    y = _text(draw, (SAFE_X, 245), page.get("heading"), _font(94, True), text, WIDTH - SAFE_X * 2, 14, 4)
    y = _text(draw, (SAFE_X, y + 24), page.get("subheading"), _font(36), muted, WIDTH - SAFE_X * 2, 12, 3)
    panel_y = max(760, y + 70)
    bullets = [str(x) for x in page.get("bullets", []) if str(x).strip()][:4]
    if not bullets:
        bullets = ["核心功能", "真实场景", "上手方法"]
    if len(bullets)<=3:
        motif=str(page.get('icon') or '</>')
        if len(motif)>4 or not motif.isascii():motif='</>'
        draw.text((WIDTH-SAFE_X-20,panel_y-18),motif,font=_font(190,True),fill=(*primary,42),anchor='ra')
        yy=panel_y+120
        for i,bullet in enumerate(bullets):
            _rounded(draw,(SAFE_X,yy,WIDTH-SAFE_X,yy+104),28,(*surface,238),(*primary,65),2)
            draw.text((SAFE_X+26,yy+26),f'{i+1:02d}',font=_font(27,True),fill=accent)
            _text(draw,(SAFE_X+98,yy+23),bullet,_font(31,True),text,WIDTH-SAFE_X*2-125,6,2)
            yy+=126
        callout=str(page.get('callout') or '').strip()
        if callout:
            draw.line((SAFE_X,HEIGHT-154,SAFE_X+110,HEIGHT-154),fill=primary,width=8)
            _text(draw,(SAFE_X,HEIGHT-128),callout,_font(29,True),text,WIDTH-SAFE_X*2,6,2)
        return
    _rounded(draw, (SAFE_X, panel_y, WIDTH - SAFE_X, HEIGHT - 120), 42, (*surface, 238), (*primary, 70), 2)
    draw.rectangle((SAFE_X + 32, panel_y + 32, WIDTH - SAFE_X - 32, panel_y + 94), fill=(*text, 235))
    for i, color in enumerate((accent, primary, (245, 192, 70))):
        draw.ellipse((SAFE_X + 55 + i * 42, panel_y + 51, SAFE_X + 75 + i * 42, panel_y + 71), fill=color)
    card_y = panel_y + 132
    card_width = (WIDTH - SAFE_X * 2 - 96) // 2
    for i, bullet in enumerate(bullets):
        row, col = divmod(i, 2)
        x, yy = SAFE_X + 32 + col * (card_width + 32), card_y + row * 142
        _rounded(draw, (x, yy, x + card_width, yy + 112), 22, (*_mix(surface, primary, .06), 255))
        draw.text((x + 22, yy + 18), f"0{i + 1}", font=_font(24, True), fill=accent)
        _text(draw, (x + 78, yy + 17), bullet, _font(28, True), text, card_width - 98, 5, 2)


def _draw_list(draw, page, palette, timeline=False):
    text, muted, primary, accent, surface = (palette[k] for k in ("text", "muted", "primary", "accent", "surface"))
    y = _text(draw, (SAFE_X, 180), page.get("heading"), _font(68, True), text, WIDTH - SAFE_X * 2, 10, 3)
    y = _text(draw, (SAFE_X, y + 15), page.get("subheading"), _font(31), muted, WIDTH - SAFE_X * 2, 9, 3) + 44
    bullets = [str(x) for x in page.get("bullets", []) if str(x).strip()][:5]
    if not bullets:
        bullets = [page.get("callout") or "把这一页的重点说具体"]
    available = HEIGHT - y - 145
    height = min(220, max(126, int((available - 20 * (len(bullets) - 1)) / len(bullets))))
    if timeline:
        draw.line((SAFE_X + 35, y + 28, SAFE_X + 35, y + (height + 20) * len(bullets) - 40), fill=(*primary, 120), width=5)
    for i, bullet in enumerate(bullets):
        yy = y + i * (height + 20)
        left = SAFE_X + (76 if timeline else 0)
        if timeline:
            draw.ellipse((SAFE_X + 16, yy + 18, SAFE_X + 54, yy + 56), fill=accent)
        _rounded(draw, (left, yy, WIDTH - SAFE_X, yy + height), 28, (*surface, 235), (*primary, 45), 2)
        draw.text((left + 26, yy + 22), f"{i + 1:02d}", font=_font(28, True), fill=accent)
        _text(draw, (left + 88, yy + 20), bullet, _font(31, True), text, WIDTH - SAFE_X - left - 116, 8, 3)
    callout = str(page.get("callout") or "").strip()
    if callout:
        _rounded(draw, (SAFE_X, HEIGHT - 128, WIDTH - SAFE_X, HEIGHT - 70), 29, primary)
        _text(draw, (SAFE_X + 24, HEIGHT - 115), callout, _font(24, True), _contrast(primary), WIDTH - SAFE_X * 2 - 48, 4, 1)


def _draw_grid(draw, page, palette):
    text, muted, primary, accent, surface = (palette[k] for k in ("text", "muted", "primary", "accent", "surface"))
    y = _text(draw, (SAFE_X, 180), page.get("heading"), _font(66, True), text, WIDTH - SAFE_X * 2, 10, 3)
    y = _text(draw, (SAFE_X, y + 15), page.get("subheading"), _font(30), muted, WIDTH - SAFE_X * 2, 8, 3) + 48
    bullets = [str(x) for x in page.get("bullets", []) if str(x).strip()][:4] or ["重点一", "重点二", "重点三", "重点四"]
    gap = 24
    card_w = (WIDTH - SAFE_X * 2 - gap) // 2
    card_h = min(320, int((HEIGHT - y - 130 - gap) / 2))
    for i, bullet in enumerate(bullets):
        row, col = divmod(i, 2)
        x, yy = SAFE_X + col * (card_w + gap), y + row * (card_h + gap)
        _rounded(draw, (x, yy, x + card_w, yy + card_h), 34, (*surface, 238), (*primary, 55), 2)
        draw.ellipse((x + 28, yy + 28, x + 86, yy + 86), fill=accent if i % 2 == 0 else primary)
        draw.text((x + 48, yy + 42), str(i + 1), font=_font(24, True), fill=_contrast(accent if i % 2 == 0 else primary), anchor="mm")
        _text(draw, (x + 28, yy + 116), bullet, _font(34, True), text, card_w - 56, 9, 4)


def _draw_comparison(draw, page, palette):
    text, muted, primary, accent, surface = (palette[k] for k in ("text", "muted", "primary", "accent", "surface"))
    y = _text(draw, (SAFE_X, 180), page.get("heading"), _font(65, True), text, WIDTH - SAFE_X * 2, 10, 3)
    y = _text(draw, (SAFE_X, y + 14), page.get("subheading"), _font(30), muted, WIDTH - SAFE_X * 2, 8, 3) + 50
    bullets = [str(x) for x in page.get("bullets", []) if str(x).strip()][:6]
    split = max(1, (len(bullets) + 1) // 2)
    columns = [bullets[:split], bullets[split:]]
    gap, card_w = 26, (WIDTH - SAFE_X * 2 - 26) // 2
    labels = ("常见做法", "更好的做法")
    for col in range(2):
        x = SAFE_X + col * (card_w + gap)
        _rounded(draw, (x, y, x + card_w, HEIGHT - 105), 34, (*surface, 238), (*(accent if col else muted), 75), 2)
        draw.text((x + 28, y + 30), labels[col], font=_font(32, True), fill=accent if col else muted)
        yy = y + 100
        for item in columns[col]:
            draw.ellipse((x + 30, yy + 10, x + 48, yy + 28), fill=accent if col else muted)
            yy = _text(draw, (x + 66, yy), item, _font(28, True), text, card_w - 96, 8, 4) + 30


def render_preview(plan_id: str, preview: dict) -> list[Path]:
    """Render every preview page and atomically replace the plan asset folder."""
    design = preview.get("design_system") or {}
    background = _color(design.get("background"), "#F4F7FB")
    secondary = _color(design.get("secondary_background"), "#E8F0FA")
    primary = _color(design.get("primary"), "#175CD3")
    accent = _color(design.get("accent"), "#FFCC33")
    text = _color(design.get("text"), "#172033")
    surface = _color(design.get("surface"), "#FFFFFF")
    palette = dict(background=background, secondary=secondary, primary=primary, accent=accent, text=text, muted=_mix(text, background, .48), surface=surface)
    pages = preview.get("image_pages") or []
    root = db.ROOT / "exports" / "plan-assets" / plan_id
    root.mkdir(parents=True, exist_ok=True)
    temp_files, targets = [], []
    for index, page in enumerate(pages, 1):
        image = _gradient(background, secondary)
        draw = ImageDraw.Draw(image, "RGBA")
        _decorations(draw, palette, index)
        _brand(draw, palette, index, len(pages))
        layout = str(page.get("layout") or ("hero" if index == 1 else "list"))
        if index == 1 or layout == "hero":
            _draw_cover(draw, page, palette)
        elif layout == "grid":
            _draw_grid(draw, page, palette)
        elif layout == "timeline" or layout == "steps":
            _draw_list(draw, page, palette, timeline=True)
        elif layout == "comparison":
            _draw_comparison(draw, page, palette)
        else:
            _draw_list(draw, page, palette)
        target = root / f"page-{index:02d}.png"
        temp = root / f".{target.name}.part"
        image.save(temp, format="PNG", optimize=True)
        temp_files.append(temp); targets.append(target)
    for old in root.glob("page-*.png"):
        if old not in targets:
            old.unlink(missing_ok=True)
    for temp, target in zip(temp_files, targets):
        temp.replace(target)
    return targets


def asset_paths(plan_id: str) -> Iterable[Path]:
    root = db.ROOT / "exports" / "plan-assets" / plan_id
    return sorted(root.glob("page-*.png")) if root.exists() else []

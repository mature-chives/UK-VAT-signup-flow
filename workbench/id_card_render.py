from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .id_card_translate import ID_CARD_EN_LABELS, normalize_bbox

CARD_SIZE = (1000, 630)
PAGE_SIZE = (1240, 1754)
PHOTO_BOX = (720, 78, 940, 368)

_FONT_CANDIDATES = (
    Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
    Path("/System/Library/Fonts/STHeiti Light.ttc"),
    Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    Path("/Library/Fonts/Arial Unicode.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
)


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    env = Path(__import__("os").environ.get("ID_CARD_FONT") or "")
    for path in (env, *_FONT_CANDIDATES):
        if not path or not path.is_file():
            continue
        try:
            return ImageFont.truetype(str(path), size, index=0)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    words = text.replace(",", ", ").split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _open_rgb(source: bytes | Image.Image | None) -> Image.Image | None:
    if source is None:
        return None
    if isinstance(source, Image.Image):
        return ImageOps.exif_transpose(source).convert("RGB")
    opened = Image.open(io.BytesIO(source))
    return ImageOps.exif_transpose(opened).convert("RGB")


def default_portrait_bbox(width: int, height: int) -> list[int]:
    """二代身份证头像在正面右侧中上部。OCR 没给出人像框时用这个比例。"""
    return [
        int(width * 0.68),
        int(height * 0.14),
        int(width * 0.96),
        int(height * 0.62),
    ]


def _fit(image: Image.Image, box: tuple[int, int]) -> Image.Image:
    copy = image.copy()
    copy.thumbnail(box, Image.Resampling.LANCZOS)
    return copy


def _cover(image: Image.Image, box: tuple[int, int, int, int]) -> Image.Image:
    width = box[2] - box[0]
    height = box[3] - box[1]
    scale = max(width / max(image.width, 1), height / max(image.height, 1))
    resized = image.resize(
        (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
        Image.Resampling.LANCZOS,
    )
    left = max(0, (resized.width - width) // 2)
    top = max(0, (resized.height - height) // 2)
    return resized.crop((left, top, left + width, top + height))


def crop_portrait(image: Image.Image, bbox: list[int] | None) -> Image.Image | None:
    coords = normalize_bbox(bbox, image.width, image.height)
    if coords is None:
        coords = default_portrait_bbox(image.width, image.height)
    x1, y1, x2, y2 = coords
    if x2 - x1 < 20 or y2 - y1 < 20:
        return None
    return image.crop((x1, y1, x2, y2))


def render_translation_front(fields: dict[str, str], portrait: Image.Image | None) -> Image.Image:
    card = Image.new("RGB", CARD_SIZE, (228, 239, 246))
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle((8, 8, 991, 621), radius=28, outline=(170, 196, 214), width=3)
    label_font = _font(22)
    value_font = _font(32)
    small_font = _font(24)
    number_font = _font(30)
    photo = PHOTO_BOX
    draw.rounded_rectangle(photo, radius=8, outline=(160, 176, 188), width=2, fill=(236, 240, 244))
    if portrait is not None:
        fitted = _cover(portrait.convert("RGB"), photo)
        card.paste(fitted, (photo[0], photo[1]))
    rows = [
        ("Name", fields.get("full_name", "")),
        ("Sex", fields.get("sex", "")),
        ("Ethnicity", fields.get("ethnicity", "")),
        ("Date of Birth", fields.get("birth_date", "")),
        ("Residential Address", fields.get("address", "")),
    ]
    y = 70
    value_width = photo[0] - 70
    for label, value in rows:
        draw.text((48, y), f"{label}:", fill=(22, 92, 122), font=label_font)
        lines = _wrap(draw, value, value_font if label != "Residential Address" else small_font, value_width)
        text_font = small_font if label == "Residential Address" else value_font
        draw.text((48, y + 26), "\n".join(lines[:3]), fill=(18, 24, 32), font=text_font, spacing=4)
        y += 78 if label != "Residential Address" else 96
    draw.text((48, 545), f"{ID_CARD_EN_LABELS['identity_document_number']}:", fill=(22, 92, 122), font=label_font)
    draw.text((48, 575), fields.get("identity_document_number", ""), fill=(18, 24, 32), font=number_font)
    return card


def render_translation_back(fields: dict[str, str]) -> Image.Image:
    card = Image.new("RGB", CARD_SIZE, (228, 239, 246))
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle((8, 8, 991, 621), radius=28, outline=(170, 196, 214), width=3)
    title_font = _font(30)
    body_font = _font(26)
    label_font = _font(22)
    draw.text(
        (500, 90),
        "Citizen Identity Card of the",
        fill=(18, 24, 32),
        font=title_font,
        anchor="mt",
    )
    draw.text(
        (500, 140),
        "People's Republic of China",
        fill=(18, 24, 32),
        font=title_font,
        anchor="mt",
    )
    draw.text((80, 360), "Authority:", fill=(22, 92, 122), font=label_font)
    authority = fields.get("issuing_authority", "")
    lines = _wrap(draw, authority, body_font, 840)
    draw.text((80, 400), "\n".join(lines[:3]), fill=(18, 24, 32), font=body_font, spacing=6)
    draw.text((80, 510), "Valid through:", fill=(22, 92, 122), font=label_font)
    draw.text((80, 548), fields.get("valid_period", ""), fill=(18, 24, 32), font=body_font)
    return card


def _page(top: Image.Image, bottom: Image.Image, heading: str, top_caption: str, bottom_caption: str) -> Image.Image:
    page = Image.new("RGB", PAGE_SIZE, (246, 247, 249))
    draw = ImageDraw.Draw(page)
    heading_font = _font(36)
    caption_font = _font(22)
    draw.text((PAGE_SIZE[0] // 2, 48), heading, fill=(18, 24, 32), font=heading_font, anchor="mt")
    slot_w, slot_h = 1080, 680
    top_fitted = _fit(top, (slot_w, slot_h))
    bottom_fitted = _fit(bottom, (slot_w, slot_h))
    top_x = (PAGE_SIZE[0] - top_fitted.width) // 2
    bottom_x = (PAGE_SIZE[0] - bottom_fitted.width) // 2
    draw.text((PAGE_SIZE[0] // 2, 108), top_caption, fill=(70, 82, 94), font=caption_font, anchor="mt")
    page.paste(top_fitted, (top_x, 150))
    draw.text((PAGE_SIZE[0] // 2, 870), bottom_caption, fill=(70, 82, 94), font=caption_font, anchor="mt")
    page.paste(bottom_fitted, (bottom_x, 910))
    return page


def _placeholder(label: str) -> Image.Image:
    card = Image.new("RGB", CARD_SIZE, (236, 238, 241))
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle((8, 8, 991, 621), radius=28, outline=(190, 196, 204), width=3)
    draw.text((500, 315), label, fill=(120, 128, 136), font=_font(28), anchor="mm")
    return card


def _read_selected(task: dict, file_id: str | None) -> bytes | None:
    if not file_id:
        return None
    for item in task.get("selected_files") or []:
        if item.get("id") != file_id:
            continue
        path = Path(str(item.get("path") or ""))
        if path.is_file():
            return path.read_bytes()
    return None


def build_task_id_card_pdf(task: dict, translated: dict[str, str]) -> bytes:
    sides = list((task.get("extracted_id_card") or {}).get("sides") or [])
    files = list(task.get("selected_files") or [])
    front_id = next((item.get("file_id") for item in sides if item.get("side") == "front"), None)
    back_id = next((item.get("file_id") for item in sides if item.get("side") == "back"), None)
    if not front_id and files:
        front_id = files[0].get("id")
    if not back_id and len(files) > 1:
        back_id = files[1].get("id")
    front_bytes = _read_selected(task, front_id)
    back_bytes = _read_selected(task, back_id)
    portrait = None
    bbox = next((item.get("portrait_bbox") for item in sides if item.get("side") == "front"), None)
    if front_bytes:
        image = _open_rgb(front_bytes)
        if image is not None:
            portrait = crop_portrait(image, bbox if isinstance(bbox, list) else None)
    return build_id_card_pdf(
        translated=translated,
        original_front=front_bytes,
        original_back=back_bytes,
        portrait=portrait,
    )


def build_id_card_pdf(
    *,
    translated: dict[str, str],
    original_front: bytes | Image.Image | None,
    original_back: bytes | Image.Image | None,
    portrait: bytes | Image.Image | None = None,
) -> bytes:
    """一页翻译件正反面，一页原件正反面。"""
    portrait_im = _open_rgb(portrait)
    front_en = render_translation_front(translated, portrait_im)
    back_en = render_translation_back(translated)
    front_zh = _open_rgb(original_front) or _placeholder("Original front not provided")
    back_zh = _open_rgb(original_back) or _placeholder("Original back not provided")
    page1 = _page(front_en, back_en, "Translated identity card", "Front side", "Back side")
    page2 = _page(front_zh, back_zh, "Original identity card", "Front side", "Back side")
    buffer = io.BytesIO()
    page1.save(buffer, format="PDF", save_all=True, append_images=[page2], resolution=150.0)
    return buffer.getvalue()

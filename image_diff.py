from pathlib import Path

from PIL import Image, ImageChops, ImageEnhance, ImageOps


def _open_rgb(path):
    with Image.open(path) as image:
        return ImageOps.exif_transpose(image).convert("RGB")


def _align_images(old_image, new_image):
    width = max(old_image.width, new_image.width)
    height = max(old_image.height, new_image.height)
    background = (255, 255, 255)
    old_canvas = Image.new("RGB", (width, height), background)
    new_canvas = Image.new("RGB", (width, height), background)
    old_canvas.paste(old_image, (0, 0))
    new_canvas.paste(new_image, (0, 0))
    return old_canvas, new_canvas


def create_image_diff(old_path, new_path, target_path):
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    old_image, new_image = _align_images(_open_rgb(old_path), _open_rgb(new_path))
    difference = ImageChops.difference(old_image, new_image)
    difference = ImageEnhance.Contrast(difference).enhance(3.0)
    overlay = Image.blend(old_image, new_image, 0.5)
    mask = difference.convert("L").point(lambda pixel: 255 if pixel > 35 else 0)
    highlight = Image.new("RGB", old_image.size, (255, 0, 120))
    overlay.paste(highlight, mask=mask)
    overlay.save(target, format="PNG")
    return str(target)


def create_side_by_side(old_path, new_path, target_path):
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    old_image = _open_rgb(old_path)
    new_image = _open_rgb(new_path)
    height = max(old_image.height, new_image.height)
    old_canvas = Image.new("RGB", (old_image.width, height), (255, 255, 255))
    new_canvas = Image.new("RGB", (new_image.width, height), (255, 255, 255))
    old_canvas.paste(old_image, (0, 0))
    new_canvas.paste(new_image, (0, 0))
    result = Image.new(
        "RGB",
        (old_canvas.width + new_canvas.width, height),
        (230, 230, 230),
    )
    result.paste(old_canvas, (0, 0))
    result.paste(new_canvas, (old_canvas.width, 0))
    result.save(target, format="PNG")
    return str(target)
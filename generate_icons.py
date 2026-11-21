"""Генерация иконок APrice (подзадача 6.1).

Создаёт три ``.ico`` в ``build_scripts/assets/`` (Pillow):

* ``icon_client.ico``  — синий фон, белое «AP» (АвтоПрайс);
* ``icon_server.ico``  — красный фон, белая «S» (Server);
* ``icon_builder.ico`` — зелёный фон, белая «B» (Builder).

Иконка — скруглённый квадрат 256×256 (рисуется в 4-кратном размере и уменьшается —
гладкие края), внутри файла вложены размеры 16, 32, 48, 64, 128 и 256.
Запуск: ``python generate_icons.py`` (``build.py`` вызывает его перед сборкой).
Нужен Pillow (``pip install -r requirements-dev.txt``).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

#: Размеры, вложенные в каждый .ico (максимальный — 256).
ICON_SIZES: tuple[int, ...] = (16, 32, 48, 64, 128, 256)
BASE_SIZE = 256
SUPERSAMPLE = 4
CORNER_RADIUS_RATIO = 0.22  # радиус скругления от стороны квадрата
TEXT_COLOR = (255, 255, 255, 255)

#: имя файла -> (текст, цвет фона RGB)
ICONS: dict[str, tuple[str, tuple[int, int, int]]] = {
    "icon_client.ico": ("AP", (30, 111, 217)),    # синий
    "icon_server.ico": ("S", (211, 47, 47)),      # красный
    "icon_builder.ico": ("B", (46, 139, 62)),     # зелёный
}

#: Жирные шрифты по убыванию предпочтения: Arial Bold (Windows), затем похожие.
FONT_CANDIDATES: tuple[str, ...] = (
    r"C:\Windows\Fonts\arialbd.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Arial_Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "arialbd.ttf",
    "DejaVuSans-Bold.ttf",
)


def default_output_dir() -> Path:
    """``build_scripts/assets`` рядом с этим скриптом."""
    return Path(__file__).resolve().parent / "build_scripts" / "assets"


def load_font(size: int):
    """Возвращает жирный шрифт нужного размера (Arial Bold или запасной вариант)."""
    from PIL import ImageFont

    for candidate in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default(size)  # Pillow >= 10.1: масштабируемый шрифт


def _fit_font(draw, text: str, box_w: float, box_h: float):
    """Подбирает наибольший размер шрифта, при котором текст влезает в рамку."""
    size = int(box_h * 1.4)
    while size > 8:
        font = load_font(size)
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        if right - left <= box_w and bottom - top <= box_h:
            return font
        size -= 4
    return load_font(8)


def render_icon(text: str, background: tuple[int, int, int], size: int = BASE_SIZE):
    """Рисует квадратную иконку ``size``×``size`` (RGBA, скруглённые углы, белый текст)."""
    from PIL import Image, ImageDraw

    big = size * SUPERSAMPLE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    radius = int(big * CORNER_RADIUS_RATIO)
    draw.rounded_rectangle((0, 0, big - 1, big - 1), radius=radius, fill=background + (255,))
    # Крупный читаемый текст: до 70% ширины и 56% высоты.
    font = _fit_font(draw, text, big * 0.70, big * 0.56)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    x = (big - (right - left)) / 2 - left
    y = (big - (bottom - top)) / 2 - top
    draw.text((x, y), text, font=font, fill=TEXT_COLOR)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def generate_icons(out_dir: Optional[Path] = None) -> list[Path]:
    """Создаёт три ``.ico`` в ``out_dir`` (по умолчанию ``build_scripts/assets``).

    Returns:
        list[Path]: пути созданных файлов.
    """
    target = Path(out_dir) if out_dir is not None else default_output_dir()
    target.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    for name, (text, color) in ICONS.items():
        path = target / name
        render_icon(text, color).save(path, format="ICO", sizes=[(s, s) for s in ICON_SIZES])
        created.append(path)
    return created


def main() -> int:
    """Точка входа: ``0`` — готово, ``1`` — не установлен Pillow."""
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("ОШИБКА: не установлен Pillow (pip install Pillow).", file=sys.stderr)
        return 1
    for path in generate_icons():
        print(f"Иконка создана: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

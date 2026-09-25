"""Иконки SHEPOT, нарисованные кодом (Pillow): для трея Windows/macOS и
для значка приложения (.ico / .icns / .png в сборке).

Круг цвета состояния + белый микрофон (или динамик при чтении).
Цвета — палитра Material Design, контраст белого значка ≥ 3:1.
"""

from PIL import Image, ImageDraw

# цвет круга для каждого состояния трея
COLORS = {
    "load": (251, 140, 0),    # оранжевый — загрузка модели
    "idle": (30, 136, 229),   # синий — готов
    "rec":  (229, 57, 53),    # красный — идёт запись
    "off":  (117, 117, 117),  # серый — выключено
    "read": (142, 36, 170),   # фиолетовый — читает вслух
}


def draw_icon(state="idle", size=64):
    """Картинка RGBA size×size для состояния трея."""
    s = 4 * size                       # рисуем крупно и уменьшаем — сглаживание
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 0, s - 1, s - 1), fill=COLORS[state] + (255,))
    white = (255, 255, 255, 255)
    u = s / 16                         # единица сетки 16×16
    if state == "read":
        # динамик: корпус + раструб + две волны
        d.rectangle((4 * u, 6.5 * u, 6 * u, 9.5 * u), fill=white)
        d.polygon([(6 * u, 6.5 * u), (9 * u, 4 * u), (9 * u, 12 * u), (6 * u, 9.5 * u)], fill=white)
        w = max(2, int(0.8 * u))
        d.arc((8 * u, 5.5 * u, 11.5 * u, 10.5 * u), -50, 50, fill=white, width=w)
        d.arc((8 * u, 3.5 * u, 13.5 * u, 12.5 * u), -50, 50, fill=white, width=w)
    else:
        # микрофон: капсула + дуга держателя + ножка
        d.rounded_rectangle((6.2 * u, 3 * u, 9.8 * u, 9.5 * u), radius=1.8 * u, fill=white)
        w = max(2, int(0.9 * u))
        d.arc((4.6 * u, 5.2 * u, 11.4 * u, 11.2 * u), 0, 180, fill=white, width=w)
        d.line((8 * u, 11.2 * u, 8 * u, 13 * u), fill=white, width=w)
        d.line((6 * u, 13 * u, 10 * u, 13 * u), fill=white, width=w)
        if state == "off":             # перечёркнутый
            d.line((4 * u, 4 * u, 12 * u, 12.5 * u), fill=white, width=w)
    return img.resize((size, size), Image.LANCZOS)


def save_app_icons(out_dir):
    """Значок приложения для сборки: shepot.png, shepot.ico, shepot.icns."""
    import os
    os.makedirs(out_dir, exist_ok=True)
    big = draw_icon("idle", 1024)
    big.save(os.path.join(out_dir, "shepot.png"))
    big.save(os.path.join(out_dir, "shepot.ico"),
             sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    big.save(os.path.join(out_dir, "shepot.icns"))
    return out_dir


if __name__ == "__main__":
    import sys
    print(save_app_icons(sys.argv[1] if len(sys.argv) > 1 else "icons"))

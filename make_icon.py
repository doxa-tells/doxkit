# Рисует иконку DoxKit (1024×1024) без шрифтов и сохраняет icon.icns + icon.png
import sys
from pathlib import Path
from PIL import Image, ImageDraw

out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
S = 1024
SS = 2  # рисуем в 2× и уменьшаем — ровные края
W = S * SS


def sc(*v):
    return [x * SS for x in v]


img = Image.new("RGBA", (W, W), (0, 0, 0, 0))

# Синий фон с вертикальным градиентом
bg = Image.new("RGBA", (W, W))
top, bot = (40, 128, 255), (0, 78, 220)
for y in range(W):
    t = y / W
    bg.paste(tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)) + (255,), (0, y, W, y + 1))
mask = Image.new("L", (W, W), 0)
ImageDraw.Draw(mask).rounded_rectangle(sc(100, 100, 924, 924), radius=185 * SS, fill=255)
img.paste(bg, (0, 0), mask)
d = ImageDraw.Draw(img)

# Белый лист с загнутым уголком
L, T, R, B, F = 300, 230, 724, 794, 130
d.polygon([(x * SS, y * SS) for x, y in [(L, T), (R - F, T), (R, T + F), (R, B), (L, B)]], fill="white")
d.polygon([(x * SS, y * SS) for x, y in [(R - F, T), (R - F, T + F), (R, T + F)]], fill=(190, 212, 255))

# Строки текста на листе
blue = (10, 102, 255)
for i, w in enumerate([300, 330, 250]):
    y = 400 + i * 70
    d.rounded_rectangle(sc(L + 62, y, L + 62 + w, y + 30), radius=15 * SS, fill=(205, 222, 255))

# Стрелки «сжатие» внизу листа
cx, cy = 512, 680
d.polygon([(x * SS, y * SS) for x, y in [(cx - 120, cy - 45), (cx - 30, cy), (cx - 120, cy + 45)]], fill=blue)
d.polygon([(x * SS, y * SS) for x, y in [(cx + 120, cy - 45), (cx + 30, cy), (cx + 120, cy + 45)]], fill=blue)

img = img.resize((S, S), Image.LANCZOS)
img.save(out / "icon.png")
img.save(out / "icon.icns")
print("icon ok")

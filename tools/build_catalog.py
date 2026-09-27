"""Собирает catalog.json из tools/raw.json (extract.py) и ручной разметки tools/meta/*.json."""
import hashlib
import json
import sys
import textwrap
from pathlib import Path
from urllib.parse import quote

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "tools" / "raw.json"
META_DIR = ROOT / "tools" / "meta"
COVERS = ROOT / "covers"
CATALOG = ROOT / "catalog.json"
BASE_URL = "https://raw.githubusercontent.com/bigmadrussian/my-library-repo/main"

FONT_DIR = Path("/System/Library/Fonts/Supplemental")
COVER_SIZE = (480, 720)
PALETTE = [
    ("#1f3a5f", "#e8d8a8"), ("#5a1e2b", "#f0dcc0"), ("#2e4a3a", "#e6e0c8"),
    ("#3b2f5c", "#e4d9f0"), ("#6b3e1f", "#f3e2c7"), ("#233d4d", "#fcca46"),
    ("#4a4a4a", "#f0e6d2"), ("#0b4f6c", "#e0f2f1"),
]

GENRES = {
    "Классическая проза", "Современная проза", "Драматургия", "Поэзия", "Приключения",
    "Фантастика", "Детская литература", "Сказки и мифы", "Историческая проза",
    "Документальная проза", "Литературоведение", "Религия", "Философия",
    "Программирование", "Алгоритмы и структуры данных", "Архитектура ПО", "Базы данных",
    "Информационная безопасность", "Linux и администрирование", "Компьютерные сети",
    "DevOps и облака", "Искусственный интеллект", "Анализ данных", "Математика",
    "Математическая логика", "Дискретная математика", "Теория вероятностей и статистика",
    "Логика", "Иностранные языки", "Медицина", "Научно-популярная литература",
    "Саморазвитие", "Экономика", "Документ",
}
REQUIRED = ("title", "author", "description", "genre", "language", "year")


def url(*parts: str) -> str:
    return "/".join([BASE_URL, *(quote(p) for p in parts)])


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_DIR / name), size)


def draw_centered(draw, lines, fnt, y, fill, width, spacing=1.2):
    for line in lines:
        w = draw.textlength(line, font=fnt)
        draw.text(((width - w) / 2, y), line, font=fnt, fill=fill)
        y += fnt.size * spacing
    return y


def fit_title(draw, title, max_w, max_h):
    for size in range(52, 21, -2):
        fnt = font("Georgia Bold.ttf", size)
        chars = max(8, int(max_w / (size * 0.55)))
        lines = textwrap.wrap(title, chars)
        if len(lines) * size * 1.2 <= max_h and all(draw.textlength(l, font=fnt) <= max_w for l in lines):
            return fnt, lines
    fnt = font("Georgia Bold.ttf", 22)
    return fnt, textwrap.wrap(title, 30)[:8]


def generate_cover(book: dict, dest: Path) -> None:
    """Типографская обложка для книг, у которых в файле нет своей."""
    w, h = COVER_SIZE
    idx = int(hashlib.md5(book["genre"].encode()).hexdigest(), 16) % len(PALETTE)
    bg, fg = PALETTE[idx]
    img = Image.new("RGB", COVER_SIZE, bg)
    d = ImageDraw.Draw(img)
    d.rectangle((24, 24, w - 24, h - 24), outline=fg, width=2)
    d.rectangle((34, 34, w - 34, h - 34), outline=fg, width=1)

    author_font = font("Georgia Italic.ttf", 26)
    draw_centered(d, textwrap.wrap(book["author"], 30)[:3], author_font, 80, fg, w)

    title_font, lines = fit_title(d, book["title"], w - 110, 330)
    block_h = len(lines) * title_font.size * 1.2
    y = draw_centered(d, lines, title_font, (h - block_h) / 2 + 10, fg, w)
    d.line((w / 2 - 50, y + 16, w / 2 + 50, y + 16), fill=fg, width=2)

    footer = book["genre"] + (f" · {book['year']}" if book["year"] else "")
    draw_centered(d, [footer], font("Georgia.ttf", 20), h - 90, fg, w)
    img.save(dest, "JPEG", quality=88, optimize=True, progressive=True)


def main() -> int:
    raw = [r for r in json.loads(RAW.read_text("utf-8")) if "skip" not in r]
    meta = {}
    for f in sorted(META_DIR.glob("*.json")):
        meta.update(json.loads(f.read_text("utf-8")))

    problems, excluded, seen_hash, generated = [], [], {}, 0
    catalog = []
    for r in raw:
        m = meta.get(r["id"])
        if m is None:
            problems.append(f"{r['id']}: нет разметки")
            continue
        missing = [k for k in REQUIRED if k not in m or m[k] in ("", None) and k != "year"]
        if missing:
            problems.append(f"{r['id']}: не заполнено {missing}")
        if m.get("genre") not in GENRES:
            problems.append(f"{r['id']}: неизвестный жанр {m.get('genre')!r}")
        year = m.get("year")
        if year is not None and not (isinstance(year, int) and -800 <= year <= 2026):
            problems.append(f"{r['id']}: странный год {year!r}")

        key = (r["size"], r["md5_head"])
        if m.get("kind") == "document":
            excluded.append((r["file"], "не книга (документ)"))
            continue
        if m.get("duplicateOf"):
            excluded.append((r["file"], f"дубликат {m['duplicateOf']}"))
            continue
        if key in seen_hash:
            excluded.append((r["file"], f"побайтовый дубликат {seen_hash[key]}"))
            continue
        seen_hash[key] = r["id"]

        book = {
            "id": r["id"],
            "title": m["title"],
            "author": m["author"],
            "description": m["description"],
            "genre": m["genre"],
            "language": m["language"],
            "format": r["ext"].lstrip("."),
            "year": year,
            "pages": r.get("pages"),
            "fileSizeBytes": r["size"],
            "coverUrl": url("covers", f"{r['id']}.jpg"),
            "downloadUrl": url("books", r["file"]),
        }
        if not r.get("cover"):
            generate_cover(book, COVERS / f"{r['id']}.jpg")
            generated += 1
        catalog.append(book)

    used = {b["id"] for b in catalog}
    for p in COVERS.glob("*.jpg"):
        if p.stem not in used:
            p.unlink()

    catalog.sort(key=lambda b: (b["genre"], b["author"], b["title"]))
    CATALOG.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", "utf-8")

    print(f"catalog.json: {len(catalog)} книг, сгенерировано обложек: {generated}")
    print(f"исключено: {len(excluded)}")
    for f, why in excluded:
        print(f"  - {f}: {why}")
    if problems:
        print(f"\nПРОБЛЕМЫ ({len(problems)}):")
        for p in problems:
            print("  -", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

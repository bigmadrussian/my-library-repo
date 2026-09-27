"""Сканирует books/, извлекает сырые метаданные и обложки.

Результат: tools/raw.json (сырые данные для ручной разметки) и covers/<id>.jpg.
"""
import hashlib
import io
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import fitz
from PIL import Image, ImageStat

ROOT = Path(__file__).resolve().parent.parent
BOOKS = ROOT / "books"
COVERS = ROOT / "covers"
OUT = ROOT / "tools" / "raw.json"
# id -> номер страницы с обложкой (с 0) или "generate"; для случаев, где эвристика ошибается
OVERRIDES_FILE = ROOT / "tools" / "cover_overrides.json"
COVER_OVERRIDES = json.loads(OVERRIDES_FILE.read_text("utf-8")) if OVERRIDES_FILE.exists() else {}

BOOK_EXT = {".pdf", ".epub", ".fb2"}
COVER_WIDTH = 480

TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}


def make_id(stem: str) -> str:
    s = "".join(TRANSLIT.get(c, c) for c in stem.lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:80].strip("-") or "book"


def save_cover(img: Image.Image, dest: Path) -> None:
    img = img.convert("RGB")
    if img.width > COVER_WIDTH:
        h = round(img.height * COVER_WIDTH / img.width)
        img = img.resize((COVER_WIDTH, h), Image.LANCZOS)
    img.save(dest, "JPEG", quality=82, optimize=True, progressive=True)


def is_flat(img: Image.Image) -> bool:
    """Пустая или почти однотонная страница: больше 3/4 клеток сетки 4x6 без деталей."""
    g = img.convert("L")
    w, h = g.size
    flat = sum(
        ImageStat.Stat(g.crop((i * w // 4, j * h // 6, (i + 1) * w // 4, (j + 1) * h // 6))).stddev[0] < 4
        for i in range(4)
        for j in range(6)
    )
    return flat > 18


def render(page) -> Image.Image:
    zoom = COVER_WIDTH / max(page.rect.width, 1)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def white_fraction(img: Image.Image) -> float:
    hist = img.convert("L").histogram()
    return sum(hist[236:]) / max(sum(hist), 1)


def looks_like_cover(page, img: Image.Image) -> bool:
    """Отсекает текстовые титульные листы, оглавления, страницы с текстом и рекламой сайтов."""
    try:
        text_len = len(page.get_text().strip())
    except Exception:
        text_len = 0
    return text_len < 400 and white_fraction(img) < 0.93 and not is_flat(img)


def render_cover(doc, dest: Path, forced_page=None):
    """Ищет обложку среди первых страниц. Возвращает номер страницы или None (нужна сгенерированная)."""
    if forced_page is not None:
        save_cover(render(doc[forced_page]), dest)
        return forced_page
    for i in range(min(4, len(doc))):
        img = render(doc[i])
        if looks_like_cover(doc[i], img):
            save_cover(img, dest)
            return i
    return None


def first_text(doc, pages: int = 4, limit: int = 1500) -> str:
    chunks = []
    for i in range(min(pages, len(doc))):
        try:
            chunks.append(doc[i].get_text())
        except Exception:
            pass
    text = re.sub(r"\s+", " ", " ".join(chunks)).strip()
    return text[:limit].encode("utf-8", "replace").decode("utf-8")


def epub_meta(path: Path):
    meta, cover = {}, None
    with zipfile.ZipFile(path) as z:
        container = ET.fromstring(z.read("META-INF/container.xml"))
        opf_path = container.find(".//{*}rootfile").get("full-path")
        opf = ET.fromstring(z.read(opf_path))
        base = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
        for tag in ("title", "creator", "language", "date", "description", "publisher", "subject"):
            el = opf.find(f".//{{*}}metadata/{{*}}{tag}")
            if el is not None and el.text:
                meta[tag] = el.text.strip()
        manifest = {i.get("id"): i for i in opf.iter("{http://www.idpf.org/2007/opf}item")}
        cover_href = None
        for m in opf.iter("{http://www.idpf.org/2007/opf}meta"):
            if m.get("name") == "cover" and m.get("content") in manifest:
                cover_href = manifest[m.get("content")].get("href")
        if not cover_href:
            for item in manifest.values():
                props = item.get("properties") or ""
                if "cover-image" in props or (
                    "cover" in (item.get("id") or "").lower()
                    and (item.get("media-type") or "").startswith("image/")
                ):
                    cover_href = item.get("href")
                    break
        if cover_href:
            try:
                cover = Image.open(io.BytesIO(z.read(base + cover_href)))
            except Exception:
                cover = None
    return meta, cover


def fb2_meta(path: Path):
    import base64

    root = ET.fromstring(path.read_bytes())
    ns = {"f": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {"f": ""}
    ti = root.find(".//f:description/f:title-info", ns)
    meta, cover = {}, None
    if ti is not None:
        def txt(p):
            el = ti.find(p, ns)
            return " ".join(el.itertext()).strip() if el is not None else None

        meta["title"] = txt("f:book-title")
        a = ti.find("f:author", ns)
        if a is not None:
            meta["creator"] = " ".join(
                (a.findtext(f"f:{k}", "", ns) or "").strip() for k in ("first-name", "last-name")
            ).strip()
        meta["language"] = txt("f:lang")
        meta["date"] = txt("f:date")
        meta["subject"] = txt("f:genre")
        meta["description"] = txt("f:annotation")
        img = ti.find("f:coverpage/f:image", ns)
        if img is not None:
            href = next((v for k, v in img.attrib.items() if k.endswith("href")), "").lstrip("#")
            for b in root.iter():
                if b.tag.endswith("binary") and b.get("id") == href:
                    cover = Image.open(io.BytesIO(base64.b64decode(b.text)))
    return {k: v for k, v in meta.items() if v}, cover


def main() -> None:
    COVERS.mkdir(exist_ok=True)
    only = set(sys.argv[1:])
    records, used_ids = [], set()
    for path in sorted(BOOKS.iterdir()):
        if path.name.startswith(".") or not path.is_file():
            continue
        ext = path.suffix.lower()
        stem = path.name[: -len(path.suffix)] if path.suffix else path.name
        base_id = make_id(stem)
        book_id, n = base_id, 2
        while book_id in used_ids:
            book_id, n = f"{base_id}-{n}", n + 1
        used_ids.add(book_id)
        if only and book_id not in only:
            continue

        h = hashlib.md5()
        with path.open("rb") as f:
            h.update(f.read(4 << 20))
        rec = {
            "id": book_id,
            "file": path.name,
            "ext": ext,
            "size": path.stat().st_size,
            "md5_head": h.hexdigest(),
        }
        if ext not in BOOK_EXT:
            rec["skip"] = "not a book format"
            records.append(rec)
            continue

        cover_path = COVERS / f"{book_id}.jpg"
        try:
            doc = fitz.open(path)
            rec["pages"] = len(doc)
            rec["meta"] = {k: v for k, v in (doc.metadata or {}).items() if v}
            rec["text"] = first_text(doc)
            cover = None
            if ext == ".epub":
                m, cover = epub_meta(path)
                rec["meta"].update(m)
            elif ext == ".fb2":
                m, cover = fb2_meta(path)
                rec["meta"].update(m)
            override = COVER_OVERRIDES.get(book_id, "auto")
            if override == "generate" or not len(doc):
                rec["cover"] = False
            elif override == "auto" and cover is not None and not is_flat(cover):
                save_cover(cover, cover_path)
                rec["cover"], rec["coverPage"] = True, "embedded"
            else:
                page = render_cover(doc, cover_path, None if override == "auto" else override)
                rec["cover"], rec["coverPage"] = page is not None, page
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {e}"
        records.append(rec)
        print(f"{book_id:60s} {rec.get('pages', '-')!s:>6} {rec.get('error', '')}", flush=True)

    data = json.dumps(records, ensure_ascii=False, indent=1)
    OUT.write_text(data.encode("utf-8", "replace").decode("utf-8"), encoding="utf-8")
    print(f"\n{len(records)} records -> {OUT}")


if __name__ == "__main__":
    main()

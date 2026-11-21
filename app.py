#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DoxKit — локальный комбайн: сжатие, конвертация и расшифровка (Whisper).
Всё работает на вашем Маке, без интернета (кроме первой загрузки модели Whisper).
Интерфейс: http://127.0.0.1:7860
"""
import io
import json
import os
import queue
import shutil
import subprocess
import threading
import time
import types
import uuid
import zipfile
import importlib.util
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file

# Когда приложение стартует из Dock/Launchpad, PATH не содержит Homebrew
os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")

APP_DIR = Path(__file__).resolve().parent
WORK_DIR = APP_DIR / "work"
OUT_ROOT = Path.home() / "Downloads" / "DoxKit"
PORT = int(os.environ.get("FILEKIT_PORT", "7891"))
WORK_DIR.mkdir(parents=True, exist_ok=True)
OUT_ROOT.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Типы файлов и допустимые форматы конвертации
# ---------------------------------------------------------------------------
KINDS = {
    "video": {"mp4", "mov", "m4v", "mkv", "avi", "webm", "wmv", "flv", "3gp", "mts", "gif"},
    "audio": {"mp3", "wav", "m4a", "aac", "flac", "ogg", "opus", "wma", "aiff", "aif"},
    "image": {"jpg", "jpeg", "png", "webp", "heic", "heif", "tif", "tiff", "bmp"},
    "pdf": {"pdf"},
    "doc": {"docx", "doc", "odt", "rtf", "txt", "pages"},
    "slides": {"pptx", "ppt", "odp", "key"},
    "sheet": {"xlsx", "xls", "ods", "csv", "numbers"},
}
TARGETS = {
    "video": ["mp4", "mov", "webm", "mkv", "gif", "mp3", "m4a", "wav"],
    "audio": ["mp3", "m4a", "wav", "flac"],
    "image": ["jpg", "png", "webp", "pdf"],
    "pdf": ["docx", "txt", "jpg", "png"],
    "doc": ["pdf", "docx", "odt", "txt"],
    "slides": ["pdf", "pptx"],
    "sheet": ["pdf", "xlsx", "csv"],
}
WHISPER_MODELS = {
    "turbo": "mlx-community/whisper-large-v3-turbo",
    "large": "mlx-community/whisper-large-v3-mlx",
}


def kind_of(ext):
    for k, exts in KINDS.items():
        if ext in exts:
            return k
    return None


# ---------------------------------------------------------------------------
# Очередь задач
# ---------------------------------------------------------------------------
JOBS = {}
JOBS_LOCK = threading.Lock()
Q_FAST = queue.Queue()      # сжатие и конвертация
Q_WHISPER = queue.Queue()   # расшифровка (по одной, чтобы не делить GPU)
Q_DL = queue.Queue()        # скачивание по ссылкам


def new_job(**kw):
    jid = uuid.uuid4().hex[:12]
    job = {
        "id": jid, "status": "queued", "progress": 0, "message": "В очереди",
        "outputs": [], "created": time.time(), "note": None, **kw,
    }
    with JOBS_LOCK:
        JOBS[jid] = job
    return job


def update(job, **kw):
    with JOBS_LOCK:
        job.update(kw)


def public(job):
    with JOBS_LOCK:
        j = {k: v for k, v in job.items() if k not in ("src", "opts")}
        j["outputs"] = [{"name": o["name"], "size": o["size"]} for o in job["outputs"]]
        return j


def add_output(job, path):
    path = Path(path)
    with JOBS_LOCK:
        job["outputs"].append({"name": path.name, "path": str(path), "size": path.stat().st_size})


def worker(q):
    while True:
        job = q.get()
        try:
            update(job, status="running", message="Обрабатываю…", started=time.time())
            HANDLERS[job["action"]](job)
            update(job, status="done", progress=100, message=job.get("note") or "Готово",
                   finished=time.time())
        except Exception as e:  # noqa: BLE001
            msg = str(e).strip() or e.__class__.__name__
            update(job, status="error", message=msg[-600:])
        finally:
            shutil.rmtree(Path(job["src"]).parent, ignore_errors=True)
            q.task_done()


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------
def unique_path(stem, ext, tag=""):
    p = OUT_ROOT / f"{stem}{tag}.{ext}"
    i = 1
    while p.exists():
        p = OUT_ROOT / f"{stem}{tag} ({i}).{ext}"
        i += 1
    return p


def unique_dir(name):
    p = OUT_ROOT / name
    i = 1
    while p.exists():
        p = OUT_ROOT / f"{name} ({i})"
        i += 1
    p.mkdir(parents=True)
    return p


def run(cmd, timeout=None):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip().splitlines()[-6:]
        raise RuntimeError("\n".join(tail) or f"Команда завершилась с кодом {p.returncode}")
    return p


def media_duration(path):
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True)
        return float(p.stdout.strip())
    except Exception:  # noqa: BLE001
        return 0.0


def run_ffmpeg(job, src, args):
    """args заканчивается путём к выходному файлу. Пишет прогресс в job."""
    dur = media_duration(src)
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-y", "-progress", "pipe:1", "-i", str(src)] + args
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    err_tail = []

    def read_err():
        for line in p.stderr:
            err_tail.append(line)
            if len(err_tail) > 30:
                del err_tail[0]

    t = threading.Thread(target=read_err, daemon=True)
    t.start()
    for line in p.stdout:
        if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
            try:
                us = int(line.split("=", 1)[1])
                if dur > 0:
                    update(job, progress=max(1, min(99, int(us / 1e6 / dur * 100))))
            except ValueError:
                pass
    p.wait()
    t.join(timeout=2)
    if p.returncode != 0:
        raise RuntimeError("".join(err_tail[-6:]).strip() or "Ошибка ffmpeg")


_ENCODERS = None


def encoders():
    global _ENCODERS
    if _ENCODERS is None:
        try:
            out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                                 capture_output=True, text=True).stdout
            _ENCODERS = {ln.split()[1] for ln in out.splitlines()
                         if ln.startswith(" ") and len(ln.split()) > 1}
        except Exception:  # noqa: BLE001
            _ENCODERS = set()
    return _ENCODERS


def video_codec_args(codec, q):
    enc = encoders()
    vt_q = str({"high": 65, "medium": 55, "low": 45}[q])
    if codec == "h265":
        if "libx265" in enc:
            return ["-c:v", "libx265", "-crf", str({"high": 22, "medium": 26, "low": 30}[q]),
                    "-preset", "medium", "-x265-params", "log-level=error",
                    "-tag:v", "hvc1", "-pix_fmt", "yuv420p"]
        return ["-c:v", "hevc_videotoolbox", "-q:v", vt_q, "-tag:v", "hvc1"]
    if "libx264" in enc:
        return ["-c:v", "libx264", "-crf", str({"high": 20, "medium": 24, "low": 28}[q]),
                "-preset", "medium", "-pix_fmt", "yuv420p"]
    return ["-c:v", "h264_videotoolbox", "-q:v", vt_q, "-pix_fmt", "yuv420p"]


def scale_filter(res):
    """Уменьшает по короткой стороне (подходит и для вертикальных видео)."""
    if not res or res == "original":
        return None
    r = int(res)
    return (f"scale='if(gte(iw,ih),-2,trunc(min({r},iw)/2)*2)'"
            f":'if(gte(iw,ih),trunc(min({r},ih)/2)*2,-2)'")


def gif_filter(width_cap, fps, colors):
    w = f"min({width_cap},iw)" if width_cap else "iw"
    return (f"fps={fps},scale='{w}':-1:flags=lanczos,split[s0][s1];"
            f"[s0]palettegen=max_colors={colors}[p];[s1][p]paletteuse=dither=bayer")


def need(enc_name, human):
    if enc_name not in encoders():
        raise RuntimeError(f"В вашем ffmpeg нет кодека {enc_name} ({human}). "
                           f"Выполните в Терминале: brew reinstall ffmpeg")


# ---------------------------------------------------------------------------
# Картинки
# ---------------------------------------------------------------------------
def _pil():
    from PIL import Image, ImageOps
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:  # noqa: BLE001
        pass
    return Image, ImageOps


def flatten(im):
    Image, _ = _pil()
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        return bg
    return im.convert("RGB")


def save_image(im, path, target, q):
    Image, _ = _pil()
    jpgq = {"high": 85, "medium": 75, "low": 60}[q]
    if target in ("jpg", "jpeg"):
        flatten(im).save(path, "JPEG", quality=jpgq, optimize=True, progressive=True)
    elif target == "webp":
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA")
        im.save(path, "WEBP", quality=jpgq, method=6)
    elif target == "png":
        if q == "low":
            im = im.convert("RGBA").quantize(colors=256, method=Image.Quantize.FASTOCTREE)
        im.save(path, "PNG", optimize=True)
    elif target == "pdf":
        flatten(im).save(path, "PDF", resolution=150)
    else:
        raise RuntimeError(f"Не умею сохранять в {target}")


def open_image(src):
    Image, ImageOps = _pil()
    im = Image.open(src)
    return ImageOps.exif_transpose(im)


# ---------------------------------------------------------------------------
# СЖАТИЕ
# ---------------------------------------------------------------------------
def h_compress(job):
    src = Path(job["src"])
    kind = job["kind"]
    o = job["opts"]
    q = o.get("quality", "medium")
    if q not in ("high", "medium", "low"):
        q = "medium"
    before = src.stat().st_size

    if kind == "video":
        out = compress_video(job, src, o, q)
    elif kind == "audio":
        out = unique_path(src.stem, "mp3", " (сжато)")
        need("libmp3lame", "MP3")
        run_ffmpeg(job, src, ["-vn", "-c:a", "libmp3lame",
                              "-b:a", {"high": "192k", "medium": "128k", "low": "96k"}[q], str(out)])
    elif kind == "image":
        out = compress_image(src, o, q)
    elif kind == "pdf":
        out = unique_path(src.stem, "pdf", " (сжато)")
        preset = {"high": "/printer", "medium": "/ebook", "low": "/screen"}[q]
        if not shutil.which("gs"):
            raise RuntimeError("Не найден Ghostscript. Выполните: brew install ghostscript")
        run(["gs", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.5", f"-dPDFSETTINGS={preset}",
             "-dDetectDuplicateImages=true", "-dCompressFonts=true",
             "-dNOPAUSE", "-dQUIET", "-dBATCH", f"-sOutputFile={out}", str(src)])
    elif kind in ("doc", "slides", "sheet"):
        out = compress_office(job, src, q)
    else:
        raise RuntimeError("Этот тип файла не сжимаю")

    after = out.stat().st_size
    if after >= before:
        shutil.copyfile(src, out)
        job["note"] = "Файл уже был максимально сжат — сохранил оригинал без изменений"
    else:
        job["note"] = f"Готово: −{round((1 - after / before) * 100)}%"
    add_output(job, out)


def compress_video(job, src, o, q):
    ext = src.suffix.lower().lstrip(".")
    res = o.get("resolution", "original")
    if ext == "gif":
        out = unique_path(src.stem, "gif", " (сжато)")
        width = None if res == "original" else int(res)
        fps = {"high": 15, "medium": 12, "low": 10}[q]
        colors = {"high": 256, "medium": 128, "low": 64}[q]
        run_ffmpeg(job, src, ["-vf", gif_filter(width or 800, fps, colors), "-loop", "0", str(out)])
        return out
    out_ext = "mov" if ext == "mov" else "mp4"
    out = unique_path(src.stem, out_ext, " (сжато)")
    args = ["-map", "0:v:0", "-map", "0:a?"] + video_codec_args(o.get("codec", "h264"), q)
    vf = scale_filter(res)
    if vf:
        args += ["-vf", vf]
    args += ["-c:a", "aac", "-b:a", {"high": "192k", "medium": "128k", "low": "96k"}[q],
             "-movflags", "+faststart", str(out)]
    run_ffmpeg(job, src, args)
    return out


def compress_image(src, o, q):
    ext = src.suffix.lower().lstrip(".")
    fmt = o.get("image_format", "keep")
    max_side = o.get("max_side", "original")
    im = open_image(src)
    if max_side != "original":
        im.thumbnail((int(max_side), int(max_side)))
    if fmt == "keep":
        target = {"jpeg": "jpg", "heic": "jpg", "heif": "jpg", "tif": "jpg",
                  "tiff": "jpg", "bmp": "jpg"}.get(ext, ext)
    else:
        target = fmt
    out = unique_path(src.stem, target, " (сжато)")
    save_image(im, out, target, q)
    return out


def shrink_image_bytes(data, name, q):
    Image, _ = _pil()
    max_side = {"high": 2560, "medium": 1920, "low": 1280}[q]
    jpgq = {"high": 85, "medium": 75, "low": 60}[q]
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        if max(im.size) > max_side:
            im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        if name.endswith((".jpg", ".jpeg")):
            flatten(im).save(buf, "JPEG", quality=jpgq, optimize=True, progressive=True)
        else:
            im.save(buf, "PNG", optimize=True)
        new = buf.getvalue()
        return new if len(new) < len(data) else None
    except Exception:  # noqa: BLE001
        return None


def compress_office(job, src, q):
    ext = src.suffix.lower().lstrip(".")
    if ext not in ("docx", "pptx", "xlsx"):
        raise RuntimeError("Сжимаю только DOCX, PPTX и XLSX. Старые форматы сначала "
                           "сконвертируйте на вкладке «Конвертировать».")
    out = unique_path(src.stem, ext, " (сжато)")
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        items = zin.infolist()
        for i, info in enumerate(items):
            data = zin.read(info.filename)
            low = info.filename.lower()
            if "/media/" in low and low.endswith((".jpg", ".jpeg", ".png")):
                data = shrink_image_bytes(data, low, q) or data
            zi = zipfile.ZipInfo(info.filename, info.date_time)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = info.external_attr
            zout.writestr(zi, data, compresslevel=9)
            update(job, progress=max(1, min(99, int((i + 1) / len(items) * 100))))
    return out


# ---------------------------------------------------------------------------
# КОНВЕРТАЦИЯ
# ---------------------------------------------------------------------------
def h_convert(job):
    src = Path(job["src"])
    kind = job["kind"]
    target = job.get("target")
    if not target or target not in TARGETS.get(kind, []):
        raise RuntimeError("Не выбран формат для этого файла")
    if kind in ("video", "audio"):
        out = convert_media(job, src, kind, target)
    elif kind == "image":
        out = unique_path(src.stem, target)
        save_image(open_image(src), out, target, "high")
    elif kind == "pdf":
        out = convert_pdf(job, src, target)
    else:
        out = convert_office(src, target)
    add_output(job, out)


def convert_media(job, src, kind, target):
    out = unique_path(src.stem, target)
    if kind == "video" and target in ("mp4", "mov", "mkv"):
        # Сначала пробуем без перекодирования — мгновенно и без потерь
        remux = ["ffmpeg", "-hide_banner", "-y", "-i", str(src),
                 "-map", "0:v", "-map", "0:a?", "-c", "copy"]
        if target in ("mp4", "mov"):
            remux += ["-movflags", "+faststart"]
        try:
            update(job, message="Перепаковываю без потери качества…")
            run(remux + [str(out)])
            job["note"] = "Готово: без перекодирования, качество 1:1"
            return out
        except Exception:  # noqa: BLE001
            out.unlink(missing_ok=True)
            update(job, message="Перекодирую…")
        args = ["-map", "0:v:0", "-map", "0:a?"] + video_codec_args("h264", "high") + \
               ["-c:a", "aac", "-b:a", "192k"]
        if target in ("mp4", "mov"):
            args += ["-movflags", "+faststart"]
        run_ffmpeg(job, src, args + [str(out)])
        return out
    if target == "webm":
        need("libvpx-vp9", "WebM VP9")
        aud = ["-c:a", "libopus", "-b:a", "128k"] if "libopus" in encoders() else ["-an"]
        run_ffmpeg(job, src, ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-row-mt", "1"]
                   + aud + [str(out)])
        return out
    if target == "gif":
        run_ffmpeg(job, src, ["-vf", gif_filter(720, 12, 256), "-loop", "0", str(out)])
        return out
    audio_args = {
        "mp3": ["-vn", "-c:a", "libmp3lame", "-q:a", "2"],
        "m4a": ["-vn", "-c:a", "aac", "-b:a", "192k"],
        "wav": ["-vn", "-c:a", "pcm_s16le"],
        "flac": ["-vn", "-c:a", "flac"],
    }
    if target not in audio_args:
        raise RuntimeError(f"Не умею конвертировать в {target}")
    if target == "mp3":
        need("libmp3lame", "MP3")
    run_ffmpeg(job, src, audio_args[target] + [str(out)])
    return out


def convert_pdf(job, src, target):
    if target == "docx":
        from pdf2docx import Converter
        out = unique_path(src.stem, "docx")
        update(job, message="Распознаю структуру PDF…")
        cv = Converter(str(src))
        try:
            cv.convert(str(out))
        finally:
            cv.close()
        return out
    import fitz  # PyMuPDF (ставится вместе с pdf2docx)
    doc = fitz.open(str(src))
    try:
        if target == "txt":
            out = unique_path(src.stem, "txt")
            out.write_text("\n\n".join(page.get_text() for page in doc), encoding="utf-8")
            return out
        mat = fitz.Matrix(200 / 72, 200 / 72)
        if len(doc) == 1:
            out = unique_path(src.stem, target)
            doc[0].get_pixmap(matrix=mat).save(str(out))
            return out
        folder = unique_dir(f"{src.stem} — страницы")
        for i, page in enumerate(doc, start=1):
            page.get_pixmap(matrix=mat).save(str(folder / f"{src.stem} — {i:03d}.{target}"))
            update(job, progress=max(1, min(95, int(i / len(doc) * 95))))
        out = unique_path(src.stem, "zip", " — страницы")
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(folder.iterdir()):
                z.write(f, f.name)
        return out
    finally:
        doc.close()


def soffice_path():
    for p in ("/Applications/LibreOffice.app/Contents/MacOS/soffice", shutil.which("soffice") or ""):
        if p and Path(p).exists():
            return p
    return None


def convert_office(src, target):
    so = soffice_path()
    if not so:
        raise RuntimeError("Не найден LibreOffice. Выполните: brew install --cask libreoffice")
    tmp = WORK_DIR / f"lo_out_{uuid.uuid4().hex[:8]}"
    profile = WORK_DIR / f"lo_profile_{uuid.uuid4().hex[:8]}"
    tmp.mkdir(parents=True)
    flt = target
    if target == "txt":
        flt = "txt:Text (encoded):UTF8"
    elif target == "csv":
        flt = "csv:Text - txt - csv (StarCalc):44,34,76"
    try:
        run([so, f"-env:UserInstallation=file://{profile}", "--headless", "--norestore",
             "--convert-to", flt, "--outdir", str(tmp), str(src)], timeout=900)
        produced = list(tmp.glob(f"*.{target}"))
        if not produced:
            raise RuntimeError("LibreOffice не смог открыть этот файл")
        out = unique_path(src.stem, target)
        shutil.move(str(produced[0]), out)
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(profile, ignore_errors=True)


# ---------------------------------------------------------------------------
# РАСШИФРОВКА (Whisper на чипе Apple через MLX)
# ---------------------------------------------------------------------------
def ts(t, sep=","):
    ms_total = int(round(max(t, 0) * 1000))
    h, rem = divmod(ms_total, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def short_ts(t):
    total = int(t)
    h, m, s = total // 3600, (total % 3600) // 60, total % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _patch_tqdm(job):
    """Перехватывает прогресс-бар mlx_whisper, чтобы показывать проценты в интерфейсе."""
    try:
        import importlib
        import tqdm as tqdm_mod
        # NB: «import mlx_whisper.transcribe» вернёт функцию, а не модуль — берём модуль явно
        T = importlib.import_module("mlx_whisper.transcribe")

        class Prog(tqdm_mod.tqdm):
            def __init__(self, *a, **k):
                k["disable"] = False
                k["file"] = open(os.devnull, "w")  # noqa: SIM115
                super().__init__(*a, **k)

            def update(self, n=1):
                r = super().update(n)
                if self.total:
                    update(job, progress=max(1, min(99, int(self.n / self.total * 100))))
                return r

        holder = getattr(T, "tqdm", None)
        if isinstance(holder, types.ModuleType):
            orig = holder.tqdm
            holder.tqdm = Prog
            return lambda: setattr(holder, "tqdm", orig)
        if holder is not None:
            T.tqdm = Prog
            return lambda: setattr(T, "tqdm", holder)
    except Exception:  # noqa: BLE001
        pass
    return lambda: None


# Типичные «галлюцинации» Whisper на тишине/музыке (особенно в русском)
JUNK = (
    "субтитры сделал", "субтитры создавал", "редактор субтитров", "корректор",
    "продолжение следует", "спасибо за просмотр", "подписывайтесь на канал",
    "dimatorzok", "amara.org", "субтитры подогнал", "ставьте лайк",
)


def clean_segments(segs):
    out, prev = [], None
    for s in segs:
        t = (s.get("text") or "").strip()
        low = t.lower()
        if not t or any(j in low for j in JUNK):
            continue
        if prev is not None and low == prev:  # одинаковые фразы подряд — зацикливание
            continue
        prev = low
        out.append({"start": s["start"], "end": s["end"], "text": t})
    return out


def whisper_model_path(job, repo):
    from huggingface_hub import snapshot_download
    try:
        return snapshot_download(repo, local_files_only=True)
    except Exception:  # noqa: BLE001
        update(job, message="Скачиваю модель распознавания — только в первый раз, 2–5 минут…")
        return snapshot_download(repo)


def h_transcribe(job):
    src = Path(job["src"])
    o = job["opts"]
    try:
        import mlx_whisper
    except ImportError as e:
        raise RuntimeError("Не установлен mlx-whisper. Запустите install.sh ещё раз") from e

    lang = o.get("language", "ru")
    choice = o.get("model", "auto")
    if choice == "auto":  # казахский заметно лучше на большой модели
        choice = "large" if lang == "kk" else "turbo"
    repo = WHISPER_MODELS.get(choice, WHISPER_MODELS["turbo"])
    prompt = (o.get("prompt") or "").strip() or None

    # 1) Готовим чистый звук: моно 16 кГц, выровненная громкость
    update(job, message="Готовлю звук…", progress=1)
    wav = src.parent / "_audio16k.wav"
    run(["ffmpeg", "-hide_banner", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
         "-af", "highpass=f=60,dynaudnorm=f=200", "-c:a", "pcm_s16le", str(wav)])
    dur = media_duration(wav)

    # 2) Модель (из кэша; если её нет — скачиваем один раз)
    model_path = whisper_model_path(job, repo)

    # 3) Расшифровка; «пульс» в интерфейсе, чтобы было видно, что идёт работа
    started = time.time()
    stop = threading.Event()

    def ticker():
        while not stop.wait(1):
            el = int(time.time() - started)
            update(job, message=f"Расшифровываю… {el // 60}:{el % 60:02d}"
                                + (f" · запись {short_ts(dur)}" if dur else ""))

    threading.Thread(target=ticker, daemon=True).start()
    restore = _patch_tqdm(job)
    kwargs = dict(
        path_or_hf_repo=model_path, verbose=False, initial_prompt=prompt,
        condition_on_previous_text=False,       # не даёт модели зацикливаться
        compression_ratio_threshold=2.4, logprob_threshold=-1.0, no_speech_threshold=0.6,
        word_timestamps=True, hallucination_silence_threshold=2.0,  # режет выдумки на паузах
    )
    if lang and lang != "auto":
        kwargs["language"] = lang
    try:
        try:
            result = mlx_whisper.transcribe(str(wav), **kwargs)
        except TypeError:  # старая версия mlx-whisper без части параметров
            for k in ("word_timestamps", "hallucination_silence_threshold"):
                kwargs.pop(k, None)
            result = mlx_whisper.transcribe(str(wav), **kwargs)
    finally:
        stop.set()
        restore()

    segs = clean_segments(result.get("segments", []))
    if not segs:
        raise RuntimeError("В записи не нашлось речи (или выбран не тот язык)")
    folder = unique_dir(f"{src.stem} — расшифровка")
    stem = src.stem

    # Чистый текст абзацами (новый абзац при паузе > 1.5 сек)
    paras, cur, last_end = [], [], None
    for s in segs:
        if last_end is not None and s["start"] - last_end > 1.5 and cur:
            paras.append(" ".join(cur))
            cur = []
        cur.append(s["text"])
        last_end = s["end"]
    if cur:
        paras.append(" ".join(cur))
    (folder / f"{stem}.txt").write_text("\n\n".join(paras) + "\n", encoding="utf-8")

    (folder / f"{stem} (таймкоды).txt").write_text(
        "\n".join(f"[{short_ts(s['start'])}] {s['text']}" for s in segs) + "\n", encoding="utf-8")

    srt = []
    for i, s in enumerate(segs, start=1):
        srt += [str(i), f"{ts(s['start'])} --> {ts(s['end'])}", s["text"], ""]
    (folder / f"{stem}.srt").write_text("\n".join(srt), encoding="utf-8")
    vtt = ["WEBVTT", ""]
    for s in segs:
        vtt += [f"{ts(s['start'], '.')} --> {ts(s['end'], '.')}", s["text"], ""]
    (folder / f"{stem}.vtt").write_text("\n".join(vtt), encoding="utf-8")

    try:
        from docx import Document
        d = Document()
        d.add_heading(stem, level=1)
        for p in paras:
            d.add_paragraph(p)
        d.save(str(folder / f"{stem}.docx"))
    except Exception:  # noqa: BLE001
        pass

    # Сначала Word и чистый текст — их чаще всего открывают
    order = {".docx": 0, ".txt": 1, ".srt": 3, ".vtt": 4}
    files = sorted(folder.iterdir(), key=lambda f: (order.get(f.suffix, 9) + (1 if "таймкод" in f.name else 0), f.name))
    for f in files:
        add_output(job, f)
    el = int(time.time() - started)
    detected = result.get("language")
    job["note"] = f"Готово за {el // 60}:{el % 60:02d}" + (f" · язык: {detected}" if detected else "")


# ---------------------------------------------------------------------------
# СКАЧИВАНИЕ ПО ССЫЛКЕ (YouTube, TikTok, Instagram и ещё ~1800 сайтов — yt-dlp)
# ---------------------------------------------------------------------------
def hsize(b):
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if b < 1024 or unit == "ГБ":
            return f"{b:.0f} {unit}" if unit in ("Б", "КБ") else f"{b:.1f} {unit}"
        b /= 1024
    return ""


def friendly_dl_error(msg):
    low = msg.lower()
    if any(k in low for k in ("unable to connect", "timed out", "connection", "getaddrinfo", "network is unreachable")):
        return "Нет связи с сайтом. Проверьте интернет (или VPN) и попробуйте ещё раз."
    if any(k in low for k in ("login required", "log in", "--cookies", "cookies-from-browser", "sign in",
                              "private video", "rate-limit", "age-restricted", "confirm your age")):
        return ("Сайт просит вход в аккаунт. Откройте «Дополнительно» → «Вход из браузера» "
                "и выберите браузер, где вы залогинены на этом сайте.")
    if "unsupported url" in low:
        return "Эту ссылку скачать не получится — сайт не поддерживается."
    if "not available" in low or "404" in low:
        return "Видео недоступно: удалено, закрыто или ссылка неверная."
    tail = msg.strip().splitlines()[-1] if msg.strip() else "Ошибка"
    return tail.replace("ERROR: ", "") + "\nЕсли раньше работало — перезапустите DoxKit, он обновит загрузчик."


def h_download(job):
    o = job["opts"]
    url = job["url"]
    try:
        import yt_dlp
    except ImportError as e:
        raise RuntimeError("Не установлен yt-dlp. Запустите install.sh ещё раз") from e

    mode = o.get("mode", "video")
    q = o.get("dl_quality", "1080")
    update(job, message="Открываю ссылку…", progress=1)

    def hook(d):
        if d.get("status") == "downloading":
            tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            got = d.get("downloaded_bytes") or 0
            if tot:
                update(job, progress=max(1, min(97, int(got / tot * 100))))
            update(job, message=f"Скачиваю… {hsize(got)}" + (f" из {hsize(tot)}" if tot else ""))
        elif d.get("status") == "finished":
            update(job, message="Собираю файл…", progress=98)

    nowm = "[format_note!*=atermark]"  # TikTok: пропускаем версии с водяным знаком
    opts = {
        "outtmpl": str(OUT_ROOT / "%(title).120B [%(id)s].%(ext)s"),
        "noplaylist": True, "quiet": True, "no_warnings": True, "noprogress": True,
        "progress_hooks": [hook], "ffmpeg_location": shutil.which("ffmpeg"),
        "windowsfilenames": True, "retries": 5, "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
    }
    if mode == "audio":
        opts["format"] = "ba/b"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                   "preferredquality": "192"}]
    else:
        opts["format"] = f"bv*{nowm}+ba/b{nowm}/bv*+ba/b"
        opts["merge_output_format"] = "mp4"
        if q == "best":
            opts["format_sort"] = ["res", "fps", "vcodec:h264", "acodec:m4a"]
        else:  # H.264 — открывается везде: QuickTime, Telegram, Premiere, iPhone
            opts["format_sort"] = [f"res:{q}", "vcodec:h264", "acodec:m4a"]
    browser = o.get("browser", "none")
    if browser and browser != "none":
        opts["cookiesfrombrowser"] = (browser,)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(friendly_dl_error(str(e))) from None

    paths = []
    for e in (info.get("entries") or [info]):
        if not e:
            continue
        for rd in e.get("requested_downloads") or []:
            p = rd.get("filepath") or rd.get("_filename")
            if p and Path(p).exists():
                paths.append(Path(p))
        if not paths and e.get("id"):
            paths += [p for p in OUT_ROOT.glob(f"*[[]{e['id']}[]].*") if p.suffix != ".part"]
    paths = list(dict.fromkeys(paths))
    if not paths:
        raise RuntimeError("Файл скачался, но я не нашёл его в папке DoxKit")

    update(job, name=info.get("title") or job["name"], size=sum(p.stat().st_size for p in paths))
    for p in paths:
        add_output(job, p)

    if o.get("then_transcribe"):
        for p in paths:
            d = WORK_DIR / uuid.uuid4().hex
            d.mkdir(parents=True)
            dst = d / p.name
            try:
                os.link(p, dst)  # мгновенно, без копирования
            except OSError:
                shutil.copy2(p, dst)
            t = new_job(action="transcribe", kind="video", name=p.name, src=str(dst),
                        size=dst.stat().st_size,
                        opts={"language": o.get("language", "ru"), "model": "auto"})
            Q_WHISPER.put(t)
        job["note"] = "Скачано · расшифровка уже в очереди ниже"
    else:
        job["note"] = "Скачано"


def update_ytdlp_in_background():
    """Сайты часто меняются — раз в 2 дня тихо обновляем загрузчик."""
    import sys
    stamp = APP_DIR / ".ytdlp_updated"
    try:
        if stamp.exists() and time.time() - stamp.stat().st_mtime < 2 * 86400:
            return
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "yt-dlp[default]"],
                       capture_output=True, timeout=300)
        stamp.touch()
    except Exception:  # noqa: BLE001
        pass


HANDLERS = {"compress": h_compress, "convert": h_convert, "transcribe": h_transcribe,
            "download": h_download}

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
app = Flask(__name__)


@app.get("/")
def index():
    return send_file(APP_DIR / "index.html")


@app.get("/api/status")
def status():
    return jsonify({
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ghostscript": bool(shutil.which("gs")),
        "libreoffice": bool(soffice_path()),
        "whisper": importlib.util.find_spec("mlx_whisper") is not None,
        "pdf2docx": importlib.util.find_spec("pdf2docx") is not None,
        "ytdlp": importlib.util.find_spec("yt_dlp") is not None,
        "out_dir": str(OUT_ROOT),
    })


@app.post("/api/jobs")
def create_jobs():
    action = request.form.get("action", "")
    if action not in HANDLERS:
        abort(400)
    opts = json.loads(request.form.get("opts") or "{}")
    targets = opts.get("targets", {})
    created = []
    if action == "download":
        for url in [u.strip() for u in (request.form.get("urls") or "").split() if u.strip()]:
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            job_dir = WORK_DIR / uuid.uuid4().hex
            job_dir.mkdir(parents=True)
            job = new_job(action="download", kind="video", name=url, url=url, size=0,
                          src=str(job_dir / "_"), opts=opts)
            Q_DL.put(job)
            created.append(public(job))
        return jsonify(created)
    for f in request.files.getlist("files"):
        name = Path(f.filename or "file").name.replace(":", "-")
        ext = Path(name).suffix.lower().lstrip(".")
        kind = kind_of(ext)
        job_dir = WORK_DIR / uuid.uuid4().hex
        job_dir.mkdir(parents=True)
        dest = job_dir / name
        f.save(dest)
        job = new_job(action=action, kind=kind, name=name, src=str(dest), opts=opts,
                      size=dest.stat().st_size, target=targets.get(kind))
        error = None
        if not kind:
            error = f"Формат .{ext} не поддерживается"
        elif action == "transcribe" and kind not in ("audio", "video"):
            error = "Расшифровать можно только аудио или видео"
        elif action == "convert" and not job["target"]:
            error = "Не выбран формат для этого файла"
        if error:
            update(job, status="error", message=error)
            shutil.rmtree(job_dir, ignore_errors=True)
        else:
            (Q_WHISPER if action == "transcribe" else Q_FAST).put(job)
        created.append(public(job))
    return jsonify(created)


@app.get("/api/jobs")
def list_jobs():
    with JOBS_LOCK:
        jobs = sorted(JOBS.values(), key=lambda j: j["created"], reverse=True)
    return jsonify([public(j) for j in jobs])


def _output(jid, idx):
    job = JOBS.get(jid)
    if not job or idx >= len(job["outputs"]):
        abort(404)
    return job["outputs"][idx]


@app.get("/api/file/<jid>/<int:idx>")
def download(jid, idx):
    o = _output(jid, idx)
    return send_file(o["path"], as_attachment=True, download_name=o["name"])


@app.post("/api/reveal/<jid>")
@app.post("/api/reveal/<jid>/<int:idx>")
def reveal(jid, idx=0):
    o = _output(jid, idx)
    subprocess.Popen(["open", "-R", o["path"]])
    return jsonify(ok=True)


@app.post("/api/open/<jid>/<int:idx>")
def open_output(jid, idx):
    o = _output(jid, idx)
    subprocess.Popen(["open", o["path"]])
    return jsonify(ok=True)


@app.post("/api/open-folder")
def open_folder():
    subprocess.Popen(["open", str(OUT_ROOT)])
    return jsonify(ok=True)


@app.post("/api/clear")
def clear():
    with JOBS_LOCK:
        for jid in [j for j, v in JOBS.items() if v["status"] in ("done", "error")]:
            del JOBS[jid]
    return jsonify(ok=True)


@app.post("/api/shutdown")
def shutdown():
    threading.Timer(0.3, lambda: os._exit(0)).start()
    return jsonify(ok=True)


def mac_identity():
    """Чтобы в Dock и в меню было «DoxKit» с нашей иконкой, а не «Python»."""
    try:
        from Foundation import NSBundle
        from AppKit import NSApplication, NSImage
        info = NSBundle.mainBundle().infoDictionary()
        if info is not None:
            info["CFBundleName"] = "DoxKit"
            info["CFBundleDisplayName"] = "DoxKit"
        icon = APP_DIR / "icon.icns"
        if icon.exists():
            img = NSImage.alloc().initWithContentsOfFile_(str(icon))
            NSApplication.sharedApplication().setApplicationIconImage_(img)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    import sys
    from werkzeug.serving import make_server

    URL = f"http://127.0.0.1:{PORT}"
    try:
        srv = make_server("127.0.0.1", PORT, app, threaded=True)
    except OSError:
        srv = None  # DoxKit уже запущен — просто откроем ещё одно окно к нему

    if srv:
        for _ in range(2):
            threading.Thread(target=worker, args=(Q_FAST,), daemon=True).start()
        threading.Thread(target=worker, args=(Q_WHISPER,), daemon=True).start()
        for _ in range(2):
            threading.Thread(target=worker, args=(Q_DL,), daemon=True).start()
        threading.Thread(target=update_ytdlp_in_background, daemon=True).start()

    try:
        import webview  # pywebview — нативное окно macOS
    except ImportError:
        webview = None

    if "--browser" in sys.argv or webview is None:
        subprocess.Popen(["open", URL])
        if srv:
            srv.serve_forever()
    else:
        if srv:
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        mac_identity()
        webview.create_window("DoxKit", URL, width=880, height=920,
                              min_size=(640, 600), background_color="#f5f5f7")
        webview.start()
        os._exit(0)  # закрыли окно — выключаемся полностью

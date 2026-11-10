#!/usr/bin/env python3
"""Servidor Web do Podcast Studio Núcleo da Fé."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from processar_podcast import (
    add_episode,
    clean_text,
    download_audio,
    download_video,
    encode_mp3,
    encode_mp4,
    generate_episode_artwork,
    https_url,
    is_compatible_mp4,
    load_env_file,
    load_feed,
    r2_client,
    required_env,
    run,
    save_feed,
    seconds,
    update_channel,
    video_details,
    ytdlp_cmd,
)
import batch_downloader
from spotify_uploader import (
    cancel_spotify_upload,
    confirm_login,
    get_spotify_state,
    run_spotify_upload,
)

LOG = logging.getLogger("studio")
load_env_file()

CURRENT_TASK: dict[str, Any] = {
    "status": "idle",  # idle, running, success, error
    "message": "Nenhuma publicação em andamento.",
    "progress": 0,
    "speed": None,
    "eta": None,
    "stage": None,
    "logs": [],
    "error": None,
    "video_url": None,
    "audio_url": None,
    "video_id": None,
    "title": None,
    "description": None,
    "local_mp4": None,
    "local_art": None,
    "local_mp3": None,
}
TASK_LOCK = threading.Lock()

MANUAL_EXPORT_TASK: dict[str, Any] = {
    "status": "idle",  # idle, running, completed, error
    "message": "Nenhum download em andamento.",
    "progress": 0,
    "speed": None,
    "eta": None,
    "stage": None,
    "video_id": None,
    "download_url": None,
    "filename": None,
    "error": None,
    "kind": "mp4",  # mp4 | mp3
}
MANUAL_EXPORT_LOCK = threading.Lock()


def set_manual_export(
    status: str,
    message: str,
    progress: int = 0,
    speed: str | None = None,
    eta: str | None = None,
    stage: str | None = None,
    video_id: str | None = None,
    download_url: str | None = None,
    filename: str | None = None,
    error: str | None = None,
) -> None:
    with MANUAL_EXPORT_LOCK:
        MANUAL_EXPORT_TASK["status"] = status
        MANUAL_EXPORT_TASK["message"] = message
        MANUAL_EXPORT_TASK["progress"] = progress
        if speed is not None:
            MANUAL_EXPORT_TASK["speed"] = speed
        if eta is not None:
            MANUAL_EXPORT_TASK["eta"] = eta
        if stage is not None:
            MANUAL_EXPORT_TASK["stage"] = stage
        if video_id is not None:
            MANUAL_EXPORT_TASK["video_id"] = video_id
        if download_url is not None:
            MANUAL_EXPORT_TASK["download_url"] = download_url
        if filename is not None:
            MANUAL_EXPORT_TASK["filename"] = filename
        if error is not None:
            MANUAL_EXPORT_TASK["error"] = error


def get_manual_export_state() -> dict[str, Any]:
    with MANUAL_EXPORT_LOCK:
        return dict(MANUAL_EXPORT_TASK)


def set_task(
    status: str,
    message: str,
    progress: int = 0,
    log_line: str | None = None,
    speed: str | None = None,
    eta: str | None = None,
    stage: str | None = None,
    error: str | None = None,
    video_url: str | None = None,
    audio_url: str | None = None,
    video_id: str | None = None,
    title: str | None = None,
    description: str | None = None,
    local_mp4: str | None = None,
    local_art: str | None = None,
    local_mp3: str | None = None,
) -> None:
    with TASK_LOCK:
        CURRENT_TASK["status"] = status
        CURRENT_TASK["message"] = message
        CURRENT_TASK["progress"] = progress
        if speed is not None:
            CURRENT_TASK["speed"] = speed
        if eta is not None:
            CURRENT_TASK["eta"] = eta
        if stage is not None:
            CURRENT_TASK["stage"] = stage
        if log_line:
            CURRENT_TASK["logs"].append(f"[{time.strftime('%H:%M:%S')}] {log_line}")
        if error:
            CURRENT_TASK["error"] = error
        if video_url is not None:
            CURRENT_TASK["video_url"] = video_url
        if audio_url is not None:
            CURRENT_TASK["audio_url"] = audio_url
        if video_id is not None:
            CURRENT_TASK["video_id"] = video_id
        if title is not None:
            CURRENT_TASK["title"] = title
        if description is not None:
            CURRENT_TASK["description"] = description
        if local_mp4 is not None:
            CURRENT_TASK["local_mp4"] = local_mp4
        if local_art is not None:
            CURRENT_TASK["local_art"] = local_art
        if local_mp3 is not None:
            CURRENT_TASK["local_mp3"] = local_mp3


def parse_video_id(input_str: str) -> str:
    input_str = input_str.strip()
    match = re.search(r"(?:v=|\/embed\/|\/watch\?v=|\/live\/|youtu\.be\/|^)([A-Za-z0-9_-]{11})(?:[?&]|$)", input_str)
    if match:
        return match.group(1)
    if re.fullmatch(r"^[A-Za-z0-9_-]{11}$", input_str):
        return input_str
    raise ValueError(f"Não foi possível identificar um ID de vídeo válido em: {input_str}")


def fetch_channel_videos(limit: int = 10) -> list[dict[str, Any]]:
    source_url = os.environ.get("PODCAST_SOURCE_URL", "https://www.youtube.com/@nucleodafelive/streams")
    cmd = [
        ytdlp_cmd(),
        "--dump-single-json",
        "--flat-playlist",
        "--playlist-end",
        str(limit),
        "--no-warnings",
        source_url,
    ]
    if Path("cookies.txt").exists():
        cmd += ["--cookies", "cookies.txt"]

    res = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"Erro ao listar vídeos do canal: {res.stderr.strip()[:300]}")

    data = json.loads(res.stdout)
    entries = data.get("entries") or [data]

    # GUIDs publicados em cada feed (áudio e vídeo)
    published_by_feed: dict[str, set[str]] = {"audio": set(), "video": set()}
    for f_name, f_type in (("podcast.xml", "audio"), ("vodcast.xml", "video")):
        feed_path = Path(f_name)
        if feed_path.exists():
            try:
                _, ch = load_feed(feed_path)
                published_by_feed[f_type] = feed_video_ids(ch)
            except Exception:
                pass
    published_guids = published_by_feed["audio"] | published_by_feed["video"]

    results: list[dict[str, Any]] = []
    for entry in entries:
        if not entry:
            continue
        v_id = entry.get("id", "")
        if not re.fullmatch(r"^[A-Za-z0-9_-]{11}$", v_id):
            continue
        raw_dur = entry.get("duration")
        dur_sec = int(float(raw_dur)) if raw_dur is not None else 0
        h = dur_sec // 3600
        m = (dur_sec % 3600) // 60
        s = dur_sec % 60
        dur_formatted = f"{h:02d}:{m:02d}:{s:02d}" if dur_sec > 0 else "Ao Vivo / Estreia"

        results.append({
            "id": v_id,
            "title": clean_text(entry.get("title", v_id)),
            "duration": dur_sec,
            "duration_formatted": dur_formatted,
            "is_short": (0 < dur_sec <= 4500),  # <= 75 min (pregação já cortada)
            "thumbnail": f"https://img.youtube.com/vi/{v_id}/mqdefault.jpg",
            "published": v_id in published_guids,
            "published_formats": [f for f in ("audio", "video") if v_id in published_by_feed[f]],
            "is_live": dur_sec == 0,
        })
    return results


def normalize_cut_bounds(start_str: str | None, end_str: str | None) -> tuple[int | None, int | None]:
    start_sec = seconds(start_str) if start_str and str(start_str).strip() else None
    end_sec = seconds(end_str) if end_str and str(end_str).strip() else None

    # Se ambos são None, 0 ou vazios: não há corte (vídeo completo)
    if (start_sec is None or start_sec == 0) and (end_sec is None or end_sec == 0):
        return None, None
    if start_sec is not None and end_sec is not None and start_sec >= end_sec:
        return None, None
    # Se início é 0 ou ausente e fim não foi definido ou é 0
    if (start_sec is None or start_sec == 0) and end_sec is None:
        return None, None
    return start_sec, end_sec


def sanitize_filename(name: str) -> str:
    """Sanitiza o título do episódio para uso seguro em nomes de arquivo em todos os SOs."""
    s = re.sub(r'\s*[\\/*?:\"<>|]+\s*', ' - ', name)
    s = re.sub(r'\s+', ' ', s)
    s = re.sub(r'(\s*-\s*)+', ' - ', s)
    return s.strip(' .-') or 'Episodio'


def get_download_filename(file_name: str, title: str | None = None) -> str:
    """Gera o nome de arquivo com base fixa no título e sufixos padronizados:
    - Vídeo: '{Titulo} - Video.mp4'
    - Capa quadrada 1:1: '{Titulo} - 1x1.jpg'
    - Foto do tema 16:9: '{Titulo} - 16-9.jpg'
    - Áudio: '{Titulo} - Audio.mp3'
    """
    if not title or not str(title).strip():
        return file_name
    base = sanitize_filename(str(title).strip())
    lower = file_name.lower()
    if lower.endswith(".mp4"):
        return f"{base} - Video.mp4"
    if lower.endswith("_thumb.jpg") or "16-9" in lower or "16x9" in lower or "thumb" in lower:
        return f"{base} - 16-9.jpg"
    if lower.endswith(".jpg") or lower.endswith(".jpeg"):
        return f"{base} - 1x1.jpg"
    if lower.endswith(".mp3"):
        return f"{base} - Audio.mp3"
    ext = Path(file_name).suffix
    return f"{base}{ext}"


# ---------------------------------------------------------------------------
# Helpers compartilhados entre Publicar, Download Manual e Robô do Spotify.
# Garantem que os três fluxos reutilizem o mesmo stream bruto (cache), o mesmo
# MP4 recortado (com meta.json) e a mesma capa (automática ou personalizada).
# ---------------------------------------------------------------------------

YT_ID_RE = re.compile(r"(?:v=|youtu\.be/|/live/|/embed/|^yt:)([A-Za-z0-9_-]{11})")


def item_video_id(item: Any) -> str | None:
    """Extrai o ID do YouTube de um <item> do feed (guid 'yt:ID' ou link do YouTube)."""
    for text in (item.findtext("guid", default=""), item.findtext("link", default=""),
                 item.findtext("description", default="")):
        m = YT_ID_RE.search(text or "")
        if m:
            return m.group(1)
    return None


def feed_video_ids(channel: Any) -> set[str]:
    return {vid for vid in (item_video_id(it) for it in channel.findall("item")) if vid}


def remove_existing_episode(channel: Any, video_id: str) -> int:
    """Remove itens do mesmo vídeo para que republicar substitua em vez de duplicar."""
    removed = 0
    for it in list(channel.findall("item")):
        if item_video_id(it) == video_id:
            channel.remove(it)
            removed += 1
    return removed


EXPORT_LOCKS: dict[str, threading.Lock] = {}
EXPORT_LOCKS_GUARD = threading.Lock()


def get_exports_dir() -> Path:
    d = Path("exports").resolve()
    d.mkdir(exist_ok=True)
    return d


def _video_lock(video_id: str) -> threading.Lock:
    with EXPORT_LOCKS_GUARD:
        return EXPORT_LOCKS.setdefault(video_id, threading.Lock())


def link_friendly_copy(src: Path, title: str | None) -> None:
    """Cria hardlink/cópia com o nome amigável baseado no título na pasta exports/."""
    if not title or not str(title).strip() or not src.exists():
        return
    dst = src.parent / get_download_filename(src.name, str(title).strip())
    if dst == src:
        return
    try:
        if dst.exists():
            dst.unlink()
        try:
            os.link(str(src), str(dst))
        except Exception:
            shutil.copy2(str(src), str(dst))
    except Exception as e:
        LOG.warning("Não foi possível criar cópia amigável %s: %s", dst.name, e)


def decode_cover_b64(custom_cover_b64: str | None) -> bytes | None:
    if not custom_cover_b64:
        return None
    try:
        c_data = custom_cover_b64.split(",", 1)[1] if "," in custom_cover_b64 else custom_cover_b64
        return base64.b64decode(c_data)
    except Exception as e:
        LOG.warning("Erro ao decodificar capa personalizada: %s", e)
        return None


def ensure_artwork(video_id: str, custom_cover_b64: str | None = None, title: str | None = None) -> Path:
    """Gera (ou reutiliza) a capa 1:1 em exports/{id}.jpg respeitando a capa personalizada."""
    exports_dir = get_exports_dir()
    art_path = exports_dir / f"{video_id}.jpg"
    marker = exports_dir / f"{video_id}.cover.json"
    custom_bytes = decode_cover_b64(custom_cover_b64)
    wanted = {"source": "custom", "digest": hashlib.sha1(custom_bytes).hexdigest()} if custom_bytes else {"source": "auto"}

    current: dict[str, Any] = {"source": "auto"}
    if marker.is_file():
        try:
            current = json.loads(marker.read_text(encoding="utf-8"))
        except Exception:
            current = {}

    if not art_path.is_file() or current != wanted:
        generate_episode_artwork(video_id, art_path, custom_image_bytes=custom_bytes)
        marker.write_text(json.dumps(wanted), encoding="utf-8")
    link_friendly_copy(art_path, title)
    return art_path


def probe_duration(path: Path) -> int:
    probe = json.loads(run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)]))
    return round(float(probe["format"]["duration"]))


def export_matches(video_id: str, start_sec: int | None, end_sec: int | None, quality: str) -> dict[str, Any] | None:
    """Retorna o meta.json salvo se o MP4 exportado corresponde exatamente ao corte/qualidade pedidos."""
    exports_dir = get_exports_dir()
    mp4 = exports_dir / f"{video_id}.mp4"
    meta_file = exports_dir / f"{video_id}.meta.json"
    if not mp4.is_file() or mp4.stat().st_size == 0:
        return None
    # MP4 antigos podem ter vídeo AV1 copiado (não abre no QuickTime/Spotify): força regenerar
    if not is_compatible_mp4(mp4):
        return None
    if meta_file.is_file():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            return None
        if (meta.get("start_sec") == start_sec and meta.get("end_sec") == end_sec
                and str(meta.get("quality", "720")) == str(quality)):
            return meta
        return None
    # Arquivos antigos sem meta: só valem para vídeo completo
    if start_sec is None and end_sec is None:
        return {"video_id": video_id, "start_sec": None, "end_sec": None, "quality": quality, "title": ""}
    return None


def _cover_state(video_id: str) -> dict[str, Any]:
    marker = get_exports_dir() / f"{video_id}.cover.json"
    try:
        return json.loads(marker.read_text(encoding="utf-8")) if marker.is_file() else {"source": "auto"}
    except Exception:
        return {"source": "auto"}


def write_mp3_meta(video_id: str, start_sec: int | None, end_sec: int | None, title: str | None,
                   size: int, duration: int) -> None:
    (get_exports_dir() / f"{video_id}.mp3.json").write_text(json.dumps({
        "video_id": video_id,
        "title": (title or "").strip(),
        "start_sec": start_sec,
        "end_sec": end_sec,
        "cover": _cover_state(video_id),
        "size": size,
        "duration": duration,
        "updated_at": time.time(),
    }, indent=2), encoding="utf-8")


def mp3_export_matches(video_id: str, start_sec: int | None, end_sec: int | None) -> dict[str, Any] | None:
    """Meta do MP3 exportado se ele corresponde ao corte e à capa atuais."""
    exports_dir = get_exports_dir()
    mp3 = exports_dir / f"{video_id}.mp3"
    meta_file = exports_dir / f"{video_id}.mp3.json"
    if not mp3.is_file() or mp3.stat().st_size == 0 or not meta_file.is_file():
        return None
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    if meta.get("start_sec") == start_sec and meta.get("end_sec") == end_sec and meta.get("cover") == _cover_state(video_id):
        return meta
    return None


def ensure_export_mp3(
    video_id: str,
    start_str: str | None,
    end_str: str | None,
    title: str | None = None,
    custom_cover_b64: str | None = None,
    *,
    force: bool = False,
    on_dl: Any = None,
    on_enc: Any = None,
    report: Any = None,
    cancel: Any = None,
    register: Any = None,
) -> dict[str, Any]:
    """Garante exports/{id}.mp3 (-16 LUFS, capa embutida) com o corte pedido.

    Fonte, da mais rápida para a mais lenta: MP4 já exportado com o mesmo corte →
    stream de vídeo em cache → áudio em cache → download só do áudio no YouTube.
    """
    def say(stage: str, msg: str, pct: int) -> None:
        if report:
            report(stage, msg, pct)

    lock = _video_lock(video_id)
    if not lock.acquire(blocking=False):
        say("downloading", "Aguardando outra exportação deste mesmo vídeo terminar...", 5)
        lock.acquire()
    try:
        exports_dir = get_exports_dir()
        cache_dir = exports_dir / ".cache"
        cache_dir.mkdir(exist_ok=True)
        mp3_path = exports_dir / f"{video_id}.mp3"
        start_sec, end_sec = normalize_cut_bounds(start_str, end_str)

        say("artwork", "Preparando capa para embutir no MP3...", 8)
        art_path = ensure_artwork(video_id, custom_cover_b64, title)

        if not force:
            meta = mp3_export_matches(video_id, start_sec, end_sec)
            if meta:
                say("complete", "MP3 com este corte já existe — reutilizando.", 100)
                link_friendly_copy(mp3_path, title)
                return {"mp3": mp3_path, "size": mp3_path.stat().st_size, "duration": meta.get("duration") or 0, "reused": True}

        source: Path | None = None
        src_start, src_end = start_sec, end_sec
        mp4_meta = None
        for q in ("720", "1080", "480", "360"):
            mp4_meta = export_matches(video_id, start_sec, end_sec, q)
            if mp4_meta:
                break
        if mp4_meta:
            source, src_start, src_end = exports_dir / f"{video_id}.mp4", None, None  # já recortado
            say("encoding", "Extraindo áudio do MP4 já recortado (sem baixar)...", 15)
        else:
            raw_videos = sorted(cache_dir.glob(f"raw_{video_id}_*.mp4"), key=lambda pth: pth.stat().st_size)
            raw_audio = sorted(cache_dir.glob(f"raw_audio_{video_id}.*"))
            if raw_videos:
                source = raw_videos[0]
                say("encoding", "Extraindo áudio do stream em cache (sem baixar)...", 15)
            elif raw_audio and raw_audio[0].stat().st_size > 10_000:
                source = raw_audio[0]
                say("encoding", "Reutilizando áudio já baixado do cache...", 15)
            else:
                say("downloading", "Baixando somente o áudio do YouTube...", 10)
                with tempfile.TemporaryDirectory(prefix="dl-audio-") as dl_tmp:
                    a_file = download_audio(
                        video_id, Path(dl_tmp),
                        progress_callback=on_dl, cancellation_check=cancel, register_proc=register,
                    )
                    source = cache_dir / f"raw_audio_{video_id}{a_file.suffix}"
                    shutil.move(str(a_file), str(source))

        say("encoding", "Normalizando áudio (-16 LUFS) e gerando MP3...", 55)
        tmp_out = exports_dir / f"{video_id}.tmp.mp3"  # só substitui o MP3 final quando terminar
        try:
            size, duration = encode_mp3(
                source, tmp_out, src_start, src_end, artwork_path=art_path,
                progress_callback=on_enc, cancellation_check=cancel, register_proc=register,
            )
            os.replace(tmp_out, mp3_path)
        finally:
            if tmp_out.exists():
                try:
                    tmp_out.unlink()
                except Exception:
                    pass

        write_mp3_meta(video_id, start_sec, end_sec, title, size, duration)
        link_friendly_copy(mp3_path, title)
        return {"mp3": mp3_path, "size": size, "duration": duration, "reused": False}
    finally:
        lock.release()


def ensure_export_mp4(
    video_id: str,
    start_str: str | None,
    end_str: str | None,
    quality: str = "720",
    title: str | None = None,
    *,
    force: bool = False,
    on_dl: Any = None,
    on_enc: Any = None,
    report: Any = None,
    cancel: Any = None,
    register: Any = None,
) -> dict[str, Any]:
    """Garante exports/{id}.mp4 com o corte e qualidade pedidos.

    Reutiliza o MP4 já exportado quando o corte coincide e o stream bruto em
    exports/.cache quando já foi baixado. Grava meta.json e cópia com nome amigável.
    """
    def say(stage: str, msg: str, pct: int) -> None:
        if report:
            report(stage, msg, pct)

    lock = _video_lock(video_id)
    if not lock.acquire(blocking=False):
        say("downloading", "Aguardando outra exportação deste mesmo vídeo terminar...", 5)
        lock.acquire()
    try:
        exports_dir = get_exports_dir()
        cache_dir = exports_dir / ".cache"
        cache_dir.mkdir(exist_ok=True)
        mp4_path = exports_dir / f"{video_id}.mp4"
        meta_path = exports_dir / f"{video_id}.meta.json"
        start_sec, end_sec = normalize_cut_bounds(start_str, end_str)
        is_cut = start_sec is not None and end_sec is not None

        if not force:
            meta = export_matches(video_id, start_sec, end_sec, quality)
            if meta:
                say("complete", "MP4 com este corte já existe — reutilizando sem baixar novamente.", 100)
                if title and title.strip() and meta.get("title") != title.strip() and meta_path.is_file():
                    meta["title"] = title.strip()
                    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
                link_friendly_copy(mp4_path, title)
                duration = meta.get("duration") or probe_duration(mp4_path)
                return {"mp4": mp4_path, "size": mp4_path.stat().st_size, "duration": duration, "reused": True, "is_cut": is_cut}

        raw_source = cache_dir / f"raw_{video_id}_{quality}.mp4"
        avc_tried = cache_dir / f"raw_{video_id}_{quality}.avc-tried"
        # Stream em cache que não é H.264 (ex.: AV1 de downloads antigos): baixa de novo uma vez
        # preferindo H.264, o que evita uma reconversão lenta. Se o YouTube não tiver H.264, reconverte.
        if raw_source.is_file() and not avc_tried.exists() and not is_compatible_mp4(raw_source):
            say("downloading", "Stream em cache está em AV1/VP9; baixando versão H.264 compatível...", 8)
            try:
                raw_source.unlink()
            except Exception:
                pass
        if raw_source.is_file() and raw_source.stat().st_size > 100_000:
            say("encoding" if is_cut else "packaging", f"Reutilizando stream já baixado do cache ({quality}p)...", 50)
        else:
            say("downloading", f"Baixando vídeo do YouTube ({quality}p)...", 10)
            with tempfile.TemporaryDirectory(prefix="dl-raw-") as dl_tmp:
                dl_file = download_video(
                    video_id,
                    Path(dl_tmp),
                    quality=quality,
                    progress_callback=on_dl,
                    cancellation_check=cancel,
                    register_proc=register,
                )
                shutil.move(str(dl_file), str(raw_source))
            avc_tried.write_text("1", encoding="utf-8")

        for old in (mp4_path, meta_path):
            if old.exists():
                try:
                    old.unlink()
                except Exception:
                    pass

        say("encoding" if is_cut else "packaging",
            "Recortando e otimizando MP4 com FFmpeg..." if is_cut else "Otimizando vídeo completo (+faststart)...", 55)
        try:
            size, duration = encode_mp4(
                raw_source,
                mp4_path,
                start_sec,
                end_sec,
                progress_callback=on_enc,
                cancellation_check=cancel,
                register_proc=register,
            )
        except BaseException:
            if mp4_path.exists():
                try:
                    mp4_path.unlink()
                except Exception:
                    pass
            raise

        meta_path.write_text(json.dumps({
            "video_id": video_id,
            "title": (title or "").strip(),
            "start_str": start_str,
            "end_str": end_str,
            "start_sec": start_sec,
            "end_sec": end_sec,
            "quality": quality,
            "is_cut": is_cut,
            "size": size,
            "duration": duration,
            "updated_at": time.time(),
        }, indent=2), encoding="utf-8")
        link_friendly_copy(mp4_path, title)
        return {"mp4": mp4_path, "size": size, "duration": duration, "reused": False, "is_cut": is_cut}
    finally:
        lock.release()


MANUAL_EXPORT_CANCEL_EVENT = threading.Event()
MANUAL_EXPORT_PROCS: set[subprocess.Popen] = set()

PUBLISH_CANCEL_EVENT = threading.Event()
PUBLISH_PROCS: set[subprocess.Popen] = set()


def cancel_manual_export() -> dict[str, Any]:
    MANUAL_EXPORT_CANCEL_EVENT.set()
    for p in list(MANUAL_EXPORT_PROCS):
        try:
            p.terminate()
            p.kill()
        except Exception:
            pass
    MANUAL_EXPORT_PROCS.clear()
    set_manual_export(
        "idle",
        "Download cancelado pelo usuário.",
        progress=0,
        stage="idle",
    )
    return {"ok": True, "message": "Download de vídeo interrompido com sucesso."}


def cancel_publish() -> dict[str, Any]:
    PUBLISH_CANCEL_EVENT.set()
    for p in list(PUBLISH_PROCS):
        try:
            p.terminate()
            p.kill()
        except Exception:
            pass
    PUBLISH_PROCS.clear()
    set_task(
        "idle",
        "Publicação cancelada pelo usuário.",
        progress=0,
    )
    return {"ok": True, "message": "Publicação cancelada com sucesso."}


def publish_worker(
    video_id: str,
    start_str: str | None,
    end_str: str | None,
    title_override: str | None,
    git_push: bool,
    media_format: str = "audio",  # "audio", "video", "both"
    custom_cover_b64: str | None = None,
    quality: str = "720",
    description_override: str | None = None,
) -> None:
    PUBLISH_CANCEL_EVENT.clear()
    PUBLISH_PROCS.clear()

    def reg_pub_proc(p: subprocess.Popen):
        PUBLISH_PROCS.add(p)

    def is_pub_cancelled() -> bool:
        return PUBLISH_CANCEL_EVENT.is_set()

    try:
        set_task("running", f"Iniciando publicação ({media_format}) do vídeo {video_id}...", 5, "Buscando detalhes do vídeo no YouTube...", speed="--", eta="--", stage="preparing")
        details = video_details(video_id)
        video_title = title_override.strip() if title_override and title_override.strip() else details.get("title", video_id)
        set_task("running", f"Vídeo identificado: {video_title}", 15, f"Título: {video_title} (Duração original: {int(details.get('duration', 0)) // 60} min)")

        start_sec, end_sec = normalize_cut_bounds(start_str, end_str)
        if (start_sec is not None) ^ (end_sec is not None):
            raise ValueError("Defina ambos início e fim para realizar o corte.")
        if start_sec is not None and end_sec is not None:
            if end_sec <= start_sec or end_sec - start_sec < 900:
                raise ValueError("O corte deve ter no mínimo 15 minutos.")
            if end_sec > int(details["duration"]) + 3:
                raise ValueError("O final do corte supera a duração do vídeo.")

        with tempfile.TemporaryDirectory(prefix="podcast-") as temp_dir:
            directory = Path(temp_dir)
            client = r2_client()
            bucket = required_env("R2_BUCKET_NAME")
            public_domain = https_url(required_env("R2_PUBLIC_DOMAIN"), "R2_PUBLIC_DOMAIN")
            feed_url_base = https_url(required_env("PODCAST_FEED_URL"), "PODCAST_FEED_URL")
            image_url = https_url(required_env("PODCAST_IMAGE_URL"), "PODCAST_IMAGE_URL")
            email = required_env("PODCAST_EMAIL")
            spec = {"video_id": video_id}
            if title_override and title_override.strip():
                spec["title"] = title_override.strip()
            if description_override and description_override.strip():
                spec["description"] = description_override.strip()

            published_info: list[str] = []
            out_v_url = None
            out_a_url = None

            # 1. Capa quadrada (2048x2048) — mesma usada no download manual e no robô do Spotify
            set_task("running", "Gerando capa quadrada do episódio (2048x2048)...", 20, "Processando arte oficial do episódio para Spotify e Apple Podcasts...", stage="artwork")
            art_file = ensure_artwork(video_id, custom_cover_b64, video_title)

            art_key = f"episodes/{video_id}.jpg"
            client.upload_file(str(art_file), bucket, art_key, ExtraArgs={"ContentType": "image/jpeg"})
            ep_image_url = f"{public_domain}/{art_key}"
            published_info.append(f"Capa: {ep_image_url}")

            if media_format in ("video", "both"):
                def on_dl(pct: float, spd: str, eta: str):
                    mapped = int(25 + (pct * 0.25))
                    set_task("running", f"Baixando vídeo ({quality}p)... {pct:.1f}%", mapped, speed=spd, eta=eta, stage="downloading")

                def on_enc(pct: float, spd: str, eta: str):
                    mapped = int(50 + (pct * 0.20))
                    set_task("running", f"Codificando MP4... {pct:.1f}%", mapped, speed=spd, eta=eta, stage="encoding")

                def on_report(stage: str, msg: str, pct: int):
                    mapped = 25 if stage == "downloading" else (70 if stage == "complete" else 50)
                    set_task("running", msg, mapped, msg, stage=stage if stage != "complete" else "encoding")

                export = ensure_export_mp4(
                    video_id,
                    start_str,
                    end_str,
                    quality,
                    video_title,
                    on_dl=on_dl,
                    on_enc=on_enc,
                    report=on_report,
                    cancel=is_pub_cancelled,
                    register=reg_pub_proc,
                )
                mp4 = export["mp4"]
                v_size, v_dur = export["size"], export["duration"]

                set_task("running", f"Enviando vídeo ({v_size // 1024 // 1024} MB) para o Cloudflare R2...", 70, "Upload do vídeo MP4 para o Cloudflare R2...", stage="upload_r2")
                v_key = f"episodes/{video_id}.mp4"
                client.upload_file(str(mp4), bucket, v_key, ExtraArgs={"ContentType": "video/mp4"})
                v_url = f"{public_domain}/{v_key}"
                out_v_url = v_url
                published_info.append(f"Vídeo MP4: {v_url}")

                vodcast_path = Path("vodcast.xml")
                v_root, v_channel = load_feed(vodcast_path)
                v_feed_url = feed_url_base.replace("podcast.xml", "vodcast.xml")
                update_channel(v_channel, v_feed_url, image_url, email, title_suffix=" (Vídeo)")
                remove_existing_episode(v_channel, video_id)
                add_episode(v_channel, details, spec, v_url, v_size, v_dur, media_type="video/mp4", image_url=ep_image_url)
                save_feed(v_root, vodcast_path)

                if media_format == "both":
                    set_task("running", "Normalizando áudio para -16 LUFS...", 80, "Extraindo áudio do vídeo para MP3 com capa embutida...", stage="encoding")
                    mp3 = directory / f"{video_id}.mp3"
                    # O MP4 exportado já está recortado: extrai o áudio dele sem novo corte
                    a_size, a_dur = encode_mp3(mp4, mp3, None, None, artwork_path=art_file)

                    set_task("running", f"Enviando áudio ({a_size // 1024 // 1024} MB) para o R2...", 90, "Upload do áudio MP3 para o Cloudflare R2...", stage="upload_r2")
                    a_key = f"episodes/{video_id}.mp3"
                    client.upload_file(str(mp3), bucket, a_key, ExtraArgs={"ContentType": "audio/mpeg"})
                    a_url = f"{public_domain}/{a_key}"
                    out_a_url = a_url
                    published_info.append(f"Áudio MP3: {a_url}")

                    feed_path = Path("podcast.xml")
                    root, channel = load_feed(feed_path)
                    update_channel(channel, feed_url_base, image_url, email)
                    remove_existing_episode(channel, video_id)
                    add_episode(channel, details, spec, a_url, a_size, a_dur, media_type="audio/mpeg", image_url=ep_image_url)
                    save_feed(root, feed_path)

            else:
                # Audio only
                set_task("running", "Baixando áudio do YouTube...", 30, f"Executando download do áudio de {video_id}...")
                audio_src = download_audio(
                    video_id,
                    directory,
                    cancellation_check=is_pub_cancelled,
                    register_proc=reg_pub_proc,
                )
                set_task("running", "Normalizando áudio para -16 LUFS com capa embutida...", 55, "Processando FFmpeg loudnorm e MP3...")
                mp3 = directory / f"{video_id}.mp3"
                size, duration = encode_mp3(audio_src, mp3, start_sec, end_sec, artwork_path=art_file)

                set_task("running", f"Áudio gerado ({size // 1024 // 1024} MB, {duration // 60} min). Enviando ao R2...", 75, "Fazendo upload para o Cloudflare R2...", stage="upload_r2")
                key = f"episodes/{video_id}.mp3"
                client.upload_file(str(mp3), bucket, key, ExtraArgs={"ContentType": "audio/mpeg"})
                audio_url = f"{public_domain}/{key}"
                out_a_url = audio_url
                published_info.append(f"Áudio MP3: {audio_url}")

                feed_path = Path("podcast.xml")
                root, channel = load_feed(feed_path)
                update_channel(channel, feed_url_base, image_url, email)
                remove_existing_episode(channel, video_id)
                add_episode(channel, details, spec, audio_url, size, duration, media_type="audio/mpeg", image_url=ep_image_url)
                save_feed(root, feed_path)

            # Update video_state.json if exists
            state_path = Path("video_state.json")
            if state_path.exists():
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    if "videos" in state and video_id in state["videos"]:
                        state["videos"][video_id]["status"] = "published"
                        state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                except Exception:
                    pass

            # Capa e MP4 já estão em exports/ (ensure_artwork / ensure_export_mp4).
            # Aqui só persistimos o MP3 para download local.
            exports_dir = get_exports_dir()
            local_art_path = f"/exports/{video_id}.jpg" if art_file.exists() else None
            local_mp4_path = f"/exports/{video_id}.mp4" if (exports_dir / f"{video_id}.mp4").exists() and media_format in ("video", "both") else None
            local_mp3_path = None

            if (directory / f"{video_id}.mp3").exists():
                local_mp3 = exports_dir / f"{video_id}.mp3"
                shutil.copy2(str(directory / f"{video_id}.mp3"), str(local_mp3))
                local_mp3_path = f"/exports/{video_id}.mp3"
                link_friendly_copy(local_mp3, video_title)
                try:
                    write_mp3_meta(video_id, start_sec, end_sec, video_title,
                                   local_mp3.stat().st_size, probe_duration(local_mp3))
                except Exception as exc:
                    LOG.warning("Não foi possível gravar meta do MP3: %s", exc)

            if git_push:
                set_task("running", "Enviando alterações para o GitHub Pages...", 95, "Executando git commit e push...", stage="git_push")
                files_to_commit = ["podcast.xml", "video_state.json"]
                if Path("vodcast.xml").exists():
                    files_to_commit.append("vodcast.xml")
                subprocess.run(["git", "add"] + files_to_commit, check=False)
                subprocess.run(["git", "commit", "-m", f"Publicar episódio {video_id} ({media_format})"], check=False)
                push_res = subprocess.run(["git", "push"], capture_output=True, text=True, check=False)
                if push_res.returncode == 0:
                    set_task("running", "GitHub Pages atualizado com sucesso!", 98, "Git push concluído com sucesso.")
                else:
                    set_task("running", "Feed gerado localmente (push pendente).", 98, f"Aviso no git push: {push_res.stderr.strip()}")

        summary = " | ".join(published_info)
        set_task(
            "success",
            f"Episódio '{video_title}' publicado com sucesso!",
            100,
            f"Publicação finalizada com sucesso! {summary}",
            stage="complete",
            video_url=out_v_url,
            audio_url=out_a_url,
            video_id=video_id,
            title=video_title,
            description=(description_override or "").strip() or details.get("description", ""),
            local_mp4=local_mp4_path,
            local_art=local_art_path,
            local_mp3=local_mp3_path,
        )
    except InterruptedError:
        LOG.info("publish_worker cancelado pelo usuário.")
        set_task("idle", "Publicação cancelada pelo usuário.", 0, "Publicação cancelada pelo usuário.")
    except Exception as exc:
        if PUBLISH_CANCEL_EVENT.is_set():
            set_task("idle", "Publicação cancelada pelo usuário.", 0, "Publicação cancelada pelo usuário.")
            return
        LOG.exception("Erro ao publicar episódio: %s", exc)
        set_task("error", f"Falha na publicação: {exc}", 0, f"ERRO: {exc}", stage="error", error=str(exc))


def spotify_upload_worker(
    video_id: str,
    title: str | None,
    description: str | None,
    quality: str = "720",
    start_str: str | None = None,
    end_str: str | None = None,
    custom_cover_b64: str | None = None,
) -> None:
    from spotify_uploader import set_spotify_state

    try:
        if not title or not description:
            try:
                d = video_details(video_id)
                if not title:
                    title = d.get("title", video_id)
                if not description:
                    description = d.get("description", "")
            except Exception:
                if not title:
                    title = video_id
                if not description:
                    description = ""

        set_spotify_state("preparing", "Preparando capa quadrada do episódio...", stage="artwork")
        art_path = ensure_artwork(video_id, custom_cover_b64, title)

        def on_sp_dl(pct: float, spd: str, eta: str):
            set_spotify_state("preparing", f"Baixando vídeo ({quality}p)... {pct:.1f}%",
                              progress=int(pct), speed=spd, eta=eta, stage="downloading")

        def on_sp_enc(pct: float, spd: str, eta: str):
            set_spotify_state("preparing", f"Recortando e otimizando vídeo MP4 (+faststart)... {pct:.1f}%",
                              progress=int(pct), speed=spd, eta=eta, stage="encoding")

        def on_sp_report(stage: str, msg: str, pct: int):
            set_spotify_state("preparing", msg, progress=pct, stage=stage)

        # Reutiliza o MP4 exportado SOMENTE se o corte/qualidade coincidirem (meta.json)
        export = ensure_export_mp4(
            video_id, start_str, end_str, quality, title,
            on_dl=on_sp_dl, on_enc=on_sp_enc, report=on_sp_report,
        )

        run_spotify_upload(
            video_path=export["mp4"],
            artwork_path=art_path if art_path.exists() else None,
            title=title,
            description=description or "",
        )
    except Exception as exc:
        LOG.exception("Erro no fluxo do Spotify: %s", exc)
        set_spotify_state("error", f"Erro ao preparar envio: {exc}", error=str(exc))


def manual_export_worker(
    video_id: str,
    start_str: str | None = None,
    end_str: str | None = None,
    quality: str = "720",
    force: bool = False,
    title: str | None = None,
    custom_cover_b64: str | None = None,
    kind: str = "mp4",
) -> None:
    MANUAL_EXPORT_CANCEL_EVENT.clear()
    MANUAL_EXPORT_PROCS.clear()
    with MANUAL_EXPORT_LOCK:
        MANUAL_EXPORT_TASK.update({"kind": kind, "download_url": None, "filename": None, "error": None})

    # Se o título não foi passado, tenta obter via YouTube para nomear os arquivos
    if not title or not str(title).strip():
        try:
            title = video_details(video_id).get("title", "")
        except Exception:
            title = ""

    def reg_manual_proc(p: subprocess.Popen):
        MANUAL_EXPORT_PROCS.add(p)

    def is_manual_cancelled() -> bool:
        return MANUAL_EXPORT_CANCEL_EVENT.is_set()

    try:
        start_sec, end_sec = normalize_cut_bounds(start_str, end_str)
        is_cut = start_sec is not None and end_sec is not None

        def on_report(stage: str, msg: str, pct: int):
            set_manual_export("running", msg, progress=pct if pct < 100 else 99, stage=stage, video_id=video_id)

        if kind == "mp3":
            set_manual_export("running", f"Preparando áudio MP3 do vídeo {video_id}...",
                              progress=5, speed="--", eta="--", stage="downloading", video_id=video_id)

            def on_dl_a(pct: float, spd: str, eta: str):
                set_manual_export("running", f"Baixando áudio do YouTube... {pct:.1f}%",
                                  progress=int(10 + (pct * 0.45)), speed=spd, eta=eta, stage="downloading", video_id=video_id)

            def on_enc_a(pct: float, spd: str, eta: str):
                set_manual_export("running", f"Normalizando e gerando MP3... {pct:.1f}%",
                                  progress=int(55 + (pct * 0.43)), speed=spd, eta=eta, stage="encoding", video_id=video_id)

            export = ensure_export_mp3(
                video_id, start_str, end_str, title, custom_cover_b64,
                force=force, on_dl=on_dl_a, on_enc=on_enc_a, report=on_report,
                cancel=is_manual_cancelled, register=reg_manual_proc,
            )
            size_mb = export["size"] // 1024 // 1024
            completed_msg = f"Áudio MP3 {'recortado ' if is_cut else ''}pronto! ({size_mb} MB)"
            ext = "mp3"
        else:
            enc_stage = "encoding" if is_cut else "packaging"
            enc_action = "Recortando e otimizando vídeo MP4" if is_cut else "Otimizando vídeo completo"

            set_manual_export("running", f"Preparando exportação do vídeo {video_id} ({quality}p)...",
                              progress=5, speed="--", eta="--", stage="downloading", video_id=video_id)

            set_manual_export("running", "Preparando capa quadrada do episódio...", 8, stage="artwork", video_id=video_id)
            ensure_artwork(video_id, custom_cover_b64, title)

            def on_dl(pct: float, spd: str, eta: str):
                set_manual_export("running", f"Baixando vídeo do YouTube ({quality}p)... {pct:.1f}%",
                                  progress=int(10 + (pct * 0.45)), speed=spd, eta=eta, stage="downloading", video_id=video_id)

            def on_enc(pct: float, spd: str, eta: str):
                set_manual_export("running", f"{enc_action} (+faststart)... {pct:.1f}%",
                                  progress=int(55 + (pct * 0.43)), speed=spd, eta=eta, stage=enc_stage, video_id=video_id)

            export = ensure_export_mp4(
                video_id, start_str, end_str, quality, title,
                force=force, on_dl=on_dl, on_enc=on_enc, report=on_report,
                cancel=is_manual_cancelled, register=reg_manual_proc,
            )
            size_mb = export["size"] // 1024 // 1024
            completed_msg = f"Vídeo recortado e pronto! ({size_mb} MB)" if is_cut else f"Vídeo completo pronto para download! ({size_mb} MB)"
            ext = "mp4"

        dl_url = f"/exports/{video_id}.{ext}?v={int(time.time())}"
        if title and title.strip():
            dl_url += f"&title={urllib.parse.quote(title.strip())}"
        set_manual_export(
            "completed",
            completed_msg,
            progress=100,
            speed="Concluído",
            eta="00:00",
            stage="complete",
            video_id=video_id,
            download_url=dl_url,
            filename=get_download_filename(f"{video_id}.{ext}", title),
        )
    except Exception as exc:
        if isinstance(exc, InterruptedError) or MANUAL_EXPORT_CANCEL_EVENT.is_set():
            LOG.info("manual_export_worker cancelado pelo usuário.")
            set_manual_export("idle", "Download cancelado pelo usuário.", progress=0, stage="idle", video_id=video_id)
            return
        LOG.exception("Erro ao gerar vídeo manual: %s", exc)
        set_manual_export("error", f"Falha ao gerar vídeo: {exc}", 0, stage="error", error=str(exc), video_id=video_id)


def download_youtube_thumbnail(video_id: str, dest_path: Path) -> Path:
    urls = [
        f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
        f"https://img.youtube.com/vi/{video_id}/sddefault.jpg",
        f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
                if len(data) > 1000:
                    dest_path.write_bytes(data)
                    return dest_path
        except Exception:
            continue
    raise RuntimeError(f"Não foi possível baixar a thumbnail do YouTube para {video_id}")


_BATCH: batch_downloader.BatchManager | None = None
_BATCH_GUARD = threading.Lock()


def get_batch() -> batch_downloader.BatchManager:
    """Gerenciador da fila de downloads em lote (criado sob demanda)."""
    global _BATCH
    with _BATCH_GUARD:
        if _BATCH is None:
            _BATCH = batch_downloader.BatchManager({
                "ensure_mp4": ensure_export_mp4,
                "ensure_mp3": ensure_export_mp3,
                "ensure_artwork": ensure_artwork,
                "download_thumbnail": download_youtube_thumbnail,
                "exports_dir": get_exports_dir,
                "download_filename": get_download_filename,
            })
        return _BATCH


class StudioHandler(BaseHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.end_headers()

    def send_json(self, data: Any, status: int = 200) -> None:
        content = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("", "/"):
            html_file = Path("studio.html")
            if html_file.exists():
                content = html_file.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            self.send_error(404, "studio.html não encontrado.")
            return

        if path.startswith("/exports/"):
            raw_filename = urllib.parse.unquote(Path(path).name)
            exports_dir = Path("exports").resolve()
            file_path = (exports_dir / raw_filename).resolve()
            if not file_path.is_file():
                for candidate in exports_dir.glob("*"):
                    if candidate.name == raw_filename:
                        file_path = candidate
                        break
            if file_path.is_file() and str(file_path).startswith(str(exports_dir)):
                mime = "application/octet-stream"
                if file_path.name.endswith(".mp4"):
                    mime = "video/mp4"
                elif file_path.name.endswith(".mp3"):
                    mime = "audio/mpeg"
                elif file_path.name.endswith((".jpg", ".jpeg")):
                    mime = "image/jpeg"

                stat = file_path.stat()
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(stat.st_size))
                if "download" in parsed.query:
                    query_params = urllib.parse.parse_qs(parsed.query)
                    title_param = query_params.get("title", [None])[0]
                    if not title_param:
                        v_id = file_path.name.split(".")[0].replace("_thumb", "")
                        meta_file = exports_dir / f"{v_id}.meta.json"
                        if meta_file.is_file():
                            try:
                                title_param = json.loads(meta_file.read_text(encoding="utf-8")).get("title")
                            except Exception:
                                pass
                    dl_name = get_download_filename(file_path.name, title_param)
                    ascii_fn = re.sub(r'[^\x20-\x7E]', '_', dl_name)
                    encoded_fn = urllib.parse.quote(dl_name, encoding='utf-8')
                    self.send_header("Content-Disposition", f'attachment; filename="{ascii_fn}"; filename*=UTF-8\'\'{encoded_fn}')
                self.end_headers()
                with open(file_path, "rb") as f:
                    shutil.copyfileobj(f, self.wfile)
                return
            self.send_response(404)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(b'{"error": "Arquivo nao encontrado na pasta exports. Gere o arquivo no Studio antes de baixar."}')
            return

        if path == "/api/manual-export-status":
            self.send_json(get_manual_export_state())
            return

        if path == "/api/check-export":
            query_map = urllib.parse.parse_qs(parsed.query)
            v_id = query_map.get("id", [""])[0]
            if not v_id:
                self.send_json({"error": "id e obrigatorio"}, status=400)
                return

            start_q = query_map.get("start", [None])[0]
            end_q = query_map.get("end", [None])[0]
            quality_q = query_map.get("quality", ["720"])[0]
            req_start_sec, req_end_sec = normalize_cut_bounds(start_q, end_q)

            exports_dir = Path("exports").resolve()
            mp4_file = exports_dir / f"{v_id}.mp4"
            art_file = exports_dir / f"{v_id}.jpg"
            mp3_file = exports_dir / f"{v_id}.mp3"
            meta_file = exports_dir / f"{v_id}.meta.json"

            mp4_exists = mp4_file.is_file()
            mp4_size = mp4_file.stat().st_size if mp4_exists else 0
            mp4_formatted = f"{mp4_size // 1024 // 1024} MB" if mp4_exists else None

            mp4_matches_cut = bool(export_matches(v_id, req_start_sec, req_end_sec, quality_q))
            saved_meta = None
            if meta_file.is_file():
                try:
                    saved_meta = json.loads(meta_file.read_text(encoding="utf-8"))
                except Exception:
                    pass
            raw_cached = any((exports_dir / ".cache").glob(f"raw_{v_id}_*.mp4")) if (exports_dir / ".cache").is_dir() else False

            self.send_json({
                "video_id": v_id,
                "mp4_exists": mp4_exists,
                "mp4_matches_cut": mp4_matches_cut,
                "mp4_size": mp4_size,
                "mp4_formatted": mp4_formatted,
                "art_exists": art_file.is_file(),
                "mp3_exists": mp3_file.is_file(),
                "mp3_matches_cut": bool(mp3_export_matches(v_id, req_start_sec, req_end_sec)),
                "mp3_formatted": f"{mp3_file.stat().st_size // 1024 // 1024} MB" if mp3_file.is_file() else None,
                "raw_cached": raw_cached,
                "saved_meta": saved_meta,
            })
            return

        if path == "/api/spotify-status":
            self.send_json(get_spotify_state())
            return

        if path == "/api/status":
            with TASK_LOCK:
                self.send_json(dict(CURRENT_TASK))
            return

        if path == "/api/channel-videos":
            try:
                videos = fetch_channel_videos(limit=10)
                self.send_json(videos)
            except Exception as exc:
                self.send_json({"error": str(exc)}, status=500)
            return

        if path == "/api/batch/channel":
            q = urllib.parse.parse_qs(parsed.query)
            mgr = get_batch()
            try:
                data = batch_downloader.list_channel(
                    ytdlp_cmd(),
                    q.get("channel", [batch_downloader.DEFAULT_CHANNEL])[0],
                    q.get("tab", ["streams"])[0],
                    int(q.get("limit", ["60"])[0] or 60),
                    mgr.settings, mgr.history,
                )
                self.send_json(data)
            except Exception as exc:
                self.send_json({"error": str(exc)}, status=500)
            return

        if path == "/api/batch/state":
            self.send_json(get_batch().snapshot())
            return

        if path == "/api/config":
            load_env_file()
            feed_url = os.environ.get("PODCAST_FEED_URL", "")
            gemini_key = os.environ.get("GEMINI_API_KEY", "")
            masked_gemini = f"{gemini_key[:4]}...{gemini_key[-4:]}" if len(gemini_key) > 8 else ("Configurada" if gemini_key else "")
            self.send_json({
                "R2_ACCOUNT_ID": os.environ.get("R2_ACCOUNT_ID", ""),
                "R2_BUCKET_NAME": os.environ.get("R2_BUCKET_NAME", ""),
                "R2_PUBLIC_DOMAIN": os.environ.get("R2_PUBLIC_DOMAIN", ""),
                "PODCAST_EMAIL": os.environ.get("PODCAST_EMAIL", ""),
                "PODCAST_FEED_URL": feed_url,
                "VODCAST_FEED_URL": feed_url.replace("podcast.xml", "vodcast.xml"),
                "PODCAST_IMAGE_URL": os.environ.get("PODCAST_IMAGE_URL", ""),
                "HAS_GEMINI_KEY": bool(gemini_key),
                "GEMINI_API_KEY_MASKED": masked_gemini,
            })
            return

        if path == "/api/episodes":
            episodes = []
            feeds = [("podcast.xml", "audio"), ("vodcast.xml", "video")]
            for f_name, f_type in feeds:
                f_path = Path(f_name)
                if not f_path.exists():
                    continue
                try:
                    root, channel = load_feed(f_path)
                    for item in channel.findall("item"):
                        guid = item.findtext("guid", default="")
                        title = item.findtext("title", default="Sem título")
                        desc = item.findtext("description", default="")
                        pub_date = item.findtext("pubDate", default="")
                        enclosure = item.find("enclosure")
                        media_url = enclosure.get("url", "") if enclosure is not None else ""
                        media_type = enclosure.get("type", "audio/mpeg" if f_type == "audio" else "video/mp4") if enclosure is not None else ""
                        duration = item.findtext("{http://www.itunes.com/dtds/podcast-1.0.dtd}duration", default="0")
                        episodes.append({
                            "guid": guid,
                            "video_id": item_video_id(item),
                            "title": title,
                            "description": desc,
                            "pubDate": pub_date,
                            "media_url": media_url,
                            "media_type": media_type,
                            "format": f_type,
                            "duration": int(duration) if duration.isdigit() else 0,
                        })
                except Exception as exc:
                    LOG.warning("Erro ao ler feed %s: %s", f_name, exc)

            self.send_json(episodes)
            return

        self.send_error(404, "Recurso não encontrado.")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            self.send_json({"error": "Corpo da requisição JSON inválido."}, status=400)
            return

        if path == "/api/preview":
            raw_id = body.get("video_id", "")
            try:
                video_id = parse_video_id(raw_id)
                details = video_details(video_id)
                duration = int(details.get("duration", 0))
                self.send_json({
                    "id": video_id,
                    "title": details.get("title", video_id),
                    "duration": duration,
                    "duration_formatted": f"{duration // 3600:02d}:{(duration % 3600) // 60:02d}:{duration % 60:02d}",
                    "channel": details.get("channel", "Núcleo da Fé"),
                    "thumbnail": f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
                    "qualities": [
                        {"label": "720p HD (Recomendado)", "value": "720", "est_size": "~210 MB"},
                        {"label": "1080p Full HD", "value": "1080", "est_size": "~450 MB"},
                        {"label": "480p SD", "value": "480", "est_size": "~120 MB"},
                        {"label": "360p Econômico", "value": "360", "est_size": "~75 MB"},
                    ],
                })
            except Exception as exc:
                self.send_json({"error": str(exc)}, status=400)
            return

        if path == "/api/test-r2":
            try:
                client = r2_client()
                bucket = required_env("R2_BUCKET_NAME")
                domain = required_env("R2_PUBLIC_DOMAIN")
                # Test put and delete small probe
                probe_key = ".probe-test.txt"
                client.put_object(Bucket=bucket, Key=probe_key, Body=b"ok", ContentType="text/plain")
                client.delete_object(Bucket=bucket, Key=probe_key)
                self.send_json({"ok": True, "message": f"Conexão bem sucedida com o bucket '{bucket}'!"})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
            return

        if path == "/api/preview-cover":
            raw_id = body.get("video_id", "")
            custom_cover_b64 = body.get("custom_cover")
            try:
                video_id = parse_video_id(raw_id)
                custom_bytes = None
                if custom_cover_b64:
                    c_data = custom_cover_b64
                    if "," in c_data:
                        c_data = c_data.split(",", 1)[1]
                    custom_bytes = base64.b64decode(c_data)
                with tempfile.TemporaryDirectory() as td:
                    art_file = Path(td) / "cover_preview.jpg"
                    generate_episode_artwork(video_id, art_file, custom_image_bytes=custom_bytes)
                    b64_str = base64.b64encode(art_file.read_bytes()).decode("ascii")
                    self.send_json({"ok": True, "preview_url": f"data:image/jpeg;base64,{b64_str}"})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
            return

        if path == "/api/clean-exports":
            exports_dir = Path("exports").resolve()
            freed_bytes = 0
            count = 0
            if exports_dir.exists():
                for f in list(exports_dir.rglob("*")):
                    if f.is_file():
                        freed_bytes += f.stat().st_size
                        try:
                            f.unlink()
                            count += 1
                        except Exception:
                            pass
            freed_mb = freed_bytes // 1024 // 1024
            self.send_json({"ok": True, "freed_mb": freed_mb, "message": f"{count} arquivos removidos. {freed_mb} MB de disco liberados!"})
            return

        if path == "/api/spotify-confirm-login":
            confirm_login()
            self.send_json({"ok": True, "message": "Confirmação de login enviada ao robô Spotify."})
            return

        if path == "/api/spotify-cancel":
            cancel_spotify_upload()
            self.send_json({"ok": True, "message": "Automação do Spotify cancelada."})
            return

        if path == "/api/cancel-manual-export":
            res = cancel_manual_export()
            self.send_json(res)
            return

        if path == "/api/cancel-publish":
            res = cancel_publish()
            self.send_json(res)
            return

        if path == "/api/suggest-cut":
            load_env_file()
            raw_id = body.get("video_id", "")
            try:
                video_id = parse_video_id(raw_id)
            except ValueError as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
                return

            api_key = body.get("gemini_api_key")
            try:
                from cut_detector import suggest_sermon_cut
                result = suggest_sermon_cut(video_id, api_key)
                self.send_json({"ok": True, **result})
            except Exception as exc:
                LOG.exception("Erro ao sugerir corte com IA: %s", exc)
                self.send_json({"ok": False, "error": str(exc)}, status=400)
            return

        if path == "/api/save-gemini-key":
            api_key = body.get("gemini_api_key", "").strip()
            if not api_key:
                self.send_json({"error": "Chave não informada."}, status=400)
                return
            env_path = Path(".env")
            lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
            new_lines = []
            found = False
            for l in lines:
                if l.startswith("GEMINI_API_KEY="):
                    new_lines.append(f"GEMINI_API_KEY={api_key}")
                    found = True
                else:
                    new_lines.append(l)
            if not found:
                new_lines.append(f"GEMINI_API_KEY={api_key}")
            env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            os.environ["GEMINI_API_KEY"] = api_key
            self.send_json({"ok": True, "message": "Chave Gemini salva com sucesso no arquivo .env!"})
            return

        if path == "/api/prepare-export":
            raw_id = body.get("video_id", "")
            try:
                video_id = parse_video_id(raw_id)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return

            quality = body.get("quality", "720")
            start = body.get("start")
            end = body.get("end")
            force = bool(body.get("force", False))
            title = (body.get("title") or "").strip()
            custom_cover_b64 = body.get("custom_cover")

            kind = "mp3" if body.get("kind") == "mp3" else "mp4"
            mp4_file = get_exports_dir() / f"{video_id}.{kind}"
            req_start_sec, req_end_sec = normalize_cut_bounds(start, end)

            # Só considera pronto se o arquivo existe e o corte coincide perfeitamente!
            if not force:
                if kind == "mp3":
                    if custom_cover_b64:
                        try:
                            ensure_artwork(video_id, custom_cover_b64, title)  # capa mudou → MP3 regenera
                        except Exception as exc:
                            LOG.warning("Falha ao atualizar capa personalizada: %s", exc)
                    meta = mp3_export_matches(video_id, req_start_sec, req_end_sec)
                else:
                    meta = export_matches(video_id, req_start_sec, req_end_sec, quality)
                if meta:
                    if not title:
                        title = meta.get("title", "")
                    link_friendly_copy(mp4_file, title)
                    if custom_cover_b64:
                        try:
                            ensure_artwork(video_id, custom_cover_b64, title)
                        except Exception as exc:
                            LOG.warning("Falha ao atualizar capa personalizada: %s", exc)
                    dl_name = get_download_filename(f"{video_id}.{kind}", title)
                    dl_url = f"/exports/{video_id}.{kind}?v={int(time.time())}"
                    if title:
                        dl_url += f"&title={urllib.parse.quote(title)}"
                    self.send_json({
                        "status": "ready",
                        "kind": kind,
                        "video_id": video_id,
                        "download_url": dl_url,
                        "filename": dl_name,
                        "size": mp4_file.stat().st_size,
                        "size_formatted": f"{mp4_file.stat().st_size // 1024 // 1024} MB",
                    })
                    return
                # Corte ou qualidade diferentes: força nova geração!
                force = True

            curr_state = get_manual_export_state()
            if curr_state.get("status") == "running":
                if curr_state.get("video_id") == video_id:
                    self.send_json({"status": "running", "message": "Download já em andamento."})
                else:
                    self.send_json({"error": "Já existe uma exportação de outro vídeo em andamento. Cancele-a primeiro."}, status=409)
                return

            thread = threading.Thread(
                target=manual_export_worker,
                args=(video_id, start, end, quality, force, title, custom_cover_b64, kind),
                daemon=True,
            )
            thread.start()
            self.send_json({"status": "started", "message": "Download e recorte iniciados."})
            return

        if path == "/api/prepare-cover":
            raw_id = body.get("video_id", "")
            title = (body.get("title") or "").strip()
            try:
                video_id = parse_video_id(raw_id)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            try:
                # Respeita a capa personalizada (colada/escolhida) do Studio
                ensure_artwork(video_id, body.get("custom_cover"), title)
            except Exception as exc:
                self.send_json({"error": f"Falha ao gerar capa: {exc}"}, status=500)
                return

            dl_name = get_download_filename(f"{video_id}.jpg", title)
            dl_url = f"/exports/{video_id}.jpg?v={int(time.time())}"
            if title:
                dl_url += f"&title={urllib.parse.quote(title)}"
            self.send_json({
                "status": "ready",
                "download_url": dl_url,
                "filename": dl_name,
            })
            return

        if path == "/api/prepare-thumb":
            raw_id = body.get("video_id", "")
            title = body.get("title", "").strip()
            try:
                video_id = parse_video_id(raw_id)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            exports_dir = Path("exports").resolve()
            exports_dir.mkdir(exist_ok=True)
            thumb_file = exports_dir / f"{video_id}_thumb.jpg"
            if not thumb_file.exists():
                try:
                    download_youtube_thumbnail(video_id, thumb_file)
                except Exception as exc:
                    self.send_json({"error": str(exc)}, status=500)
                    return

            if title and thumb_file.exists():
                friendly_thumb_name = get_download_filename(f"{video_id}_thumb.jpg", title)
                friendly_thumb_path = exports_dir / friendly_thumb_name
                try:
                    if friendly_thumb_path.exists():
                        friendly_thumb_path.unlink()
                    try:
                        os.link(str(thumb_file), str(friendly_thumb_path))
                    except Exception:
                        shutil.copy2(str(thumb_file), str(friendly_thumb_path))
                except Exception:
                    pass

            dl_name = get_download_filename(f"{video_id}_thumb.jpg", title)
            dl_url = f"/exports/{video_id}_thumb.jpg?v={int(time.time())}"
            if title:
                dl_url += f"&title={urllib.parse.quote(title)}"
            self.send_json({
                "status": "ready",
                "download_url": dl_url,
                "filename": dl_name,
            })
            return

        if path.startswith("/api/batch/"):
            mgr = get_batch()
            try:
                if path == "/api/batch/enqueue":
                    if body.get("settings"):
                        mgr.update_settings(body["settings"])
                    self.send_json(mgr.enqueue(body.get("items") or []))
                elif path == "/api/batch/control":
                    mgr.control(str(body.get("action", "")), body.get("job_id"), body.get("extra"))
                    self.send_json(mgr.snapshot())
                elif path == "/api/batch/settings":
                    self.send_json(mgr.update_settings(body))
                elif path == "/api/batch/lookup":
                    urls = [u for u in re.split(r"[\s,]+", str(body.get("text", ""))) if u]
                    self.send_json({"videos": batch_downloader.lookup_urls(ytdlp_cmd(), urls, mgr.settings, mgr.history)})
                elif path == "/api/batch/preview-folder":
                    tpl = str(body.get("template") or mgr.settings["folder_template"])
                    out = []
                    for it in body.get("items") or []:
                        title = str(it.get("title", ""))
                        info = batch_downloader.parse_title(title, it.get("timestamp"))
                        out.append(batch_downloader.render_folder(tpl, info, title, str(it.get("id", ""))))
                    self.send_json({"folders": out})
                elif path == "/api/batch/forget":
                    mgr.forget_history(str(body.get("video_id", "")))
                    self.send_json({"ok": True})
                elif path == "/api/batch/open-folder":
                    target = Path(mgr.settings["output_dir"]) / batch_downloader.safe_relpath(str(body.get("folder", "")))
                    while not target.exists() and target != target.parent:
                        target = target.parent
                    if sys.platform == "darwin":
                        subprocess.Popen(["open", str(target)])
                    elif os.name == "nt":
                        os.startfile(str(target))  # type: ignore[attr-defined]
                    else:
                        subprocess.Popen(["xdg-open", str(target)])
                    self.send_json({"ok": True, "path": str(target)})
                else:
                    self.send_json({"error": "Rota de lote desconhecida."}, status=404)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
            except Exception as exc:
                LOG.exception("Erro na rota %s", path)
                self.send_json({"error": str(exc)}, status=500)
            return

        if path == "/api/spotify-start-upload":
            raw_id = body.get("video_id", "")
            try:
                video_id = parse_video_id(raw_id)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return

            sp_state = get_spotify_state()
            if sp_state.get("status") in ("preparing", "opening_browser", "waiting_login", "uploading", "filling_details"):
                self.send_json({"error": "Já existe uma automação do Spotify em andamento!"}, status=409)
                return

            title = body.get("title")
            description = body.get("description")
            quality = body.get("quality", "720")
            start = body.get("start")
            end = body.get("end")
            custom_cover_b64 = body.get("custom_cover")

            thread = threading.Thread(
                target=spotify_upload_worker,
                args=(video_id, title, description, quality, start, end, custom_cover_b64),
                daemon=True,
            )
            thread.start()
            self.send_json({"ok": True, "message": "Automação do Spotify iniciada."})
            return

        if path == "/api/publish":
            with TASK_LOCK:
                if CURRENT_TASK["status"] == "running":
                    self.send_json({"error": "Já existe uma publicação em andamento!"}, status=409)
                    return
                CURRENT_TASK["logs"] = []
                CURRENT_TASK["error"] = None

            raw_id = body.get("video_id", "")
            try:
                video_id = parse_video_id(raw_id)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, status=400)
                return

            start_str = body.get("start")
            end_str = body.get("end")
            title_override = body.get("title")
            description_override = body.get("description")
            git_push = body.get("git_push", True)
            media_format = body.get("media_format", "audio")  # "audio", "video", "both"
            custom_cover_b64 = body.get("custom_cover")
            quality = body.get("quality", "720")

            thread = threading.Thread(
                target=publish_worker,
                args=(video_id, start_str, end_str, title_override, git_push, media_format, custom_cover_b64, quality, description_override),
                daemon=True,
            )
            thread.start()
            self.send_json({"ok": True, "message": "Publicação iniciada com sucesso."})
            return

        self.send_error(404, "Endpoint não encontrado.")


def main() -> None:
    port = int(os.environ.get("PORT", 8000))
    server = ThreadingHTTPServer(("0.0.0.0", port), StudioHandler)
    server.daemon_threads = True
    url = f"http://localhost:{port}"
    print(f"\n🎙️  Podcast Studio Núcleo da Fé rodando em: {url}\n")
    print("Pressione Ctrl+C para encerrar o servidor.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor encerrado.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    main()

#!/usr/bin/env python3
"""Publica pregações do canal Núcleo da Fé em um feed RSS de podcast.

O modo ``reviewed`` só publica vídeos com início e fim definidos em episodes.json.
O modo ``short`` publica transmissões completas de até 75 minutos.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
CONTENT = "http://purl.org/rss/1.0/modules/content/"
ATOM = "http://www.w3.org/2005/Atom"
ET.register_namespace("itunes", ITUNES)
ET.register_namespace("content", CONTENT)
ET.register_namespace("atom", ATOM)

LOG = logging.getLogger("podcast")
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
DEFAULT_SOURCE = "https://www.youtube.com/@nucleodafelive/streams"
DEFAULT_CHANNEL_ID = "UC7TCRwfLKSwDLErV1Zrq-_A"


def load_env_file(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip("'\""))


load_env_file()


def parse_iso8601_duration(value: str) -> int:
    match = re.fullmatch(r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value)
    if not match:
        return 0
    days, hours, minutes, secs = (int(group or 0) for group in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + secs


def youtube_api_recent_videos(channel_id: str, api_key: str, limit: int = 20) -> list[dict[str, Any]]:
    uploads_id = f"UU{channel_id[2:]}" if channel_id.startswith("UC") else channel_id
    url = (
        f"https://www.googleapis.com/youtube/v3/playlistItems"
        f"?part=contentDetails&playlistId={uploads_id}&maxResults={min(limit, 50)}&key={api_key}"
    )
    request = urllib.request.Request(url, headers={"User-Agent": "podcast-feed/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        playlist_data = json.loads(response.read().decode("utf-8"))
    items = playlist_data.get("items", [])
    video_ids = [item["contentDetails"]["videoId"] for item in items if "contentDetails" in item]
    if not video_ids:
        return []

    ids_param = ",".join(video_ids)
    videos_url = (
        f"https://www.googleapis.com/youtube/v3/videos"
        f"?part=snippet,contentDetails&id={ids_param}&key={api_key}"
    )
    req_videos = urllib.request.Request(videos_url, headers={"User-Agent": "podcast-feed/1.0"})
    with urllib.request.urlopen(req_videos, timeout=20) as response:
        videos_data = json.loads(response.read().decode("utf-8"))

    results: list[dict[str, Any]] = []
    for item in videos_data.get("items", []):
        snippet = item.get("snippet", {})
        content = item.get("contentDetails", {})
        live_status = snippet.get("liveBroadcastContent", "none")
        if live_status in ("live", "upcoming"):
            continue
        duration_sec = parse_iso8601_duration(content.get("duration", ""))
        if duration_sec <= 0:
            continue
        results.append({
            "id": item["id"],
            "title": snippet.get("title", item["id"]),
            "duration": duration_sec,
            "channel_id": snippet.get("channelId", channel_id),
            "upload_date": snippet.get("publishedAt", "").replace("-", "")[:8],
            "description": snippet.get("description", ""),
        })
    return results


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Configure {name} antes de publicar.")
    return value


def https_url(value: str, name: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError(f"{name} deve ser uma URL HTTPS sem parâmetros: {value!r}")
    return value.rstrip("/")


def run(command: list[str]) -> str:
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout)[-1500:].strip()
        raise RuntimeError(f"Falha em {command[0]}: {detail}")
    return result.stdout


def ytdlp_cmd() -> str:
    """Retorna o binário yt-dlp do ambiente virtual atual (mais recente), ou fallback para PATH."""
    venv_bin = Path(sys.executable).parent / "yt-dlp"
    if venv_bin.exists():
        return str(venv_bin)
    return "yt-dlp"


def video_details(video_id: str) -> dict[str, Any]:
    if not VIDEO_ID.fullmatch(video_id):
        raise ValueError(f"ID de vídeo inválido: {video_id!r}")
    url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = [ytdlp_cmd(), "-j", "--no-warnings"]
    if Path("cookies.txt").exists():
        cmd += ["--cookies", "cookies.txt"]
    cmd.append(url)
    data = json.loads(run(cmd))
    expected = os.environ.get("YOUTUBE_CHANNEL_ID", DEFAULT_CHANNEL_ID)
    if data.get("channel_id") != expected:
        raise ValueError(f"O vídeo {video_id} não pertence ao canal esperado {expected}.")
    if data.get("live_status") not in (None, "not_live", "was_live"):
        raise ValueError(f"O vídeo {video_id} ainda não terminou: {data.get('live_status')}.")
    if not data.get("duration"):
        raise ValueError(f"O vídeo {video_id} ainda não tem duração disponível.")
    return data


def recent_video_ids(source_url: str, limit: int) -> list[str]:
    cmd = [
        ytdlp_cmd(), "--dump-single-json", "--flat-playlist", "--playlist-end", str(limit),
        "--no-warnings",
        "--extractor-args", "youtube:player_client=android,ios,web",
    ]
    if Path("cookies.txt").exists():
        cmd += ["--cookies", "cookies.txt"]
    cmd.append(source_url)
    data = json.loads(run(cmd))
    entries = data.get("entries") or [data]
    ids: list[str] = []
    for entry in entries:
        video_id = (entry or {}).get("id", "")
        if VIDEO_ID.fullmatch(video_id):
            ids.append(video_id)
    return ids


def seconds(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", value):
        parts = [int(part) for part in value.split(":")]
        if len(parts) == 2:
            minutes, sec = parts
            if sec < 60:
                return minutes * 60 + sec
        else:
            hours, minutes, sec = parts
            if minutes < 60 and sec < 60:
                return hours * 3600 + minutes * 60 + sec
    raise ValueError(f"Horário inválido: {value!r}; use segundos ou HH:MM:SS.")


def reviewed_specs(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise ValueError(f"Arquivo de cortes não encontrado: {path}")
    specs = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(specs, list):
        raise ValueError("episodes.json deve conter uma lista JSON.")
    seen: set[str] = set()
    for spec in specs:
        if not isinstance(spec, dict) or not VIDEO_ID.fullmatch(str(spec.get("video_id", ""))):
            raise ValueError(f"Entrada inválida em {path}: {spec!r}")
        if spec["video_id"] in seen:
            raise ValueError(f"Vídeo repetido em {path}: {spec['video_id']}")
        seen.add(spec["video_id"])
        start, end = seconds(spec.get("start")), seconds(spec.get("end"))
        if end <= start or end - start < 900:
            raise ValueError(f"O corte de {spec['video_id']} deve durar ao menos 15 minutos.")
        spec["start_seconds"], spec["end_seconds"] = start, end
    return specs


def clean_text(value: str) -> str:
    return "".join(ch for ch in value if ch in "\t\n\r" or ord(ch) >= 32)


def set_text(parent: ET.Element, name: str, value: str) -> ET.Element:
    element = parent.find(name)
    if element is None:
        element = ET.SubElement(parent, name)
    element.text = clean_text(value)
    return element


def load_feed(path: Path) -> tuple[ET.Element, ET.Element]:
    if path.exists():
        root = ET.parse(path).getroot()  # Falha se o feed existente estiver inválido.
        if root.tag != "rss" or root.find("channel") is None:
            raise ValueError(f"Feed existente inválido: {path}")
        channel = root.find("channel")
        assert channel is not None
        return root, channel
    root = ET.Element("rss", {"version": "2.0"})
    return root, ET.SubElement(root, "channel")


def update_channel(channel: ET.Element, feed_url: str, image_url: str, email: str, title_suffix: str = "") -> None:
    base_title = os.environ.get("PODCAST_TITLE", "Núcleo da Fé")
    title = f"{base_title}{title_suffix}" if title_suffix else base_title
    set_text(channel, "title", title)
    set_text(channel, "link", os.environ.get("PODCAST_SITE_URL", "https://www.youtube.com/@nucleodafelive"))
    base_desc = os.environ.get(
        "PODCAST_DESCRIPTION", "Pregações e mensagens do Núcleo da Fé para acompanhar onde você estiver."
    )
    desc = f"{base_desc} (Edição em vídeo para Apple Podcasts e reprodutores compatíveis)" if title_suffix else base_desc
    set_text(channel, "description", desc)
    set_text(channel, "language", "pt-BR")
    self_link = channel.find(f"{{{ATOM}}}link")
    if self_link is None:
        self_link = ET.SubElement(channel, f"{{{ATOM}}}link")
    self_link.attrib.update({"href": feed_url, "rel": "self", "type": "application/rss+xml"})
    set_text(channel, f"{{{ITUNES}}}author", os.environ.get("PODCAST_AUTHOR", "Núcleo da Fé"))
    set_text(channel, f"{{{ITUNES}}}explicit", "false")
    owner = channel.find(f"{{{ITUNES}}}owner")
    if owner is None:
        owner = ET.SubElement(channel, f"{{{ITUNES}}}owner")
    set_text(owner, f"{{{ITUNES}}}name", os.environ.get("PODCAST_AUTHOR", "Núcleo da Fé"))
    set_text(owner, f"{{{ITUNES}}}email", email)
    image = channel.find(f"{{{ITUNES}}}image")
    if image is None:
        image = ET.SubElement(channel, f"{{{ITUNES}}}image")
    image.set("href", image_url)
    category = channel.find(f"{{{ITUNES}}}category")
    if category is None:
        category = ET.SubElement(channel, f"{{{ITUNES}}}category")
    category.set("text", "Religion & Spirituality")
    subcategory = category.find(f"{{{ITUNES}}}category")
    if subcategory is None:
        subcategory = ET.SubElement(category, f"{{{ITUNES}}}category")
    subcategory.set("text", "Christianity")
    # O namespace content é emitido quando o primeiro item é acrescentado.


def existing_guids(channel: ET.Element) -> set[str]:
    return {item.findtext("guid", default="") for item in channel.findall("item")}


def download_audio(video_id: str, directory: Path) -> Path:
    url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = [
        ytdlp_cmd(), "--no-playlist", "--no-warnings", "--no-progress", "-f", "ba/b",
        "-o", str(directory / "source.%(ext)s"),
    ]
    if Path("cookies.txt").exists():
        cmd += ["--cookies", "cookies.txt"]
    cmd.append(url)
    run(cmd)
    files = [path for path in directory.glob("source.*") if path.is_file() and path.stat().st_size]
    if len(files) != 1:
        raise RuntimeError(f"Download de {video_id} não gerou um único arquivo de áudio.")
    return files[0]


def download_video(video_id: str, directory: Path) -> Path:
    url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = [
        ytdlp_cmd(), "--no-playlist", "--no-warnings", "--no-progress",
        "-f", "bv*[height<=720][ext=mp4]+ba[ext=m4a]/bv*[height<=720]+ba/b[height<=720]/b",
        "--merge-output-format", "mp4",
        "-o", str(directory / "source_video.%(ext)s"),
    ]
    if Path("cookies.txt").exists():
        cmd += ["--cookies", "cookies.txt"]
    cmd.append(url)
    run(cmd)
    files = [path for path in directory.glob("source_video.*") if path.is_file() and path.stat().st_size]
    if not files:
        raise RuntimeError(f"Download do vídeo de {video_id} não gerou arquivo.")
    return files[0]


def encode_mp4(source: Path, output: Path, start: int | None = None, end: int | None = None) -> tuple[int, int]:
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if start is not None and end is not None:
        command += ["-ss", str(start), "-to", str(end)]
    command += [
        "-i", str(source),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "44100",
        "-movflags", "+faststart",
        str(output),
    ]
    run(command)
    size = output.stat().st_size
    if size == 0:
        raise RuntimeError("FFmpeg gerou um vídeo MP4 vazio.")
    probe = json.loads(run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(output),
    ]))
    duration = round(float(probe["format"]["duration"]))
    return size, duration


def generate_episode_artwork(video_id: str, output_path: Path, custom_image_bytes: bytes | None = None) -> Path:
    from PIL import Image, ImageFilter
    if custom_image_bytes:
        orig = Image.open(io.BytesIO(custom_image_bytes)).convert("RGB")
    else:
        thumb_url = f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
        req = urllib.request.Request(thumb_url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                thumb_data = resp.read()
        except Exception:
            thumb_url = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
            with urllib.request.urlopen(urllib.request.Request(thumb_url, headers={"User-Agent": "Mozilla/5.0"}), timeout=10) as resp:
                thumb_data = resp.read()
        orig = Image.open(io.BytesIO(thumb_data)).convert("RGB")

    ow, oh = orig.size
    size = 2048
    cover = Image.new("RGB", (size, size), color=(15, 23, 42))

    # Background blurred fill
    bg_fill = orig.resize((int(size * (ow / oh)), size), Image.Resampling.LANCZOS)
    bg_fill_blurred = bg_fill.filter(ImageFilter.GaussianBlur(radius=40))
    left = (bg_fill_blurred.width - size) // 2
    bg_square = bg_fill_blurred.crop((left, 0, left + size, size))
    cover.paste(bg_square, (0, 0))

    # Foreground centered banner with sharp borders
    fg_w = int(size * 0.94)
    fg_h = int(fg_w * (oh / ow))
    fg = orig.resize((fg_w, fg_h), Image.Resampling.LANCZOS)
    pos_x = (size - fg_w) // 2
    pos_y = (size - fg_h) // 2
    cover.paste(fg, (pos_x, pos_y))

    cover.save(output_path, format="JPEG", quality=90)
    return output_path


def encode_mp3(source: Path, output: Path, start: int | None, end: int | None, artwork_path: Path | None = None) -> tuple[int, int]:
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if start is not None and end is not None:
        command += ["-ss", str(start), "-t", str(end - start)]
    command += ["-i", str(source)]

    if artwork_path and artwork_path.exists():
        command += [
            "-i", str(artwork_path),
            "-map", "0:a", "-map", "1:v",
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ac", "2", "-ar", "44100",
            "-c:a", "libmp3lame", "-b:a", "128k",
            "-c:v", "copy", "-id3v2_version", "3",
            "-metadata:s:v", 'title="Album cover"',
            "-metadata:s:v", 'comment="Cover (front)"',
            "-disposition:v:0", "attached_pic",
            "-shortest",
            str(output),
        ]
    else:
        command += [
            "-vn", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ac", "2", "-ar", "44100",
            "-c:a", "libmp3lame", "-b:a", "128k", str(output),
        ]

    run(command)
    size = output.stat().st_size
    if size == 0:
        raise RuntimeError("FFmpeg gerou um MP3 vazio.")
    probe = json.loads(run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(output),
    ]))
    duration = round(float(probe["format"]["duration"]))
    return size, duration


def r2_client() -> Any:
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("Instale as dependências de requirements.txt.") from exc
    return boto3.client(
        "s3",
        region_name="auto",
        endpoint_url=f"https://{required_env('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com",
        aws_access_key_id=required_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=required_env("R2_SECRET_ACCESS_KEY"),
    )


def check_public_audio(url: str, expected_size: int) -> None:
    # Apple Podcasts precisa aceitar HEAD e downloads por intervalo de bytes.
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=20) as response:
                if response.status != 200:
                    raise RuntimeError(f"HEAD retornou HTTP {response.status}.")
                length = response.headers.get("Content-Length")
                if length and int(length) != expected_size:
                    raise RuntimeError("O arquivo público tem tamanho diferente do MP3 enviado.")
            request = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
            with urllib.request.urlopen(request, timeout=20) as response:
                if response.status != 206:
                    raise RuntimeError(f"O servidor não aceitou Range: HTTP {response.status}.")
            return
        except (urllib.error.URLError, RuntimeError) as exc:
            if attempt == 2:
                raise RuntimeError(f"Áudio não está disponível para podcasts em {url}: {exc}") from exc
            time.sleep(3)


def check_artwork(url: str) -> None:
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Instale as dependências de requirements.txt.") from exc
    headers = {"User-Agent": "podcast-feed/1.0"}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers, method="HEAD"), timeout=20) as response:
        if response.status != 200:
            raise ValueError(f"A capa não aceita HEAD: HTTP {response.status}.")
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
        data = response.read(10 * 1024 * 1024 + 1)
    if len(data) > 10 * 1024 * 1024:
        raise ValueError("A capa supera 10 MB.")
    with Image.open(io.BytesIO(data)) as image:
        if image.format not in ("JPEG", "PNG") or image.mode != "RGB":
            raise ValueError("A capa deve ser JPG ou PNG em RGB, sem transparência.")
        width, height = image.size
        if width != height or not 1400 <= width <= 3000:
            raise ValueError("A capa deve ser quadrada, entre 1400 e 3000 pixels.")


def episode_date(details: dict[str, Any]) -> str:
    timestamp = details.get("release_timestamp") or details.get("timestamp")
    if timestamp:
        date = datetime.fromtimestamp(timestamp, timezone.utc)
    elif details.get("upload_date"):
        date = datetime.strptime(details["upload_date"], "%Y%m%d").replace(tzinfo=timezone.utc)
    else:
        date = datetime.now(timezone.utc)
    return format_datetime(date)


def add_episode(channel: ET.Element, details: dict[str, Any], spec: dict[str, Any], media_url: str,
                size: int, duration: int, media_type: str = "audio/mpeg", image_url: str | None = None) -> None:
    video_id = details["id"]
    url = f"https://www.youtube.com/watch?v={video_id}"
    title = spec.get("title") or re.sub(r"^Culto ao vivo\s*[-–:]\s*", "", details.get("title", ""), flags=re.I)
    description = spec.get("description") or f"Pregação do Núcleo da Fé. Vídeo original: {url}"
    item = ET.Element("item")
    set_text(item, "title", str(title))
    set_text(item, "description", str(description))
    set_text(item, f"{{{CONTENT}}}encoded", str(description))
    set_text(item, "link", url)
    guid = set_text(item, "guid", f"yt:{video_id}")
    guid.set("isPermaLink", "false")
    set_text(item, "pubDate", episode_date(details))
    ET.SubElement(item, "enclosure", {"url": media_url, "length": str(size), "type": media_type})
    set_text(item, f"{{{ITUNES}}}duration", str(duration))
    set_text(item, f"{{{ITUNES}}}explicit", "false")
    if image_url:
        ep_img = ET.SubElement(item, f"{{{ITUNES}}}image")
        ep_img.set("href", image_url)
    first = channel.find("item")
    channel.insert(list(channel).index(first), item) if first is not None else channel.append(item)


def save_feed(root: ET.Element, path: Path) -> None:
    ET.indent(root, space="  ")
    xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    ET.fromstring(xml)  # Não substitui o feed se a serialização ficou inválida.
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".podcast-", suffix=".xml", delete=False) as tmp:
        tmp.write(xml)
        tmp.flush()
        os.fsync(tmp.fileno())
        temp_path = Path(tmp.name)
    os.replace(temp_path, path)


def select_specs(mode: str, limit: int, plan_path: Path) -> list[dict[str, Any]]:
    if mode == "reviewed":
        return reviewed_specs(plan_path)
    source = os.environ.get("YOUTUBE_SOURCE_URL", DEFAULT_SOURCE)
    title_pattern = re.compile(os.environ.get("TITLE_PATTERN", r"(?i)^culto ao vivo\b"))
    specs: list[dict[str, Any]] = []
    for video_id in reversed(recent_video_ids(source, limit)):
        try:
            details = video_details(video_id)
        except ValueError as exc:
            LOG.info("Ignorando %s: %s", video_id, exc)
            continue
        duration = int(details["duration"])
        maximum = 75 * 60 if mode == "short" else 3 * 3600
        if not title_pattern.search(details.get("title", "")) or not 900 <= duration <= maximum:
            LOG.info("Ignorando %s: título ou duração fora dos critérios.", video_id)
            continue
        specs.append({"video_id": video_id, "details": details})
    return specs


def main() -> int:
    parser = argparse.ArgumentParser(description="Publica pregações no feed RSS do Núcleo da Fé")
    parser.add_argument("--mode", choices=("reviewed", "short", "full"),
                        default=os.environ.get("PODCAST_MODE", "reviewed"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-episodes", type=int, default=1)
    parser.add_argument("--candidate-limit", type=int, default=20)
    parser.add_argument("--plan", type=Path, default=Path("episodes.json"))
    parser.add_argument("--feed", type=Path, default=Path("podcast.xml"))
    parser.add_argument("--video-id", help="Publica um vídeo marcado como pronto em video_state.json")
    parser.add_argument("--start", help="Início do corte (HH:MM:SS ou segundos)")
    parser.add_argument("--end", help="Fim do corte (HH:MM:SS ou segundos)")
    parser.add_argument("--force", action="store_true", help="Publica sem corte mesmo se tiver mais de 75 min")
    parser.add_argument("--format", choices=("audio", "video", "both"), default="audio",
                        help="Formato de mídia a gerar (audio, video ou both)")
    parser.add_argument("--skip-artwork-check", action="store_true", help="Ignora verificação estrita da capa")
    parser.add_argument("--state", type=Path, default=Path("video_state.json"))
    args = parser.parse_args()
    if args.max_episodes < 1 or args.candidate_limit < 1:
        parser.error("Os limites devem ser positivos.")
    if not shutil.which("yt-dlp") or not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("yt-dlp, ffmpeg e ffprobe são necessários.")

    root, channel = load_feed(args.feed)
    if args.video_id:
        if not VIDEO_ID.fullmatch(args.video_id):
            parser.error("--video-id deve ser um ID de YouTube válido.")
        has_cut = bool(args.start or args.end)
        if has_cut:
            if not (args.start and args.end):
                parser.error("Para recortar um vídeo, forneça ambos --start e --end.")
            start_sec = seconds(args.start)
            end_sec = seconds(args.end)
            if end_sec <= start_sec or end_sec - start_sec < 900:
                raise ValueError("O corte deve durar ao menos 15 minutos.")
            specs = [{
                "video_id": args.video_id,
                "start_seconds": start_sec,
                "end_seconds": end_sec,
            }]
        else:
            specs = [{"video_id": args.video_id}]
    else:
        specs = select_specs(args.mode, args.candidate_limit, args.plan)
    published = existing_guids(channel)
    selected: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for spec in specs:
        video_id = spec["video_id"]
        if f"yt:{video_id}" in published:
            continue
        try:
            details = spec.get("details") or video_details(video_id)
        except ValueError as exc:
            LOG.info("Ignorando %s: %s", video_id, exc)
            continue
        duration_sec = int(details.get("duration", 0))
        if not spec.get("start_seconds") and duration_sec > 75 * 60 and not args.force:
            raise ValueError(
                f"O vídeo {video_id} tem {duration_sec // 60} minutos (> 75 min). "
                "Para recortar a pregação, informe --start e --end (ex: --start 00:35:00 --end 01:20:00) "
                "ou adicione --force para publicar na íntegra."
            )
        if spec.get("end_seconds"):
            if spec["end_seconds"] > duration_sec + 3:
                raise ValueError(f"O fim do corte de {video_id} supera a duração do vídeo.")
        elif spec.get("expected_duration") and duration_sec != spec["expected_duration"]:
            raise ValueError(f"A duração de {video_id} mudou após a última varredura. Execute a varredura novamente.")
        selected.append((spec, details))
        if len(selected) >= args.max_episodes:
            break
    if not selected:
        LOG.info("Nenhum episódio novo elegível.")
        return 0
    for spec, details in selected:
        LOG.info("Selecionado: %s (%s)", details.get("title"), details["id"])
    if args.dry_run:
        return 0

    public_domain = https_url(required_env("R2_PUBLIC_DOMAIN"), "R2_PUBLIC_DOMAIN")
    image_url = https_url(required_env("PODCAST_IMAGE_URL"), "PODCAST_IMAGE_URL")
    email = required_env("PODCAST_EMAIL")
    feed_url = https_url(required_env("PODCAST_FEED_URL"), "PODCAST_FEED_URL")
    bucket = required_env("R2_BUCKET_NAME")
    try:
        check_artwork(image_url)
    except Exception as exc:
        if args.skip_artwork_check:
            LOG.warning("Aviso sobre a capa ignorado (--skip-artwork-check): %s", exc)
        else:
            raise
    client = r2_client()
    for spec, details in selected:
        video_id = details["id"]
        with tempfile.TemporaryDirectory(prefix="podcast-") as temp_dir:
            directory = Path(temp_dir)
            start_sec = spec.get("start_seconds")
            end_sec = spec.get("end_seconds")

            if args.format in ("video", "both"):
                source_video = download_video(video_id, directory)
                mp4 = directory / f"{video_id}.mp4"
                v_size, v_duration = encode_mp4(source_video, mp4, start_sec, end_sec)
                v_key = f"episodes/{video_id}.mp4"
                client.upload_file(str(mp4), bucket, v_key, ExtraArgs={"ContentType": "video/mp4"})
                v_url = f"{public_domain}/{v_key}"

                vodcast_path = Path("vodcast.xml")
                vroot, vchannel = load_feed(vodcast_path)
                v_feed_url = feed_url.replace("podcast.xml", "vodcast.xml")
                update_channel(vchannel, v_feed_url, image_url, email, title_suffix=" (Vídeo)")
                add_episode(vchannel, details, spec, v_url, v_size, v_duration, media_type="video/mp4")
                save_feed(vroot, vodcast_path)
                LOG.info("Vídeo publicado: %s", v_url)

                if args.format == "both":
                    mp3 = directory / f"{video_id}.mp3"
                    size, duration = encode_mp3(source_video, mp3, start_sec, end_sec)
                    key = f"episodes/{video_id}.mp3"
                    client.upload_file(str(mp3), bucket, key, ExtraArgs={"ContentType": "audio/mpeg"})
                    audio_url = f"{public_domain}/{key}"
                    update_channel(channel, feed_url, image_url, email)
                    add_episode(channel, details, spec, audio_url, size, duration, media_type="audio/mpeg")
                    save_feed(root, args.feed)
                    LOG.info("Áudio publicado: %s", audio_url)

            if args.format == "audio":
                source = download_audio(video_id, directory)
                mp3 = directory / f"{video_id}.mp3"
                size, duration = encode_mp3(source, mp3, start_sec, end_sec)
                key = f"episodes/{video_id}.mp3"
                client.upload_file(str(mp3), bucket, key, ExtraArgs={"ContentType": "audio/mpeg"})
                audio_url = f"{public_domain}/{key}"
                update_channel(channel, feed_url, image_url, email)
                add_episode(channel, details, spec, audio_url, size, duration, media_type="audio/mpeg")
                save_feed(root, args.feed)
                LOG.info("Áudio publicado: %s", audio_url)

    LOG.info("Processamento concluído com sucesso.")
    if args.state.exists() and args.video_id:
        try:
            state = json.loads(args.state.read_text(encoding="utf-8"))
            if "videos" in state and args.video_id in state["videos"]:
                state["videos"][args.video_id]["status"] = "published"
                args.state.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", stream=sys.stdout)
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, ET.ParseError, OSError) as error:
        LOG.error("Publicação interrompida: %s", error)
        raise SystemExit(1) from error

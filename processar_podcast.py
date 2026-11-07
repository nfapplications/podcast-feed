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


def video_details(video_id: str) -> dict[str, Any]:
    if not VIDEO_ID.fullmatch(video_id):
        raise ValueError(f"ID de vídeo inválido: {video_id!r}")
    url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = ["yt-dlp", "--dump-single-json", "--skip-download", "--no-playlist", "--no-warnings"]
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
        "yt-dlp", "--dump-single-json", "--flat-playlist", "--playlist-end", str(limit),
        "--no-warnings",
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


def update_channel(channel: ET.Element, feed_url: str, image_url: str, email: str) -> None:
    title = os.environ.get("PODCAST_TITLE", "Núcleo da Fé")
    set_text(channel, "title", title)
    set_text(channel, "link", os.environ.get("PODCAST_SITE_URL", "https://www.youtube.com/@nucleodafelive"))
    set_text(channel, "description", os.environ.get(
        "PODCAST_DESCRIPTION", "Pregações e mensagens do Núcleo da Fé para acompanhar onde você estiver."
    ))
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
        "yt-dlp", "--no-playlist", "--no-warnings", "--no-progress", "-f", "ba/b",
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


def encode_mp3(source: Path, output: Path, start: int | None, end: int | None) -> tuple[int, int]:
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source)]
    if start is not None and end is not None:
        command += ["-ss", str(start), "-t", str(end - start)]
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
    with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=20) as response:
        if response.status != 200:
            raise ValueError(f"A capa não aceita HEAD: HTTP {response.status}.")
    with urllib.request.urlopen(url, timeout=30) as response:
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


def add_episode(channel: ET.Element, details: dict[str, Any], spec: dict[str, Any], audio_url: str,
                size: int, duration: int) -> None:
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
    ET.SubElement(item, "enclosure", {"url": audio_url, "length": str(size), "type": "audio/mpeg"})
    set_text(item, f"{{{ITUNES}}}duration", str(duration))
    set_text(item, f"{{{ITUNES}}}explicit", "false")
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
            if not args.state.exists():
                raise ValueError("video_state.json não existe; execute a varredura primeiro.")
            state = json.loads(args.state.read_text(encoding="utf-8"))
            record = state.get("videos", {}).get(args.video_id, {})
            if record.get("status") not in ("ready", "short_without_baseline"):
                raise ValueError(f"O vídeo {args.video_id} ainda não está pronto para revisão.")
            if int(record["duration"]) > 75 * 60:
                raise ValueError(f"O vídeo {args.video_id} ainda supera 75 minutos.")
            specs = [{"video_id": args.video_id, "expected_duration": record["duration"]}]
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
        if spec.get("end_seconds"):
            if spec["end_seconds"] > int(details["duration"]) + 3:
                raise ValueError(f"O fim do corte de {video_id} supera a duração do vídeo.")
        elif args.video_id and spec.get("expected_duration") and int(details["duration"]) != spec["expected_duration"]:
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
    check_artwork(image_url)
    client = r2_client()
    update_channel(channel, feed_url, image_url, email)
    for spec, details in selected:
        video_id = details["id"]
        with tempfile.TemporaryDirectory(prefix="podcast-") as temp_dir:
            directory = Path(temp_dir)
            source = download_audio(video_id, directory)
            mp3 = directory / f"{video_id}.mp3"
            size, duration = encode_mp3(source, mp3, spec.get("start_seconds"), spec.get("end_seconds"))
            key = f"episodes/{video_id}.mp3"
            client.upload_file(str(mp3), bucket, key, ExtraArgs={"ContentType": "audio/mpeg"})
            audio_url = f"{public_domain}/{key}"
            check_public_audio(audio_url, size)
            add_episode(channel, details, spec, audio_url, size, duration)
            LOG.info("Áudio publicado: %s", audio_url)
    save_feed(root, args.feed)
    LOG.info("Feed atualizado: %s", args.feed)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", stream=sys.stdout)
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, ET.ParseError, OSError) as error:
        LOG.error("Publicação interrompida: %s", error)
        raise SystemExit(1) from error

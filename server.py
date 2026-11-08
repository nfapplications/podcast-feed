#!/usr/bin/env python3
"""Servidor Web do Podcast Studio Núcleo da Fé."""

from __future__ import annotations

import base64
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
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from processar_podcast import (
    add_episode,
    clean_text,
    download_audio,
    download_video,
    encode_mp3,
    encode_mp4,
    existing_guids,
    generate_episode_artwork,
    https_url,
    load_env_file,
    load_feed,
    r2_client,
    required_env,
    save_feed,
    seconds,
    update_channel,
    video_details,
)

LOG = logging.getLogger("studio")
load_env_file()

CURRENT_TASK: dict[str, Any] = {
    "status": "idle",  # idle, running, success, error
    "message": "Nenhuma publicação em andamento.",
    "progress": 0,
    "logs": [],
    "error": None,
}
TASK_LOCK = threading.Lock()


def set_task(status: str, message: str, progress: int = 0, log_line: str | None = None, error: str | None = None) -> None:
    with TASK_LOCK:
        CURRENT_TASK["status"] = status
        CURRENT_TASK["message"] = message
        CURRENT_TASK["progress"] = progress
        if log_line:
            CURRENT_TASK["logs"].append(f"[{time.strftime('%H:%M:%S')}] {log_line}")
        if error:
            CURRENT_TASK["error"] = error


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
        "yt-dlp",
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

    # Get published GUIDs
    feed_path = Path("podcast.xml")
    published_guids = set()
    if feed_path.exists():
        try:
            _, ch = load_feed(feed_path)
            published_guids = existing_guids(ch)
        except Exception:
            pass

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
            "is_live": dur_sec == 0,
        })
    return results


def publish_worker(
    video_id: str,
    start_str: str | None,
    end_str: str | None,
    title_override: str | None,
    git_push: bool,
    media_format: str = "audio",  # "audio", "video", "both"
    custom_cover_b64: str | None = None,
) -> None:
    try:
        set_task("running", f"Iniciando publicação ({media_format}) do vídeo {video_id}...", 5, "Buscando detalhes do vídeo no YouTube...")
        details = video_details(video_id)
        video_title = title_override.strip() if title_override and title_override.strip() else details.get("title", video_id)
        set_task("running", f"Vídeo identificado: {video_title}", 15, f"Título: {video_title} (Duração original: {int(details.get('duration', 0)) // 60} min)")

        start_sec = seconds(start_str) if start_str and start_str.strip() else None
        end_sec = seconds(end_str) if end_str and end_str.strip() else None
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

            published_info: list[str] = []

            # 1. Generate square episode artwork (2048x2048)
            set_task("running", "Gerando capa quadrada do episódio (2048x2048)...", 20, "Processando arte oficial do episódio para Spotify e Apple Podcasts...")
            custom_bytes = None
            if custom_cover_b64:
                try:
                    c_data = custom_cover_b64
                    if "," in c_data:
                        c_data = c_data.split(",", 1)[1]
                    custom_bytes = base64.b64decode(c_data)
                except Exception as e:
                    LOG.warning("Erro ao decodificar capa personalizada: %s", e)

            art_file = directory / f"{video_id}.jpg"
            generate_episode_artwork(video_id, art_file, custom_image_bytes=custom_bytes)

            art_key = f"episodes/{video_id}.jpg"
            client.upload_file(str(art_file), bucket, art_key, ExtraArgs={"ContentType": "image/jpeg"})
            ep_image_url = f"{public_domain}/{art_key}"
            published_info.append(f"Capa: {ep_image_url}")

            if media_format in ("video", "both"):
                set_task("running", "Baixando vídeo do YouTube...", 30, f"Baixando stream de vídeo de {video_id}...")
                video_src = download_video(video_id, directory)

                set_task("running", "Processando vídeo MP4 com FFmpeg...", 50, "Cortando e otimizando vídeo MP4 (+faststart)...")
                mp4 = directory / f"{video_id}.mp4"
                v_size, v_dur = encode_mp4(video_src, mp4, start_sec, end_sec)

                set_task("running", f"Enviando vídeo ({v_size // 1024 // 1024} MB) para o Cloudflare R2...", 70, "Upload do vídeo MP4 para o Cloudflare R2...")
                v_key = f"episodes/{video_id}.mp4"
                client.upload_file(str(mp4), bucket, v_key, ExtraArgs={"ContentType": "video/mp4"})
                v_url = f"{public_domain}/{v_key}"
                published_info.append(f"Vídeo MP4: {v_url}")

                vodcast_path = Path("vodcast.xml")
                v_root, v_channel = load_feed(vodcast_path)
                v_feed_url = feed_url_base.replace("podcast.xml", "vodcast.xml")
                update_channel(v_channel, v_feed_url, image_url, email, title_suffix=" (Vídeo)")
                add_episode(v_channel, details, spec, v_url, v_size, v_dur, media_type="video/mp4", image_url=ep_image_url)
                save_feed(v_root, vodcast_path)

                if media_format == "both":
                    set_task("running", "Normalizando áudio para -16 LUFS...", 80, "Extraindo áudio do vídeo para MP3 com capa embutida...")
                    mp3 = directory / f"{video_id}.mp3"
                    a_size, a_dur = encode_mp3(video_src, mp3, start_sec, end_sec, artwork_path=art_file)

                    set_task("running", f"Enviando áudio ({a_size // 1024 // 1024} MB) para o R2...", 90, "Upload do áudio MP3 para o Cloudflare R2...")
                    a_key = f"episodes/{video_id}.mp3"
                    client.upload_file(str(mp3), bucket, a_key, ExtraArgs={"ContentType": "audio/mpeg"})
                    a_url = f"{public_domain}/{a_key}"
                    published_info.append(f"Áudio MP3: {a_url}")

                    feed_path = Path("podcast.xml")
                    root, channel = load_feed(feed_path)
                    update_channel(channel, feed_url_base, image_url, email)
                    add_episode(channel, details, spec, a_url, a_size, a_dur, media_type="audio/mpeg", image_url=ep_image_url)
                    save_feed(root, feed_path)

            else:
                # Audio only
                set_task("running", "Baixando áudio do YouTube...", 30, f"Executando download do áudio de {video_id}...")
                audio_src = download_audio(video_id, directory)
                set_task("running", "Normalizando áudio para -16 LUFS com capa embutida...", 55, "Processando FFmpeg loudnorm e MP3...")
                mp3 = directory / f"{video_id}.mp3"
                size, duration = encode_mp3(audio_src, mp3, start_sec, end_sec, artwork_path=art_file)

                set_task("running", f"Áudio gerado ({size // 1024 // 1024} MB, {duration // 60} min). Enviando ao R2...", 75, "Fazendo upload para o Cloudflare R2...")
                key = f"episodes/{video_id}.mp3"
                client.upload_file(str(mp3), bucket, key, ExtraArgs={"ContentType": "audio/mpeg"})
                audio_url = f"{public_domain}/{key}"
                published_info.append(f"Áudio MP3: {audio_url}")

                feed_path = Path("podcast.xml")
                root, channel = load_feed(feed_path)
                update_channel(channel, feed_url_base, image_url, email)
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

            if git_push:
                set_task("running", "Enviando alterações para o GitHub Pages...", 95, "Executando git commit e push...")
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
        set_task("success", f"Episódio '{video_title}' publicado com sucesso!", 100, f"Publicação finalizada com sucesso! {summary}")
    except Exception as exc:
        LOG.exception("Erro ao publicar episódio: %s", exc)
        set_task("error", f"Falha na publicação: {exc}", 0, f"ERRO: {exc}", error=str(exc))


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

        if path == "/api/config":
            load_env_file()
            feed_url = os.environ.get("PODCAST_FEED_URL", "")
            self.send_json({
                "R2_ACCOUNT_ID": os.environ.get("R2_ACCOUNT_ID", ""),
                "R2_BUCKET_NAME": os.environ.get("R2_BUCKET_NAME", ""),
                "R2_PUBLIC_DOMAIN": os.environ.get("R2_PUBLIC_DOMAIN", ""),
                "PODCAST_EMAIL": os.environ.get("PODCAST_EMAIL", ""),
                "PODCAST_FEED_URL": feed_url,
                "VODCAST_FEED_URL": feed_url.replace("podcast.xml", "vodcast.xml"),
                "PODCAST_IMAGE_URL": os.environ.get("PODCAST_IMAGE_URL", ""),
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
            git_push = body.get("git_push", True)
            media_format = body.get("media_format", "audio")  # "audio", "video", "both"
            custom_cover_b64 = body.get("custom_cover")

            thread = threading.Thread(
                target=publish_worker,
                args=(video_id, start_str, end_str, title_override, git_push, media_format, custom_cover_b64),
                daemon=True,
            )
            thread.start()
            self.send_json({"ok": True, "message": "Publicação iniciada com sucesso."})
            return

        self.send_error(404, "Endpoint não encontrado.")


def main() -> None:
    port = int(os.environ.get("PORT", 8000))
    server = HTTPServer(("0.0.0.0", port), StudioHandler)
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

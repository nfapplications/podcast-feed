#!/usr/bin/env python3
"""Detecta quando uma transmissão do Núcleo da Fé foi encurtada após o culto."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from processar_podcast import (
    DEFAULT_SOURCE,
    existing_guids,
    load_feed,
    recent_video_ids,
    video_details,
)

LOG = logging.getLogger("podcast-scan")


def classify(previous: dict, duration: int, published: bool, max_podcast_seconds: int,
             min_reduction_seconds: int) -> tuple[int, int, str]:
    longest = max(int(previous.get("longest_duration", 0)), duration)
    stable_checks = min(2, int(previous.get("stable_checks", 0)) + 1) if previous.get("duration") == duration else 1
    if published:
        status = "published"
    elif longest - duration >= min_reduction_seconds and duration <= max_podcast_seconds:
        status = "ready" if stable_checks >= 2 else "confirming"
    elif duration <= max_podcast_seconds:
        status = "short_without_baseline"
    else:
        status = "monitoring"
    return longest, stable_checks, status


def write_summary(records: dict, new_ready: list[str]) -> None:
    lines = ["## Posições das pregações", "", "| Vídeo | Duração atual | Maior duração vista | Situação |",
             "| --- | ---: | ---: | --- |"]
    labels = {
        "ready": "Pronto para revisão",
        "confirming": "Aguardando segunda verificação",
        "short_without_baseline": "Curto, sem duração anterior registrada",
        "monitoring": "Aguardando recorte",
        "published": "Publicado",
    }
    for video_id, record in sorted(records.items(), key=lambda pair: pair[1].get("first_seen", ""), reverse=True):
        title = record.get("title", video_id).replace("|", "\\|").replace("\n", " ")
        link = f"https://www.youtube.com/watch?v={video_id}"
        duration = int(record.get("duration", 0)) // 60
        longest = int(record.get("longest_duration", 0)) // 60
        lines.append(f"| [{title}]({link}) | {duration} min | {longest} min | {labels.get(record['status'], record['status'])} |")
    if new_ready:
        lines += ["", "**Novos vídeos prontos:** " + ", ".join(new_ready)]
    summary = "\n".join(lines) + "\n"
    print(summary)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as file:
            file.write(summary)
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as file:
            file.write(f"new_ready={','.join(new_ready)}\n")
            ready_ids = [
                video_id for video_id, record in records.items()
                if record["status"] in ("ready", "short_without_baseline")
            ]
            file.write(f"ready_ids={','.join(ready_ids)}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path("video_state.json"))
    parser.add_argument("--feed", type=Path, default=Path("podcast.xml"))
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--max-podcast-minutes", type=int, default=75)
    parser.add_argument("--min-reduction-minutes", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if min(args.limit, args.max_podcast_minutes, args.min_reduction_minutes) < 1:
        parser.error("Os limites devem ser positivos.")
    if args.state.exists():
        state = json.loads(args.state.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not isinstance(state.get("videos"), dict):
            raise ValueError("video_state.json inválido.")
    else:
        state = {"videos": {}}
    videos = state["videos"]
    published = existing_guids(load_feed(args.feed)[1])
    new_ready: list[str] = []
    source = os.getenv("YOUTUBE_SOURCE_URL", DEFAULT_SOURCE)
    candidate_ids = recent_video_ids(source, args.limit)
    inspected = 0
    for video_id in candidate_ids:
        time.sleep(2)
        try:
            details = video_details(video_id)
        except (ValueError, RuntimeError) as exc:
            LOG.info("Ignorando %s: %s", video_id, exc)
            continue
        inspected += 1
        duration = int(details["duration"])
        previous = videos.get(video_id, {})
        longest, checks, status = classify(
            previous, duration, f"yt:{video_id}" in published,
            args.max_podcast_minutes * 60, args.min_reduction_minutes * 60,
        )
        if status == "ready" and previous.get("status") != "ready":
            new_ready.append(video_id)
        elif status == "short_without_baseline" and "duration" not in previous:
            new_ready.append(video_id)
        videos[video_id] = {
            "title": details.get("title", video_id),
            "first_seen": previous.get("first_seen", datetime.now(timezone.utc).isoformat()),
            "duration": duration,
            "longest_duration": longest,
            "stable_checks": checks,
            "status": status,
        }
    if candidate_ids and inspected == 0:
        raise RuntimeError("Nenhum vídeo pôde ser inspecionado; verifique o acesso ao YouTube.")
    write_summary(videos, new_ready)
    if not args.dry_run:
        args.state.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", stream=sys.stdout)
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as error:
        LOG.error("Varredura interrompida: %s", error)
        raise SystemExit(1) from error

"""Downloads em lote: listagem de canais do YouTube e fila com downloads paralelos.

Recursos inspirados em gerenciadores de mercado (4K Video Downloader, JDownloader,
yt-dlp GUIs): fila persistente (sobrevive a reinício), paralelismo ajustável em tempo
real, início escalonado com jitter, limite de banda por download, retry com backoff,
pausa global automática quando o YouTube responde 429/"confirme que não é um robô",
histórico para pular o que já foi baixado e pastas por pregador/evento → palavra.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

LOG = logging.getLogger("batch")

STATE_DIR = Path(".")
SETTINGS_FILE = STATE_DIR / "batch_settings.json"
QUEUE_FILE = STATE_DIR / "batch_queue.json"
HISTORY_FILE = STATE_DIR / "batch_history.json"

DEFAULT_CHANNEL = "@nucleodafelive"
SUGGESTED_CONCURRENCY = 2
MAX_CONCURRENCY = 6
MIN_START_GAP = 4.0          # segundos entre o início de dois downloads
RETRY_DELAYS = (20, 90, 240)  # backoff entre tentativas
RATE_LIMIT_COOLDOWN = 180     # pausa global após 429 / bot-check

DEFAULT_SETTINGS: dict[str, Any] = {
    "output_dir": str(Path.home() / "Downloads" / "Núcleo da Fé"),
    "concurrency": SUGGESTED_CONCURRENCY,
    "limit_rate": "",  # ex.: "5M" por download; vazio = sem limite
    "folder_template": "{grupo}/{data} - {palavra}",
    "keep_cache": False,
    "skip_downloaded": True,
}

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
RATE_LIMIT_RE = re.compile(r"429|too many requests|sign in to confirm|not a bot|rate.?limit", re.I)

# ---------------------------------------------------------------------------
# Parser de títulos → pregador / evento / palavra / data
# ---------------------------------------------------------------------------

SPEAKER_RE = re.compile(
    r"^(?P<prefix>pastora|pastor|pra\.?|pr\.?|ap\.?|apóstolo|apostolo|apóstola|bispo|bispa|rev\.?|reverendo|"
    r"missionária|missionaria|missionário|missionario|miss\.?|profeta|profetisa|ev\.?|evangelista|dr\.?|dra\.?)"
    r"\s+(?P<name>.+)$",
    re.I,
)
PREFIX_CANON = {
    "pr": "Pastor", "pr.": "Pastor", "pastor": "Pastor",
    "pra": "Pastora", "pra.": "Pastora", "pastora": "Pastora",
    "ap": "Apóstolo", "ap.": "Apóstolo", "apostolo": "Apóstolo", "apóstolo": "Apóstolo",
    "miss": "Missionária", "miss.": "Missionária",
    "ev": "Evangelista", "ev.": "Evangelista",
}
GENERIC_RE = re.compile(
    r"^(live service|live stream|livestream|culto ao vivo|culto|transmiss[aã]o ao vivo|ao vivo|live|"
    r"sunday|sunday \d+ ?(am|pm)|domingo|s[aá]bado|sexta(-feira)?|quarta(-feira)?|quinta(-feira)?|"
    r"segunda(-feira)?|ter[cç]a(-feira)?|saturday|friday|wednesday|\w+ \d{1,2} ?h(s|rs)?|\d{1,2} ?(h|hs|am|pm))$",
    re.I,
)
EVENT_RE = re.compile(
    r"(youth|jovem|jovens|teen|kids|infantil|congresso|confer[eê]ncia|conference|encontro|encounter|"
    r"semin[aá]rio|retiro|vig[ií]lia|batismo|water service|santa ceia|ceia|celebra[cç][aã]o|"
    r"mulheres|homens|women|men's|casais|louvor|worship|escola|school|summit|festival|workshop)",
    re.I,
)
DATE_RE = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\b")


def _canon_speaker(seg: str) -> str | None:
    m = SPEAKER_RE.match(seg.strip())
    if not m:
        return None
    prefix = m.group("prefix")
    canon = PREFIX_CANON.get(prefix.lower(), prefix[:1].upper() + prefix[1:].lower())
    return f"{canon} {m.group('name').strip()}"


def _parse_date(title: str, timestamp: int | None) -> str | None:
    m = DATE_RE.search(title)
    approx = datetime.fromtimestamp(timestamp, timezone.utc) if timestamp else None
    if not m:
        return approx.strftime("%Y-%m-%d") if approx else None
    a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    y = y + 2000 if y < 100 else y
    candidates = []
    for d, mo in ((a, b), (b, a)):  # dd/mm (pt) e mm/dd (en)
        try:
            candidates.append(datetime(y, mo, d, tzinfo=timezone.utc))
        except ValueError:
            pass
    if not candidates:
        return approx.strftime("%Y-%m-%d") if approx else None
    english = bool(re.search(r"live service|live stream|sunday|youth", title, re.I))
    if len(candidates) == 2 and candidates[0] != candidates[1]:
        if approx:
            best = min(candidates, key=lambda c: abs((c - approx).total_seconds()))
        else:
            best = candidates[1] if english else candidates[0]
    else:
        best = candidates[0]
    return best.strftime("%Y-%m-%d")


def parse_title(title: str, timestamp: int | None = None) -> dict[str, str]:
    """Extrai pregador, evento, palavra (tema) e data de títulos como
    'Live Service - Part of the Story - Pastor Paulo Itajahy - 11/08/2026'."""
    clean = re.sub(r"\s+", " ", title or "").strip()
    date = _parse_date(clean, timestamp) or ""
    no_date = DATE_RE.sub("", clean)
    segments = [s.strip(" -–|:") for s in re.split(r"\s[-–|]\s|\s\|\s?|\|", no_date)]
    segments = [s for s in segments if s]

    speaker = ""
    event = ""
    themes: list[str] = []
    for seg in segments:
        sp = _canon_speaker(seg)
        if sp and not speaker:
            speaker = sp
        elif GENERIC_RE.match(seg):
            continue
        elif EVENT_RE.search(seg) and not event and len(seg.split()) <= 4:
            event = seg
        else:
            themes.append(seg)
    # Em 'He is a Shepherd - Isabella | Youth Core' o 2º segmento curto é o pregador sem título:
    # nesse caso a pasta principal é o evento e o nome entra junto da palavra.
    untitled = ""
    if not speaker and event and len(themes) >= 2 and len(themes[-1].split()) <= 3:
        untitled = themes.pop()
    word = themes[0] if themes else ""
    group = speaker or event or "Outros"
    if not word:
        word = event if (event and event != group) else "Culto"
    if untitled:
        word = f"{word} ({untitled})"
    return {"pregador": speaker or untitled, "evento": event, "palavra": word, "data": date, "grupo": group}


def safe_segment(name: str) -> str:
    s = re.sub(r'[\\/*?:"<>|\x00-\x1f]+', " - ", str(name))
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"(?:\s+-+\s*|\s*-+\s+|-{2,})+", " - ", s).strip(" .-")
    return s[:120] or "Sem nome"


def safe_relpath(rel: str) -> str:
    parts = [safe_segment(p) for p in re.split(r"[/\\]+", rel or "") if p.strip() and p.strip() not in (".", "..")]
    return "/".join(parts)


def render_folder(template: str, info: dict[str, str], title: str, video_id: str) -> str:
    data = info.get("data") or ""
    values = {
        "grupo": info.get("grupo") or "Outros",
        "pregador": info.get("pregador") or info.get("evento") or "Outros",
        "evento": info.get("evento") or info.get("grupo") or "Outros",
        "palavra": info.get("palavra") or title,
        "data": data or "sem-data",
        "ano": data[:4] or "sem-ano",
        "mes": data[:7] or "sem-mes",
        "titulo": title,
        "id": video_id,
    }
    try:
        rel = (template or DEFAULT_SETTINGS["folder_template"]).format(**values)
    except (KeyError, IndexError, ValueError):
        rel = DEFAULT_SETTINGS["folder_template"].format(**values)
    return safe_relpath(rel)


# ---------------------------------------------------------------------------
# Listagem de canal
# ---------------------------------------------------------------------------

def channel_tab_url(channel: str, tab: str) -> str:
    c = (channel or DEFAULT_CHANNEL).strip()
    if re.fullmatch(r"UC[A-Za-z0-9_-]{22}", c):
        base = f"https://www.youtube.com/channel/{c}"
    elif c.startswith("@"):
        base = f"https://www.youtube.com/{c}"
    elif re.match(r"^https?://", c) or "youtube.com" in c:
        base = c if c.startswith("http") else "https://" + c
        base = re.sub(r"/(videos|streams|shorts|featured|live|playlists|community)/?$", "", base.split("?")[0].rstrip("/"))
    else:
        base = f"https://www.youtube.com/@{c.lstrip('@')}"
    return f"{base.rstrip('/')}/{tab}"


def _ytdlp_json(cmd_base: list[str], url: str, extra: list[str], timeout: int = 180) -> dict[str, Any]:
    cmd = cmd_base + ["--dump-single-json", "--no-warnings"] + extra
    if Path("cookies.txt").exists():
        cmd += ["--cookies", "cookies.txt"]
    cmd.append(url)
    res = subprocess.run(cmd, text=True, capture_output=True, check=False, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip()[-400:] or "yt-dlp falhou")
    return json.loads(res.stdout)


def _row(entry: dict[str, Any], tab: str, settings: dict[str, Any], history: dict[str, Any]) -> dict[str, Any] | None:
    vid = entry.get("id") or ""
    if not VIDEO_ID_RE.fullmatch(vid):
        return None
    title = re.sub(r"\s+", " ", entry.get("title") or vid).strip()
    ts = entry.get("timestamp") or entry.get("release_timestamp")
    if not ts and entry.get("upload_date"):
        try:
            ts = int(datetime.strptime(entry["upload_date"], "%Y%m%d").replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            ts = None
    info = parse_title(title, ts)
    dur = int(float(entry.get("duration") or 0))
    live_status = entry.get("live_status") or ("was_live" if tab == "streams" else "not_live")
    return {
        "id": vid,
        "title": title,
        "duration": dur,
        "timestamp": ts,
        "date": info["data"],
        "views": entry.get("view_count"),
        "live_status": live_status,
        "is_live_stream": live_status in ("was_live", "is_live", "post_live") or tab == "streams",
        "available": live_status not in ("is_live", "is_upcoming"),
        "tab": tab,
        "thumbnail": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
        "parsed": info,
        "folder": render_folder(settings.get("folder_template", ""), info, title, vid),
        "downloaded": history.get(vid),
    }


def list_channel(ytdlp: str, channel: str, tab: str = "streams", limit: int = 60,
                 settings: dict[str, Any] | None = None, history: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = settings or DEFAULT_SETTINGS
    history = history or {}
    tabs = ["streams", "videos"] if tab == "all" else [tab if tab in ("streams", "videos", "shorts") else "streams"]
    rows: dict[str, dict[str, Any]] = {}
    meta: dict[str, Any] = {}
    errors: list[str] = []
    for t in tabs:
        try:
            data = _ytdlp_json([ytdlp, "--flat-playlist", "--playlist-end", str(max(1, min(int(limit), 1000)))],
                               channel_tab_url(channel, t), ["--extractor-args", "youtubetab:approximate_date"])
        except Exception as exc:  # aba inexistente no canal (ex.: sem Shorts)
            errors.append(f"{t}: {exc}")
            continue
        meta = meta or {"name": data.get("channel") or data.get("uploader") or data.get("title"),
                        "id": data.get("channel_id"), "handle": data.get("uploader_id"),
                        "url": data.get("channel_url") or data.get("uploader_url")}
        for entry in data.get("entries") or []:
            row = _row(entry or {}, t, settings, history)
            if row and row["id"] not in rows:
                rows[row["id"]] = row
    if not rows and errors:
        raise RuntimeError("Não foi possível listar o canal: " + " | ".join(errors))
    videos = sorted(rows.values(), key=lambda r: r.get("timestamp") or 0, reverse=True)
    return {"channel": meta, "videos": videos, "errors": errors}


def lookup_urls(ytdlp: str, urls: list[str], settings: dict[str, Any], history: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve links colados (vídeos ou playlists) para linhas do grid."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in urls[:200]:
        raw = raw.strip()
        if not raw:
            continue
        url = f"https://www.youtube.com/watch?v={raw}" if VIDEO_ID_RE.fullmatch(raw) else raw
        try:
            data = _ytdlp_json([ytdlp, "--flat-playlist"], url, [], timeout=90)
        except Exception as exc:
            LOG.warning("Falha ao resolver %s: %s", raw, exc)
            continue
        entries = data.get("entries") if data.get("_type") == "playlist" else [data]
        for e in entries or []:
            row = _row(e or {}, "videos", settings, history)
            if row and row["id"] not in seen:
                seen.add(row["id"])
                out.append(row)
    return out


# ---------------------------------------------------------------------------
# Fila
# ---------------------------------------------------------------------------

def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def place_file(src: Path, dst: Path) -> None:
    """Coloca o arquivo no destino com hardlink (instantâneo, sem ocupar espaço) ou cópia."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        try:
            if dst.samefile(src):
                return
        except OSError:
            pass
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        tmp = dst.with_name(dst.name + ".part")
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)


class BatchManager:
    """Fila persistente com N workers. `ops` injeta as funções de exportação do server."""

    def __init__(self, ops: dict[str, Callable[..., Any]]):
        self.ops = ops
        self.lock = threading.RLock()
        self.settings: dict[str, Any] = {**DEFAULT_SETTINGS, **_read_json(SETTINGS_FILE, {})}
        self.history: dict[str, Any] = _read_json(HISTORY_FILE, {})
        self.jobs: list[dict[str, Any]] = _read_json(QUEUE_FILE, [])
        for j in self.jobs:  # jobs interrompidos por reinício voltam para a fila
            if j.get("status") in ("running", "waiting"):
                j.update(status="queued", progress=0, stage=None, speed=None, eta=None,
                         message="Retomado após reinício do servidor.")
        self.paused = False
        self.cooldown_until = 0.0
        self.next_start_at = 0.0
        self.cancel_events: dict[str, threading.Event] = {}
        self.procs: dict[str, set[subprocess.Popen]] = {}
        self._dirty = False
        threading.Thread(target=self._dispatcher, daemon=True, name="batch-dispatcher").start()

    # ----- persistência -------------------------------------------------
    def _save(self) -> None:
        with self.lock:
            _write_json(QUEUE_FILE, self.jobs)
            self._dirty = False

    def _save_history(self) -> None:
        with self.lock:
            _write_json(HISTORY_FILE, self.history)

    # ----- API pública --------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            jobs = [dict(j) for j in self.jobs]
            counts: dict[str, int] = {}
            for j in jobs:
                counts[j["status"]] = counts.get(j["status"], 0) + 1
            return {
                "settings": dict(self.settings),
                "jobs": jobs,
                "counts": counts,
                "paused": self.paused,
                "cooldown_left": max(0, int(self.cooldown_until - time.time())),
                "suggested_concurrency": SUGGESTED_CONCURRENCY,
                "max_concurrency": MAX_CONCURRENCY,
            }

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if "concurrency" in patch:
                self.settings["concurrency"] = max(1, min(MAX_CONCURRENCY, int(patch["concurrency"])))
            if "output_dir" in patch and str(patch["output_dir"]).strip():
                out = Path(os.path.expanduser(str(patch["output_dir"]).strip()))
                if not out.is_absolute():
                    raise ValueError("Informe um caminho absoluto para a pasta de destino.")
                self.settings["output_dir"] = str(out)
            if "limit_rate" in patch:
                lr = str(patch["limit_rate"] or "").strip().upper()
                if lr and not re.fullmatch(r"\d+(\.\d+)?[KMG]?", lr):
                    raise ValueError("Limite de banda inválido (use por ex. 2M, 500K).")
                self.settings["limit_rate"] = lr
            if "folder_template" in patch:
                self.settings["folder_template"] = str(patch["folder_template"] or "").strip() or DEFAULT_SETTINGS["folder_template"]
            for key in ("keep_cache", "skip_downloaded"):
                if key in patch:
                    self.settings[key] = bool(patch[key])
            _write_json(SETTINGS_FILE, self.settings)
            return dict(self.settings)

    def enqueue(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        added, skipped = 0, []
        with self.lock:
            active = {j["video_id"] for j in self.jobs if j["status"] in ("queued", "running", "waiting")}
            for it in items:
                vid = str(it.get("video_id") or "")
                if not VIDEO_ID_RE.fullmatch(vid):
                    continue
                if vid in active:
                    skipped.append(vid)
                    continue
                mode = it.get("mode") if it.get("mode") in ("video", "audio", "both", "none") else "video"
                job = {
                    "id": uuid.uuid4().hex[:10],
                    "video_id": vid,
                    "title": str(it.get("title") or vid).strip(),
                    "base_name": safe_segment(it.get("base_name") or it.get("title") or vid),
                    "folder": safe_relpath(str(it.get("folder") or "")),
                    "quality": str(it.get("quality") or "720"),
                    "mode": mode,
                    "cover": bool(it.get("cover")),
                    "thumb": bool(it.get("thumb")),
                    "duration": int(it.get("duration") or 0),
                    "status": "queued",
                    "stage": None, "progress": 0, "speed": None, "eta": None,
                    "message": "Na fila", "error": None, "attempts": 0, "files": [],
                    "added_at": time.time(), "started_at": None, "finished_at": None,
                }
                if mode == "none" and not (job["cover"] or job["thumb"]):
                    continue
                self.jobs.append(job)
                active.add(vid)
                added += 1
            self._save()
        return {"added": added, "skipped": skipped}

    def control(self, action: str, job_id: str | None = None, extra: dict[str, Any] | None = None) -> None:
        with self.lock:
            job = next((j for j in self.jobs if j["id"] == job_id), None) if job_id else None
            if action == "pause":
                self.paused = True
            elif action == "resume":
                self.paused = False
                self.cooldown_until = 0
            elif action == "cancel" and job:
                if job["status"] in ("running", "waiting"):
                    ev = self.cancel_events.get(job["id"])
                    if ev:
                        ev.set()
                    for p in list(self.procs.get(job["id"], ())):
                        try:
                            p.terminate()
                        except Exception:
                            pass
                elif job["status"] == "queued":
                    job.update(status="cancelled", message="Cancelado.")
            elif action == "cancel_all":
                for j in self.jobs:
                    if j["status"] == "queued":
                        j.update(status="cancelled", message="Cancelado.")
                    elif j["status"] in ("running", "waiting"):
                        self.control("cancel", j["id"])
            elif action == "retry" and job and job["status"] in ("error", "cancelled"):
                job.update(status="queued", progress=0, error=None, attempts=0, message="Na fila (nova tentativa)")
            elif action == "retry_failed":
                for j in self.jobs:
                    if j["status"] in ("error", "cancelled"):
                        j.update(status="queued", progress=0, error=None, attempts=0, message="Na fila (nova tentativa)")
            elif action == "remove" and job and job["status"] not in ("running", "waiting"):
                self.jobs.remove(job)
            elif action == "clear_done":
                self.jobs = [j for j in self.jobs if j["status"] not in ("done", "cancelled")]
            elif action in ("up", "down", "top") and job and job["status"] == "queued":
                i = self.jobs.index(job)
                self.jobs.pop(i)
                if action == "top":
                    self.jobs.insert(0, job)
                else:
                    self.jobs.insert(max(0, min(len(self.jobs), i + (-1 if action == "up" else 1))), job)
            elif action == "edit" and job and job["status"] in ("queued", "error", "cancelled") and extra:
                if "folder" in extra:
                    job["folder"] = safe_relpath(str(extra["folder"]))
                if "base_name" in extra:
                    job["base_name"] = safe_segment(extra["base_name"])
            self._save()

    def forget_history(self, video_id: str) -> None:
        with self.lock:
            self.history.pop(video_id, None)
            self._save_history()

    # ----- execução -------------------------------------------------------
    def _set(self, job: dict[str, Any], persist: bool = False, **fields: Any) -> None:
        with self.lock:
            job.update(fields)
            if persist:
                self._save()

    def _dispatcher(self) -> None:
        while True:
            time.sleep(0.5)
            try:
                with self.lock:
                    now = time.time()
                    if self.paused or now < self.cooldown_until or now < self.next_start_at:
                        continue
                    running = sum(1 for j in self.jobs if j["status"] in ("running", "waiting"))
                    if running >= int(self.settings.get("concurrency", SUGGESTED_CONCURRENCY)):
                        continue
                    job = next((j for j in self.jobs if j["status"] == "queued" and now >= j.get("retry_at", 0)), None)
                    if not job:
                        continue
                    job.update(status="running", started_at=now, message="Iniciando...", error=None)
                    self.cancel_events[job["id"]] = threading.Event()
                    self.procs[job["id"]] = set()
                    # início escalonado com jitter: evita rajadas de requisições ao YouTube
                    self.next_start_at = now + MIN_START_GAP + random.uniform(0, 3)
                    self._save()
                threading.Thread(target=self._run_job, args=(job,), daemon=True, name=f"batch-{job['id']}").start()
            except Exception:
                LOG.exception("Erro no dispatcher do lote")

    def _run_job(self, job: dict[str, Any]) -> None:
        from processar_podcast import YTDLP_THREAD_OPTS

        jid = job["id"]
        cancel_ev = self.cancel_events[jid]
        lr = self.settings.get("limit_rate") or ""
        YTDLP_THREAD_OPTS.extra_args = ["--limit-rate", lr] if lr else []
        mode = job["mode"]
        steps: list[tuple[str, float]] = []
        if mode in ("video", "both"):
            steps.append(("video", 80 if mode == "video" else 70))
        if mode in ("audio", "both"):
            steps.append(("audio", 85 if mode == "audio" else 15))
        if job["cover"]:
            steps.append(("cover", 6))
        if job["thumb"]:
            steps.append(("thumb", 4))
        total_w = sum(w for _, w in steps) or 1
        done_w = 0.0

        def overall(frac: float, weight: float) -> int:
            return int(min(99, (done_w + weight * max(0.0, min(1.0, frac))) / total_w * 100))

        def register(p: subprocess.Popen) -> None:
            self.procs.setdefault(jid, set()).add(p)

        def cancelled() -> bool:
            return cancel_ev.is_set()

        out_dir = Path(self.settings["output_dir"]) / (job["folder"] or "")
        name_for = self.ops["download_filename"]
        files: list[str] = []
        try:
            for step, weight in steps:
                if cancelled():
                    raise InterruptedError()
                if step == "video":
                    def on_dl(pct, spd, eta, w=weight):
                        self._set(job, stage="downloading", progress=overall(pct / 100 * 0.85, w), speed=spd, eta=eta,
                                  message=f"Baixando vídeo {job['quality']}p... {pct:.0f}%")

                    def on_enc(pct, spd, eta, w=weight):
                        self._set(job, stage="encoding", progress=overall(0.85 + pct / 100 * 0.15, w), speed=spd, eta=eta,
                                  message=f"Otimizando MP4 (+faststart)... {pct:.0f}%")

                    res = self.ops["ensure_mp4"](job["video_id"], None, None, job["quality"], job["title"],
                                                 on_dl=on_dl, on_enc=on_enc,
                                                 report=lambda s, m, p: self._set(job, stage=s, message=m),
                                                 cancel=cancelled, register=register)
                    dst = out_dir / name_for(f"{job['video_id']}.mp4", job["base_name"])
                elif step == "audio":
                    def on_dl_a(pct, spd, eta, w=weight):
                        self._set(job, stage="downloading", progress=overall(pct / 100 * 0.6, w), speed=spd, eta=eta,
                                  message=f"Baixando áudio... {pct:.0f}%")

                    def on_enc_a(pct, spd, eta, w=weight):
                        self._set(job, stage="encoding", progress=overall(0.6 + pct / 100 * 0.4, w), speed=spd, eta=eta,
                                  message=f"Normalizando (-16 LUFS) e gerando MP3... {pct:.0f}%")

                    res = self.ops["ensure_mp3"](job["video_id"], None, None, job["title"], None,
                                                 on_dl=on_dl_a, on_enc=on_enc_a,
                                                 report=lambda s, m, p: self._set(job, stage=s, message=m),
                                                 cancel=cancelled, register=register)
                    dst = out_dir / name_for(f"{job['video_id']}.mp3", job["base_name"])
                elif step == "cover":
                    self._set(job, stage="artwork", message="Gerando capa 1:1...")
                    res = {"path": self.ops["ensure_artwork"](job["video_id"], None, job["title"])}
                    dst = out_dir / name_for(f"{job['video_id']}.jpg", job["base_name"])
                else:
                    self._set(job, stage="artwork", message="Baixando thumbnail 16:9...")
                    thumb = self.ops["exports_dir"]() / f"{job['video_id']}_thumb.jpg"
                    if not thumb.is_file():
                        self.ops["download_thumbnail"](job["video_id"], thumb)
                    res = {"path": thumb}
                    dst = out_dir / name_for(f"{job['video_id']}_thumb.jpg", job["base_name"])
                src = Path(res.get("mp4") or res.get("mp3") or res.get("path"))
                self._set(job, stage="saving", message=f"Salvando {dst.name}...")
                place_file(src, dst)
                files.append(str(dst))
                done_w += weight
                self._set(job, progress=overall(0, 0), files=list(files))

            if not self.settings.get("keep_cache"):
                cache = self.ops["exports_dir"]() / ".cache"
                for p in list(cache.glob(f"raw_{job['video_id']}_*")) + list(cache.glob(f"raw_audio_{job['video_id']}.*")):
                    try:
                        p.unlink()
                    except OSError:
                        pass
            with self.lock:
                self.history[job["video_id"]] = {"at": time.time(), "folder": str(out_dir), "files": files,
                                                 "title": job["title"]}
                self._save_history()
            self._set(job, persist=True, status="done", progress=100, stage="complete", speed=None, eta=None,
                      finished_at=time.time(), message=f"Concluído • {len(files)} arquivo(s)")
        except Exception as exc:
            if isinstance(exc, InterruptedError) or cancelled():
                self._set(job, persist=True, status="cancelled", stage=None, speed=None, eta=None,
                          message="Cancelado pelo usuário.", files=files)
            else:
                err = str(exc)[-500:]
                attempts = job.get("attempts", 0) + 1
                limited = bool(RATE_LIMIT_RE.search(err))
                if limited:
                    with self.lock:
                        self.cooldown_until = time.time() + RATE_LIMIT_COOLDOWN
                    LOG.warning("YouTube limitou as requisições; pausa global de %ss", RATE_LIMIT_COOLDOWN)
                if attempts <= len(RETRY_DELAYS):
                    delay = RETRY_DELAYS[attempts - 1] + (RATE_LIMIT_COOLDOWN if limited else 0)
                    self._set(job, persist=True, status="queued", attempts=attempts, retry_at=time.time() + delay,
                              error=err, stage=None, speed=None, eta=None, progress=0,
                              message=f"Falhou ({'limite do YouTube' if limited else 'erro'}); nova tentativa "
                                      f"{attempts}/{len(RETRY_DELAYS)} em {delay}s")
                else:
                    self._set(job, persist=True, status="error", attempts=attempts, error=err, stage="error",
                              speed=None, eta=None, finished_at=time.time(), message="Falhou após várias tentativas.")
        finally:
            YTDLP_THREAD_OPTS.extra_args = []
            with self.lock:
                self.cancel_events.pop(jid, None)
                self.procs.pop(jid, None)

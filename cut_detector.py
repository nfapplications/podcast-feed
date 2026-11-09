"""Módulo de detecção e sugestão de cortes inteligentes de pregações com IA."""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

from processar_podcast import ytdlp_cmd, seconds, video_details

LOG = logging.getLogger("cut_detector")

PREACHING_KEYWORDS = [
    "palavra", "prega", "pregação", "mensagem", "sermão", "sermao",
    "ministra", "ministração", "pastor", "pastora", "bispo", "estudo"
]


def check_chapters_or_description(details: dict[str, Any]) -> dict[str, Any] | None:
    """Tenta identificar o corte de pregação através de capítulos ou timestamps na descrição (0 tokens)."""
    # 1. Capítulos oficiais
    chapters = details.get("chapters")
    if isinstance(chapters, list) and chapters:
        for idx, ch in enumerate(chapters):
            title = ch.get("title", "").lower()
            if any(kw in title for kw in PREACHING_KEYWORDS):
                s_sec = int(ch.get("start_time", 0))
                # Se houver próximo capítulo, o fim é o início dele; senão, end_time do capítulo
                e_sec = int(ch.get("end_time", 0))
                if e_sec <= s_sec and idx + 1 < len(chapters):
                    e_sec = int(chapters[idx + 1].get("start_time", s_sec + 2700))
                if e_sec > s_sec:
                    return {
                        "start": format_seconds(s_sec),
                        "end": format_seconds(e_sec),
                        "start_sec": s_sec,
                        "end_sec": e_sec,
                        "title": ch.get("title", ""),
                        "source": "capitulos_youtube",
                        "reasoning": f"Identificado diretamente nos capítulos do YouTube: '{ch.get('title')}'."
                    }

    # 2. Descrição do vídeo
    desc = details.get("description", "")
    if desc:
        lines = desc.splitlines()
        found_stamps: list[tuple[int, str]] = []
        for line in lines:
            m = re.search(r"\b(?:(\d{1,2}):)?(\d{2}):(\d{2})\b", line)
            if m:
                h = int(m.group(1)) if m.group(1) else 0
                minute = int(m.group(2))
                s = int(m.group(3))
                total_s = h * 3600 + minute * 60 + s
                found_stamps.append((total_s, line.strip()))

        for idx, (t_sec, text) in enumerate(found_stamps):
            text_lower = text.lower()
            if any(kw in text_lower for kw in PREACHING_KEYWORDS):
                end_s = found_stamps[idx + 1][0] if idx + 1 < len(found_stamps) else t_sec + 2700
                return {
                    "start": format_seconds(t_sec),
                    "end": format_seconds(end_s),
                    "start_sec": t_sec,
                    "end_sec": end_s,
                    "title": text,
                    "source": "descricao_youtube",
                    "reasoning": f"Identificado na descrição do vídeo: '{text}'."
                }

    return None


def extract_transcript_chunks(video_id: str) -> list[str]:
    """Baixa a legenda automática em português e formata em blocos de 30 segundos."""
    with tempfile.TemporaryDirectory(prefix="yt-sub-") as tmp:
        out_tmpl = str(Path(tmp) / "%(id)s.%(ext)s")
        cmd = [
            ytdlp_cmd(),
            "--skip-download",
            "--write-auto-subs",
            "--sub-lang", "pt",
            "--sub-format", "json3",
            "-o", out_tmpl,
            f"https://www.youtube.com/watch?v={video_id}"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            LOG.warning("yt-dlp falhou ao baixar legendas: %s", res.stderr)
            return []

        sub_file = Path(tmp) / f"{video_id}.pt.json3"
        if not sub_file.exists():
            # Tenta encontrar qualquer arquivo de legenda gerado
            candidates = list(Path(tmp).glob(f"{video_id}*.json3"))
            if candidates:
                sub_file = candidates[0]
            else:
                return []

        data = json.loads(sub_file.read_text(encoding="utf-8"))
        events = data.get("events", [])
        chunks: list[str] = []
        curr_min = -1
        curr_text: list[str] = []

        for ev in events:
            t_ms = ev.get("tStartMs", 0)
            sec = t_ms // 1000
            m_idx = sec // 30  # blocos de 30 segundos
            segs = ev.get("segs", [])
            txt = "".join(s.get("utf8", "") for s in segs).strip()
            if not txt or txt == "\n":
                continue
            if m_idx != curr_min:
                if curr_text:
                    s_block = curr_min * 30
                    h, m, s = s_block // 3600, (s_block % 3600) // 60, s_block % 60
                    chunks.append(f"[{h:02d}:{m:02d}:{s:02d}] {' '.join(curr_text)}")
                curr_min = m_idx
                curr_text = [txt]
            else:
                curr_text.append(txt)

        if curr_text:
            s_block = curr_min * 30
            h, m, s = s_block // 3600, (s_block % 3600) // 60, s_block % 60
            chunks.append(f"[{h:02d}:{m:02d}:{s:02d}] {' '.join(curr_text)}")

        return chunks


def call_gemini_api(prompt_text: str, api_key: str) -> dict[str, Any]:
    """Chama a API do Google Gemini usando modelos Flash ultrarrápidos e econômicos."""
    models_to_try = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
    last_err = None

    for model in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        payload = {
            "contents": [{
                "parts": [{"text": prompt_text}]
            }],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json"
            }
        }
        body_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body_bytes,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                candidates = resp_data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        raw_text = parts[0].get("text", "")
                        return json.loads(raw_text)
        except urllib.error.HTTPError as http_err:
            last_err = http_err
            LOG.warning("Erro HTTP chamando modelo %s: %s", model, http_err)
            continue
        except Exception as exc:
            last_err = exc
            LOG.warning("Erro chamando modelo %s: %s", model, exc)
            continue

    raise RuntimeError(f"Falha ao chamar API Gemini: {last_err}")


def suggest_sermon_cut(video_id: str, gemini_api_key: str | None = None) -> dict[str, Any]:
    """Identifica o corte ideal da pregação: primeiro por capítulos/descrição, depois por IA."""
    details = video_details(video_id)

    # 1. Tentar capítulos ou timestamps da descrição (0 tokens)
    direct_match = check_chapters_or_description(details)
    if direct_match:
        return direct_match

    # 2. Se não encontrou capítulos, usar a transcrição com IA
    api_key = (gemini_api_key or os.environ.get("GEMINI_API_KEY", "")).strip()
    if not api_key:
        raise ValueError(
            "O vídeo não possui capítulos no YouTube e a chave GEMINI_API_KEY não foi configurada. "
            "Por favor, configure sua chave na aba Configurações para usar a IA."
        )

    chunks = extract_transcript_chunks(video_id)
    if not chunks:
        raise ValueError(
            "Não foi possível extrair legendas automáticas deste vídeo no YouTube. "
            "Defina os horários de início e fim manualmente no reprodutor."
        )

    transcript_text = "\n".join(chunks)

    prompt = f"""Você é um assistente especialista em produção de áudio e podcasts para igrejas.
Abaixo está a transcrição com marcações de tempo [HH:MM:SS] de um culto evangélico transmitido ao vivo pela Igreja Núcleo da Fé.
O título original da transmissão no YouTube é: "{details.get('title', '')}".

Sua tarefa:
Identificar o trecho exato correspondente à PREGAÇÃO / MINISTRAÇÃO DA PALAVRA para recorte e publicação em podcast.

Estrutura típica de culto:
1. Início: Músicas de louvor e adoração (geralmente primeiros 30 a 50 minutos).
2. Transição: Avisos, oração pelos dízimos/ofertas e apresentação do pregador.
3. PREGAÇÃO: Início da mensagem (leitura bíblica, tema da palavra) até a conclusão do sermão.
4. Final: Oração de encerramento, apelo e bênção final.

Instruções:
- Identifique o timestamp de início exato [HH:MM:SS] em que o pastor/pastora começa a ministrar o sermão.
- Identifique o timestamp de término [HH:MM:SS] em que o sermão termina (antes ou no início da oração final).
- Sugira um título conciso e marcante para o episódio.
- Forneça uma breve explicação (1-2 frases) justificando os pontos de corte escolhidos.

Retorne EXCLUSIVAMENTE um objeto JSON com o formato:
{{
  "start": "HH:MM:SS",
  "end": "HH:MM:SS",
  "title": "Título Marcante do Episódio",
  "reasoning": "Breve justificativa..."
}}

Transcrição:
{transcript_text}
"""

    result = call_gemini_api(prompt, api_key)
    start_str = result.get("start", "")
    end_str = result.get("end", "")

    s_sec = seconds(start_str)
    e_sec = seconds(end_str)

    return {
        "start": format_seconds(s_sec),
        "end": format_seconds(e_sec),
        "start_sec": s_sec,
        "end_sec": e_sec,
        "title": result.get("title", details.get("title", "")),
        "source": "gemini_ai",
        "reasoning": result.get("reasoning", "Corte detectado pela IA através da análise textual do sermão.")
    }


def format_seconds(secs: int) -> str:
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

#!/usr/bin/env python3
"""Módulo de automação de upload de vídeo para o Spotify for Creators (Playwright)."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

LOG = logging.getLogger("spotify-uploader")

STATE: dict[str, Any] = {
    "status": "idle",  # idle, opening_browser, waiting_login, uploading, filling_details, ready_for_review, success, error
    "message": "Nenhum upload para o Spotify em andamento.",
    "error": None,
    "current_url": None,
}
STATE_LOCK = threading.Lock()
LOGIN_EVENT = threading.Event()
STOP_EVENT = threading.Event()


def get_spotify_state() -> dict[str, Any]:
    with STATE_LOCK:
        return dict(STATE)


def set_spotify_state(status: str, message: str, error: str | None = None, current_url: str | None = None) -> None:
    with STATE_LOCK:
        STATE["status"] = status
        STATE["message"] = message
        if error:
            STATE["error"] = error
        if current_url:
            STATE["current_url"] = current_url
        LOG.info("[%s] %s", status, message)


def confirm_login() -> None:
    """Acionado pelo usuário quando ele clica no botão 'Já fiz o Login' no Studio."""
    LOGIN_EVENT.set()


def cancel_spotify_upload() -> None:
    """Cancela o upload em andamento e fecha o navegador se estiver ativo."""
    STOP_EVENT.set()
    LOGIN_EVENT.set()
    set_spotify_state("idle", "Upload para o Spotify cancelado pelo usuário.")


def run_spotify_upload(
    video_path: Path,
    artwork_path: Path | None,
    title: str,
    description: str,
    auto_publish: bool = False,
) -> None:
    """Executa o processo de upload via Playwright com navegador visível."""
    LOGIN_EVENT.clear()
    STOP_EVENT.clear()
    set_spotify_state("opening_browser", "Iniciando o Google Chrome com o perfil do Spotify...")

    try:
        from playwright.sync_api import sync_playwright

        profile_dir = Path(".spotify_profile").resolve()
        profile_dir.mkdir(exist_ok=True)

        with sync_playwright() as p:
            # Launch system Chrome with persistent user data dir (saves login sessions!)
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                channel="chrome",
                headless=False,  # Janela visível para o usuário interagir
                viewport={"width": 1280, "height": 900},
            )
            page = context.pages[0] if context.pages else context.new_page()

            target_url = "https://podcasters.spotify.com/pod/dashboard/episode/wizard"
            set_spotify_state("opening_browser", "Navegando para o Spotify for Podcasters...", current_url=target_url)
            page.goto(target_url, timeout=45000)
            time.sleep(3)

            if STOP_EVENT.is_set():
                context.close()
                return

            # Check if login is needed
            cur_url = page.url.lower()
            needs_login = (
                "accounts.spotify.com" in cur_url
                or "login" in cur_url
                or "auth" in cur_url
                or "pod/dashboard" not in cur_url
                or page.locator('text="Entrar"').count() > 0
                or page.locator('text="Log in"').count() > 0
            )

            if needs_login:
                set_spotify_state(
                    "waiting_login",
                    "🔑 Faça o login na janela do Chrome. Em seguida, clique em 'Já fiz o Login, Continuar Envio' no Studio.",
                    current_url=page.url,
                )

                # Wait until user finishes login: either via button click OR automatic URL detection!
                for _ in range(600):  # Wait up to 10 minutes
                    if STOP_EVENT.is_set():
                        context.close()
                        return
                    if LOGIN_EVENT.is_set():
                        LOG.info("Login confirmado manualmente pelo usuário no Studio!")
                        break
                    try:
                        cur = page.url.lower()
                        if "pod/dashboard" in cur and "login" not in cur and "accounts.spotify.com" not in cur:
                            LOG.info("Login detectado automaticamente pela URL do painel!")
                            break
                    except Exception:
                        pass
                    time.sleep(1)

                set_spotify_state("uploading", "Login concluído! Acessando o assistente de publicação...")
                if "episode/wizard" not in page.url:
                    page.goto(target_url, timeout=30000)
                    time.sleep(3)

            if STOP_EVENT.is_set():
                context.close()
                return

            # Locate file input
            set_spotify_state("uploading", "Localizando campo de upload do arquivo MP4...")
            page.wait_for_selector('input[type="file"]', timeout=30000)
            file_input = page.locator('input[type="file"]').first

            file_size_mb = video_path.stat().st_size // 1024 // 1024
            set_spotify_state("uploading", f"Enviando arquivo {video_path.name} ({file_size_mb} MB)...")
            file_input.set_input_files(str(video_path))
            time.sleep(5)

            # Fill title and description
            set_spotify_state("filling_details", "Preenchendo título e detalhes do episódio...")

            # Wait for title input
            try:
                title_input = page.locator('input[name="title"], input[data-testid="episode-title"], #title, input[placeholder*="título" i], input[placeholder*="title" i]').first
                if title_input.count():
                    title_input.fill(title)
            except Exception as e:
                LOG.warning("Tentativa de preencher título: %s", e)

            # Fill description
            try:
                desc_input = page.locator('textarea, div[contenteditable="true"], div[data-testid="rich-text-editor"]').first
                if desc_input.count():
                    desc_input.fill(description)
            except Exception as e:
                LOG.warning("Tentativa de preencher descrição: %s", e)

            # Upload artwork if available
            if artwork_path and artwork_path.exists():
                try:
                    art_input = page.locator('input[type="file"][accept*="image"]').first
                    if art_input.count():
                        art_input.set_input_files(str(artwork_path))
                except Exception:
                    pass

            set_spotify_state(
                "ready_for_review",
                "✅ Episódio carregado com sucesso na janela do Spotify! Revise e clique em Publicar no Chrome.",
                current_url=page.url,
            )

            # Keep context open so user can inspect or click publish in Chrome
            LOG.info("Aguardando usuário revisar no navegador Chrome (o navegador permanecerá aberto até ser fechado)...")
            try:
                while not page.is_closed() and not STOP_EVENT.is_set():
                    time.sleep(2)
            except Exception:
                pass

            if not STOP_EVENT.is_set():
                set_spotify_state("idle", "Upload concluído e navegador fechado.")

    except Exception as exc:
        LOG.exception("Erro na automação do Spotify: %s", exc)
        set_spotify_state("error", f"Falha na automação: {exc}", error=str(exc))

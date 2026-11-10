import unittest
from pathlib import Path
import xml.etree.ElementTree as ET

from processar_podcast import (
    add_episode,
    clean_text,
    episode_date,
    existing_guids,
    seconds,
    update_channel,
)
from scan_videos import classify
from server import parse_video_id
from spotify_uploader import (
    cancel_spotify_upload,
    confirm_login,
    get_spotify_state,
    set_spotify_state,
)


class PodcastTests(unittest.TestCase):
    def test_recorte_precisa_de_duas_verificacoes(self):
        longest, checks, status = classify({}, 6600, False, 4500, 600)
        self.assertEqual(status, "monitoring")
        first = {"longest_duration": longest, "duration": 6600, "stable_checks": checks}
        longest, checks, status = classify(first, 3000, False, 4500, 600)
        self.assertEqual(status, "confirming")
        second = {"longest_duration": longest, "duration": 3000, "stable_checks": checks}
        self.assertEqual(classify(second, 3000, False, 4500, 600)[2], "ready")

    def test_video_ja_curto_exige_revisao(self):
        self.assertEqual(classify({}, 3000, False, 4500, 600)[2], "short_without_baseline")

    def test_video_ja_publicado_fica_com_status_published(self):
        _, _, status = classify({}, 3000, True, 4500, 600)
        self.assertEqual(status, "published")

    def test_parse_segundos(self):
        self.assertEqual(seconds(3600), 3600)
        self.assertEqual(seconds("45:00"), 2700)
        self.assertEqual(seconds("01:15:30"), 4530)
        with self.assertRaises(ValueError):
            seconds("invalido")
        with self.assertRaises(ValueError):
            seconds("12:65")

    def test_clean_text(self):
        self.assertEqual(clean_text("Texto com \x00 caractere nulo"), "Texto com  caractere nulo")
        self.assertEqual(clean_text("Linha 1\nLinha 2\tTab"), "Linha 1\nLinha 2\tTab")

    def test_episode_date(self):
        self.assertTrue(episode_date({"timestamp": 1700000000}))
        self.assertTrue(episode_date({"upload_date": "20261101"}))

    def test_feed_valido_e_guid_estavel(self):
        root = ET.Element("rss", {"version": "2.0"})
        channel = ET.SubElement(root, "channel")
        update_channel(channel, "https://exemplo.com/podcast.xml", "https://exemplo.com/capa.jpg", "podcast@exemplo.com")
        add_episode(
            channel,
            {"id": "1N7M-oTSOlY", "title": "Culto ao vivo - Os dias de silêncio", "upload_date": "20261101"},
            {}, "https://audio.exemplo.com/episodes/1N7M-oTSOlY.mp3", 12345, 3000,
        )
        xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        ET.fromstring(xml)
        self.assertIn(b"xmlns:itunes=", xml)
        self.assertEqual(xml.count(b"xmlns:itunes="), 1)
        self.assertEqual(existing_guids(channel), {"yt:1N7M-oTSOlY"})

    def test_video_enclosure_e_vodcast(self):
        root = ET.Element("rss", {"version": "2.0"})
        channel = ET.SubElement(root, "channel")
        update_channel(channel, "https://exemplo.com/vodcast.xml", "https://exemplo.com/capa.jpg", "podcast@exemplo.com", title_suffix=" (Vídeo)")
        add_episode(
            channel,
            {"id": "5CyVFrCs5Pk", "title": "Culto ao vivo - Mensagem", "upload_date": "20261108"},
            {}, "https://audio.exemplo.com/episodes/5CyVFrCs5Pk.mp4", 543210, 3200, media_type="video/mp4"
        )
        item = channel.find("item")
        self.assertIsNotNone(item)
        enclosure = item.find("enclosure")
        self.assertIsNotNone(enclosure)
        self.assertEqual(enclosure.get("type"), "video/mp4")
        self.assertEqual(enclosure.get("url"), "https://audio.exemplo.com/episodes/5CyVFrCs5Pk.mp4")
        self.assertIn("Vídeo", channel.findtext("title", ""))

    def test_parse_video_id_variations(self):
        self.assertEqual(parse_video_id("5CyVFrCs5Pk"), "5CyVFrCs5Pk")
        self.assertEqual(parse_video_id("https://www.youtube.com/watch?v=5CyVFrCs5Pk"), "5CyVFrCs5Pk")
        self.assertEqual(parse_video_id("https://youtu.be/5CyVFrCs5Pk?t=10"), "5CyVFrCs5Pk")
        self.assertEqual(parse_video_id("https://www.youtube.com/live/5CyVFrCs5Pk"), "5CyVFrCs5Pk")

    def test_spotify_state_and_login_events(self):
        set_spotify_state("waiting_login", "Aguardando login de teste...")
        state = get_spotify_state()
        self.assertEqual(state["status"], "waiting_login")
        self.assertIn("login", state["message"].lower())

        confirm_login()
        cancel_spotify_upload()
        state_after = get_spotify_state()
        self.assertEqual(state_after["status"], "idle")

    def test_cut_detector_chapters_and_description(self):
        from cut_detector import check_chapters_or_description, format_seconds

        self.assertEqual(format_seconds(3665), "01:01:05")

        # Test chapters detection
        details_with_ch = {
            "chapters": [
                {"title": "Louvor e Adoração", "start_time": 0, "end_time": 2400},
                {"title": "Ministração da Palavra", "start_time": 2400, "end_time": 5400},
                {"title": "Oração Final", "start_time": 5400, "end_time": 6000},
            ]
        }
        res_ch = check_chapters_or_description(details_with_ch)
        self.assertIsNotNone(res_ch)
        self.assertEqual(res_ch["start"], "00:40:00")
        self.assertEqual(res_ch["end"], "01:30:00")
        self.assertEqual(res_ch["source"], "capitulos_youtube")

        # Test description detection
        details_with_desc = {
            "description": "Culto de Domingo\n00:00 Louvor\n00:48:30 Pregação da Palavra\n01:35:00 Oração\n"
        }
        res_desc = check_chapters_or_description(details_with_desc)
        self.assertIsNotNone(res_desc)
        self.assertEqual(res_desc["start"], "00:48:30")
        self.assertEqual(res_desc["end"], "01:35:00")
        self.assertEqual(res_desc["source"], "descricao_youtube")

    def test_normalize_cut_bounds(self):
        from server import normalize_cut_bounds
        self.assertEqual(normalize_cut_bounds(None, None), (None, None))
        self.assertEqual(normalize_cut_bounds("00:00:00", "00:00:00"), (None, None))
        self.assertEqual(normalize_cut_bounds("00:00:00", None), (None, None))
        self.assertEqual(normalize_cut_bounds(None, "00:00:00"), (None, None))
        self.assertEqual(normalize_cut_bounds("01:00:00", "01:00:00"), (None, None))
        self.assertEqual(normalize_cut_bounds("01:30:00", "01:00:00"), (None, None))
        self.assertEqual(normalize_cut_bounds("00:30:00", "01:15:00"), (1800, 4500))

    def test_cancel_manual_export_and_publish(self):
        from server import cancel_manual_export, cancel_publish, get_manual_export_state, CURRENT_TASK
        res_export = cancel_manual_export()
        self.assertTrue(res_export["ok"])
        self.assertEqual(get_manual_export_state()["status"], "idle")

        res_pub = cancel_publish()
        self.assertTrue(res_pub["ok"])
        self.assertEqual(CURRENT_TASK["status"], "idle")

    def test_download_filenames_with_episode_title_base(self):
        from server import sanitize_filename, get_download_filename
        title = "A Graça de Viver: O Poder da Fé / Pastor Paulo Itajahy"
        base = sanitize_filename(title)
        self.assertNotIn(":", base)
        self.assertNotIn("/", base)
        self.assertIn("A Graça de Viver", base)

        # Video
        video_name = get_download_filename("H_tIiMrPt78.mp4", title)
        self.assertTrue(video_name.endswith(" - Video.mp4"))
        self.assertTrue(video_name.startswith(base))

        # Square photo 1:1
        art_name = get_download_filename("H_tIiMrPt78.jpg", title)
        self.assertTrue(art_name.endswith(" - 1x1.jpg"))
        self.assertTrue(art_name.startswith(base))

        # YouTube theme thumbnail 16:9
        thumb_name = get_download_filename("H_tIiMrPt78_thumb.jpg", title)
        self.assertTrue(thumb_name.endswith(" - 16-9.jpg"))
        self.assertTrue(thumb_name.startswith(base))

    def test_feed_video_id_matching_and_replace(self):
        from server import item_video_id, feed_video_ids, remove_existing_episode
        channel = ET.Element("channel")
        a = ET.SubElement(channel, "item"); ET.SubElement(a, "guid").text = "yt:H_tIiMrPt78"
        b = ET.SubElement(channel, "item"); ET.SubElement(b, "guid").text = "3511e6b5-0850-4ae0-864c-a3fb168b5cee"
        ET.SubElement(b, "link").text = "https://podcasters.spotify.com/pod/show/x/episodes/y"
        c = ET.SubElement(channel, "item"); ET.SubElement(c, "guid").text = "abc"
        ET.SubElement(c, "description").text = "Vídeo oficial no YouTube: https://www.youtube.com/watch?v=5CyVFrCs5Pk"
        self.assertEqual(item_video_id(a), "H_tIiMrPt78")
        self.assertIsNone(item_video_id(b))
        self.assertEqual(item_video_id(c), "5CyVFrCs5Pk")
        self.assertEqual(feed_video_ids(channel), {"H_tIiMrPt78", "5CyVFrCs5Pk"})
        self.assertEqual(remove_existing_episode(channel, "H_tIiMrPt78"), 1)
        self.assertEqual(len(channel.findall("item")), 2)

    def test_export_matches_uses_meta_cut_and_quality(self):
        import json, os, tempfile
        from unittest import mock
        import server
        from server import export_matches
        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(server, "is_compatible_mp4", return_value=True) as compat:
            os.chdir(tmp)
            try:
                Path("exports").mkdir()
                Path("exports/AAAAAAAAAAA.mp4").write_bytes(b"x" * 10)
                # Sem meta: só vale para vídeo completo
                self.assertIsNotNone(export_matches("AAAAAAAAAAA", None, None, "720"))
                self.assertIsNone(export_matches("AAAAAAAAAAA", 0, 3600, "720"))
                Path("exports/AAAAAAAAAAA.meta.json").write_text(json.dumps(
                    {"start_sec": 0, "end_sec": 3600, "quality": "720"}))
                self.assertIsNotNone(export_matches("AAAAAAAAAAA", 0, 3600, "720"))
                self.assertIsNone(export_matches("AAAAAAAAAAA", 0, 3600, "1080"))
                self.assertIsNone(export_matches("AAAAAAAAAAA", 60, 3600, "720"))
                self.assertIsNone(export_matches("AAAAAAAAAAA", None, None, "720"))
                # MP4 com vídeo AV1/VP9 nunca é reaproveitado (QuickTime/Spotify mostram só áudio)
                compat.return_value = False
                self.assertIsNone(export_matches("AAAAAAAAAAA", 0, 3600, "720"))
            finally:
                os.chdir(old_cwd)

    def test_encode_mp3_with_artwork_keeps_full_duration(self):
        """Regressão: '-shortest' + capa embutida cortava o MP3 para ~0,04 s no ffmpeg 8."""
        import shutil, subprocess, tempfile
        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg não instalado")
        from processar_podcast import encode_mp3
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src, art, out = tmp / "src.m4a", tmp / "art.jpg", tmp / "out.mp3"
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
                            "-c:a", "aac", str(src)], check=True)
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=64x64",
                            "-frames:v", "1", str(art)], check=True)
            seen = []
            size, duration = encode_mp3(src, out, None, None, artwork_path=art, progress_callback=lambda *a: seen.append(a))
            self.assertGreater(size, 0)
            self.assertGreaterEqual(duration, 7)
            size_cut, dur_cut = encode_mp3(src, out, 2, 6, artwork_path=art)
            self.assertTrue(3 <= dur_cut <= 5)


if __name__ == "__main__":
    unittest.main()

import unittest
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


if __name__ == "__main__":
    unittest.main()

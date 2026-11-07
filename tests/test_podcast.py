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


if __name__ == "__main__":
    unittest.main()

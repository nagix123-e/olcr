import unittest

from olcr_api.translation_languages import (
    LANGUAGE_CODES_BY_NAME,
    _plamo_code_for_natural_language,
    plamo_source_language_tag,
)


class TranslationLanguageDetectionTests(unittest.TestCase):
    def test_auto_source_tags_japanese_instead_of_leaving_language_blank(self):
        self.assertEqual(
            "ja",
            plamo_source_language_tag("auto", "私は長い間街を歩いていた。心は不思議なものだった。"),
        )

    def test_auto_source_identifies_spanish_text(self):
        self.assertEqual(
            "es",
            plamo_source_language_tag(
                "auto",
                "La novela cuenta que el hombre caminaba por la ciudad y pensaba en su vida.",
            ),
        )

    def test_auto_source_uses_unicode_script_for_supported_languages(self):
        self.assertEqual("ko", plamo_source_language_tag("auto", "그는 오래된 도시를 천천히 걸으며 생각했다."))
        self.assertEqual("th", plamo_source_language_tag("auto", "เขาเดินไปตามถนนและคิดถึงเรื่องราวในชีวิต"))

    def test_explicit_source_language_takes_precedence_over_detection(self):
        self.assertEqual("fr", plamo_source_language_tag("fr", "私は長い間街を歩いていた。"))

    def test_every_configured_plamo_language_code_is_accepted(self):
        for code in set(LANGUAGE_CODES_BY_NAME.values()):
            with self.subTest(code=code):
                self.assertEqual(code, plamo_source_language_tag(code, ""))

    def test_natural_language_tags_map_to_plamo_codes(self):
        self.assertEqual("zh-hans", _plamo_code_for_natural_language("zh-Hans"))
        self.assertEqual("zh-hant", _plamo_code_for_natural_language("zh-Hant"))
        self.assertEqual("tl", _plamo_code_for_natural_language("fil"))
        self.assertEqual("sr", _plamo_code_for_natural_language("sr-Latn"))

    def test_ambiguous_empty_source_uses_plamo_auto_direction(self):
        self.assertEqual("English|Japanese", plamo_source_language_tag("auto", ""))


if __name__ == "__main__":
    unittest.main()

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from src.slug import slugify


class SlugifyTests(unittest.TestCase):
    def test_basic_casing_and_spaces(self):
        self.assertEqual(slugify("Hello World"), "hello-world")
        self.assertEqual(slugify("HELLO WORLD"), "hello-world")
        self.assertEqual(slugify("hello world"), "hello-world")

    def test_strips_punctuation(self):
        self.assertEqual(slugify("Hello, World!"), "hello-world")
        self.assertEqual(slugify("What?! How... Why?!"), "what-how-why")
        self.assertEqual(slugify("Rock & Roll"), "rock-roll")
        self.assertEqual(slugify("Tags: #python, #coding!"), "tags-python-coding")
        self.assertEqual(
            slugify("Punctuation test: [one] {two} (three) <four>"),
            "punctuation-test-one-two-three-four",
        )
        self.assertEqual(
            slugify('Quotes "double" and \'single\''),
            "quotes-double-and-single",
        )

    def test_whitespace_handling(self):
        self.assertEqual(
            slugify("   leading and trailing whitespace   "),
            "leading-and-trailing-whitespace",
        )
        self.assertEqual(
            slugify("multiple    spaces   between   words"),
            "multiple-spaces-between-words",
        )
        self.assertEqual(
            slugify("tabs\tand\nnewlines\r\nhere"),
            "tabs-and-newlines-here",
        )

    def test_underscores_and_hyphens(self):
        self.assertEqual(slugify("snake_case_string"), "snake-case-string")
        self.assertEqual(slugify("already-kebab-case"), "already-kebab-case")
        self.assertEqual(slugify("mixed_snake-and_kebab"), "mixed-snake-and-kebab")
        self.assertEqual(
            slugify("multiple---hyphens___and---underscores"),
            "multiple-hyphens-and-underscores",
        )
        self.assertEqual(slugify("---leading-and-trailing---"), "leading-and-trailing")
        self.assertEqual(slugify("___leading_and_trailing___"), "leading-and-trailing")

    def test_numbers_and_alphanumeric(self):
        self.assertEqual(slugify("Release 2.0.1 candidate"), "release-201-candidate")
        self.assertEqual(slugify("Item 123 version 456"), "item-123-version-456")
        self.assertEqual(slugify("v1_2_3"), "v1-2-3")

    def test_accented_and_unicode_characters(self):
        self.assertEqual(slugify("Café Münster"), "cafe-munster")
        self.assertEqual(slugify("Crème brûlée"), "creme-brulee")
        self.assertEqual(slugify("Façade & naïve"), "facade-naive")
        self.assertEqual(slugify("em—dash and en–dash"), "em-dash-and-en-dash")

    def test_empty_and_special_character_only_inputs(self):
        self.assertEqual(slugify(""), "")
        self.assertEqual(slugify("   "), "")
        self.assertEqual(slugify("!!!"), "")
        self.assertEqual(slugify("---"), "")
        self.assertEqual(slugify("___"), "")
        self.assertEqual(slugify("!@#$%^&*()_+-=[]{}|;':\",./<>?"), "")

    def test_type_error_on_non_string_input(self):
        with self.assertRaises(TypeError):
            slugify(None)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            slugify(123)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            slugify(["hello"])  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()

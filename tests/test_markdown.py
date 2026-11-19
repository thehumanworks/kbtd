import unittest

from kbtd.markdown import parse_markdown


class MarkdownTests(unittest.TestCase):
    def test_scope_and_lossless_round_trip(self):
        text = (
            "preamble\r\n## Not a column\r\n- [x] Not a card\n"
            "# First\n- [ ] Also ignored\n## Queue\n\n"
            "- [ ]  Keep spacing  \n    - [x] subtask\n\n\tnote\n\n"
            "loose prose\n- [x] complete\n"
            "````md\n# Example\n```\n## Fake\n- [ ] Fake\n````\n"
            "# Another\n## Empty"
        )
        doc = parse_markdown(text)
        self.assertEqual(doc.serialize(), text)
        self.assertEqual([s.name for s in doc.sections], ["First", "Another"])
        self.assertEqual([c.name for c in doc.sections[0].columns], ["Queue"])
        a, b = doc.sections[0].columns[0].cards
        self.assertEqual((a.text, a.done), (" Keep spacing  ", False))
        self.assertEqual(doc.lines[a.start + 1:a.end], ("    - [x] subtask\n", "\n", "\tnote\n"))
        self.assertEqual((b.text, b.done), ("complete", True))
        self.assertEqual(doc.sections[1].columns[0].cards, ())

    def test_toggle_and_edit_touch_only_the_task_line(self):
        text = "\ufeff# A\r\n## B\n\n- [ ] A  \r\n  notes\nother"
        doc = parse_markdown(text)
        card = doc.sections[0].columns[0].cards[0]
        self.assertEqual(doc.toggle(card).serialize(), text.replace("[ ]", "[x]"))
        self.assertEqual(doc.edit(card, "changed").serialize(), text.replace("] A  ", "] changed"))
        self.assertEqual(doc.serialize(), text)

    def test_move_preserves_notes_prose_crlf_and_unterminated_eof(self):
        prefix = "intro\r\n# First\r\n## A\r\n\r\n"
        block = "- [ ] First   \r\n    - [x] nested\r\n\r\n\tmore"
        middle = "\r\nloose prose\r\n## B\r\n\r\n"
        last = "- [x] existing"
        doc = parse_markdown(prefix + block + "\r\n" + middle + last)
        a, b = doc.sections[0].columns
        moved = doc.move(a.cards[0], b)
        self.assertEqual(moved.serialize(), prefix + middle + last + "\r\n" + block)
        a, b = moved.sections[0].columns
        self.assertEqual(a.cards, ())
        self.assertEqual([c.text for c in b.cards], ["existing", "First   "])
        back = moved.move(b.cards[-1], a)
        self.assertEqual(back.sections[0].columns[0].cards[0].text, "First   ")
        self.assertIn(block + "\r\n## B", back.serialize())
        self.assertTrue(back.serialize().endswith(last))

    def test_add_to_empty_column_and_delete_only_the_selected_card(self):
        doc = parse_markdown("# S\n## C")
        doc = doc.add(doc.sections[0].columns[0], "one")
        self.assertEqual(doc.serialize(), "# S\n## C\n- [ ] one")
        self.assertEqual(doc.delete(doc.sections[0].columns[0].cards[0]).serialize(), "# S\n## C")
        text = "# S\n## C\n- [x] same\n- [ ] same\n  note\nend\n"
        doc = parse_markdown(text)
        col = doc.sections[0].columns[0]
        self.assertEqual(doc.delete(col.cards[1]).serialize(), "# S\n## C\n- [x] same\nend\n")
        self.assertIs(doc.move(col.cards[0], col), doc)

    def test_unfinished_fence_does_not_swallow_new_card(self):
        doc = parse_markdown("# S\n## C\n~~~md\n- [ ] example\n")
        changed = doc.add(doc.sections[0].columns[0], "real")
        self.assertEqual([c.text for c in changed.sections[0].columns[0].cards], ["real"])

    def test_empty_and_invalid_text(self):
        for text in ("", "## C\n- [ ] ignored\n"):
            self.assertEqual(parse_markdown(text).sections, ())
        doc = parse_markdown("# S\n## C\n- [ ] card\n")
        col = doc.sections[0].columns[0]
        for text in ("", "  ", "two\nlines", "other\rline"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    doc.add(col, text)
                with self.assertRaises(ValueError):
                    doc.edit(col.cards[0], text)

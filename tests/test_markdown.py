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

    def test_child_ranges_preserve_parent_notes_fences_and_declined_tasks(self):
        head = "\ufeff# TODO\r\n## A\r\n- [ ] Root\r\n"
        leaf = "    - [ ] leaf\r\n      leaf note  \r\n"
        child = "  - [ ] one\r\n    child note\r\n" + leaf
        rest = ("\r\n  parent's note\r\n   ~~~md\r\n  - [ ] example, not a child\r\n"
                "   ~~~\r\n  - [-] declined\r\n    - [ ] ignored\r\n  - [ ] two\r\n")
        tail = "\r\nloose prose\r\n## B\r\n- [ ] other"
        doc = parse_markdown(head + child + rest + tail)
        a, b = doc.sections[0].columns
        self.assertEqual([(path, c.text) for path, c in a.rows],
                         [("1", "Root"), ("1.1", "one"), ("1.1.1", "leaf"), ("1.2", "two")])
        self.assertEqual(doc.delete(a.resolve("1.1")).serialize(), head + rest + tail)
        self.assertEqual(doc.delete(a.resolve("1.1.1")).serialize(), doc.serialize().replace(leaf, ""))
        self.assertEqual(doc.edit(a.resolve("1.2"), "three").serialize(),
                         doc.serialize().replace("] two", "] three"))
        moved = doc.move(a.resolve("1"), b)
        self.assertEqual(moved.serialize(), head[:head.index("- [ ]")] + tail + "\r\n"
                         + head[head.index("- [ ]"):] + child + rest.rstrip("\r\n"))
        with self.assertRaisesRegex(ValueError, "top-level"):
            doc.move(a.resolve("1.1"), b)

    def test_unclosed_fenced_note_cannot_swallow_new_child(self):
        doc = parse_markdown("# TODO\n## B\n- [ ] Parent\n  ```md\n  - [ ] example\n")
        parent = doc.sections[0].columns[0].resolve("1")
        self.assertEqual(parent.children, ())
        with self.assertRaisesRegex(ValueError, "fenced note"):
            doc.add_child(parent, "not really a task")

    def test_description_header_is_scoped_to_one_task(self):
        header = "    Description: first\r\n    Description:\n    Description: last\r\n"
        text = ("# S\r\n## C\r\n- [ ] Root\r\n  Description: root\r\n  - [ ] Child\r\n"
                + header + "    legacy note\r\n    Description: quoted in prose\r\n"
                "    - [ ] Grandchild\r\n      Description: keep me")
        doc = parse_markdown(text)
        parent, child, leaf = (card for _, card in doc.sections[0].columns[0].rows)
        self.assertEqual((parent.description, child.description, leaf.description),
                         ("root", "first\n\nlast", "keep me"))
        self.assertIs(doc.set_description(child, child.description), doc)
        self.assertEqual(doc.set_description(child, "new\r\nline").serialize(),
                         text.replace(header, "    Description: new\r\n    Description: line\n"))
        self.assertEqual(doc.set_description(child, None).serialize(), text.replace(header, ""))
        self.assertEqual(doc.set_description(leaf, "  end ").serialize(),
                         text.replace("Description: keep me", "Description:   end "))

    def test_due_uses_one_suffix_tag_and_preserves_legacy_dates_in_prose(self):
        text = "# S\r\n## C\r\n- [ ] before #due-2027-01-01 is prose @alice\t#due-2028-02-29 #ops "
        doc = parse_markdown(text)
        card = doc.sections[0].columns[0].resolve("1")
        self.assertEqual(card.due, "2028-02-29")
        self.assertIs(doc.set_due(card, card.due), doc)
        self.assertEqual(doc.set_due(card, "2028-03-01").serialize(), text.replace("02-29", "03-01"))
        self.assertEqual(doc.set_due(card, None).serialize(), text.replace("\t#due-2028-02-29", ""))

    def test_metadata_edits_touch_only_one_suffix_token(self):
        line = "- [x] Ask @jo about a@b.co and https://docs/#blocked #keep\t@ada  #ops  "
        doc = parse_markdown("# TODO\r\n## Here\r\n" + line + "\r\n  note @ada #blocked")
        task = doc.sections[0].columns[0].resolve("1")
        self.assertFalse(task.blocked)
        self.assertEqual(doc.set_owner(task, "@bob").serialize(),
                         doc.serialize().replace("#keep\t@ada", "#keep\t@bob"))
        self.assertEqual(doc.set_owner(task, None).serialize(),
                         doc.serialize().replace("\t@ada", ""))
        self.assertIs(doc.set_blocked(task, False), doc)
        flagged = doc.set_blocked(task, True)
        task = flagged.sections[0].columns[0].resolve("1")
        self.assertTrue(task.blocked)
        self.assertTrue(task.done)
        self.assertEqual(flagged.serialize(), doc.serialize().replace("#ops  ", "#ops #blocked  "))
        self.assertIs(flagged.set_blocked(task, True), flagged)
        self.assertEqual(flagged.set_blocked(task, False).serialize(), doc.serialize())

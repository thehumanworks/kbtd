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

    def test_title_notes_and_descriptions_given_when_adding(self):
        text = ("<!-- kbtd next-id: KBT-0002 -->\r\n# T\r\n## A\r\n"
                "- [ ] KBT-0001 Fix login @bob\t#docs\r\n  Description: Users see a 500\r\n  first note\r\n"
                "  - [ ] Child\r\n    child note\r\n\r\n  last note\r\n## B")
        doc = parse_markdown(text)
        card = doc.sections[0].columns[0].cards[0]
        self.assertEqual((card.plain_title, doc.notes(card)), ("Fix login", ("first note", "", "last note")))
        self.assertIs(doc.card_at(card.children[0].start), card.children[0])
        # The ID, the separator before the tokens and the tokens stay.
        self.assertEqual(doc.set_title(card, "  Repair login ").serialize(),
                         text.replace("Fix login @bob", "Repair login @bob"))
        with self.assertRaisesRegex(ValueError, "needs a title"):
            doc.set_title(card, " ")
        bare = parse_markdown("# T\n## A\n- [ ] @bob #x\n- [ ] Plain\n")
        tokens, plain = bare.sections[0].columns[0].cards
        self.assertEqual(tokens.plain_title, "")
        self.assertEqual(bare.set_title(tokens, "Named").sections[0].columns[0].cards[0].text, "Named @bob #x")
        self.assertEqual(bare.set_title(plain, "Renamed").sections[0].columns[0].cards[1].text, "Renamed")

        added = doc.add(doc.sections[0].columns[1], "New", "KBT", "one\ntwo")
        self.assertTrue(added.serialize().endswith("## B\r\n- [ ] KBT-0002 New\r\n"
                                                   "  Description: one\r\n  Description: two"))
        parent = added.sections[0].columns[0].cards[0]
        child = added.add_child(parent, "Kid", "why").sections[0].columns[0].cards[0].children[-1]
        self.assertEqual((child.text, child.description), ("Kid", "why"))
        fresh = parse_markdown("# T\n## A\n")
        first = fresh.add(fresh.sections[0].columns[0], "First", "ABC", "said")
        self.assertEqual(first.serialize(), "<!-- kbtd next-id: ABC-0002 -->\n# T\n## A\n"
                                            "- [ ] ABC-0001 First\n  Description: said\n")
        self.assertEqual(fresh.add(fresh.sections[0].columns[0], "x", "", None).serialize(), "# T\n## A\n- [ ] x\n")

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


class CardIdTests(unittest.TestCase):
    def test_add_numbers_cards_and_keeps_the_counter_on_line_one(self):
        doc = parse_markdown("﻿# Tasks\r\n\r\n## Backlog\r\n\r\n## Done\r\n")
        backlog = lambda d: d.sections[0].columns[0]
        doc = doc.add(backlog(doc), "Fix login", "KBT")
        doc = doc.add(backlog(doc), "Fix signup @bob", "ZZZ")  # the counter's prefix wins
        self.assertEqual(doc.serialize(), "﻿<!-- kbtd next-id: KBT-0003 -->\r\n# Tasks\r\n\r\n## Backlog\r\n\r\n"
                                          "- [ ] KBT-0001 Fix login\r\n- [ ] KBT-0002 Fix signup @bob\r\n## Done\r\n")
        card = backlog(doc).cards[1]
        self.assertEqual((card.id, card.title, card.field("owner")[0]), ("KBT-0002", "Fix signup @bob", "@bob"))
        # A deleted card's number is not reused.
        doc = doc.delete(card)
        doc = doc.add(backlog(doc), "Fix logout", "KBT")
        self.assertEqual(backlog(doc).cards[1].text, "KBT-0003 Fix logout")
        self.assertIn("next-id: KBT-0004 -->", doc.lines[0])
        self.assertEqual(parse_markdown(doc.serialize()), doc)
        # Subtasks get no ID, and a document without a prefix numbers nothing.
        child = doc.add_child(backlog(doc).cards[0], "step")
        self.assertEqual(backlog(child).cards[0].children[0].text, "step")
        plain = parse_markdown("# T\n## B\n").add(parse_markdown("# T\n## B\n").sections[0].columns[0], "x")
        self.assertEqual(plain.serialize(), "# T\n## B\n- [ ] x\n")
        self.assertRaises(ValueError, parse_markdown("# T\n## B\n").add,
                          parse_markdown("# T\n## B\n").sections[0].columns[0], "x", "kbt")

    def test_only_the_boards_prefix_is_an_id(self):
        text = ("<!-- kbtd next-id: KBT-0002 -->\n# T\n## B\n- [ ] CVE-2024 patch\n- [ ] KBT-0001 Real\n"
                "- [ ] KBT-12 short\n- [ ] KBT-0001x glued\n```\n<!-- kbtd next-id: ABC-0001 -->\n```\n")
        doc = parse_markdown(text)
        self.assertEqual([c.id for c in doc.sections[0].columns[0].cards], ["", "KBT-0001", "", ""])
        self.assertEqual((doc.id_prefix, len(doc.counters), doc.id_warnings), ("KBT", 1, ()))
        # Without a counter line no title counts as an ID.
        self.assertEqual(parse_markdown("# T\n## B\n- [ ] KBT-0001 Real\n").sections[0].columns[0].cards[0].id, "")

    def test_next_number_never_repeats_an_id_on_the_board(self):
        text = "<!-- kbtd next-id: KBT-0002 -->\n# T\n## B\n- [ ] KBT-0009 hand-numbered\n  - [ ] KBT-0011 sub\n"
        doc = parse_markdown(text)
        doc = doc.add(doc.sections[0].columns[0], "next", "KBT")
        self.assertEqual(doc.sections[0].columns[0].cards[-1].text, "KBT-0012 next")
        doc = doc.add(doc.sections[0].columns[0], "KBT-0040 typed", "KBT")  # a typed ID is kept
        self.assertEqual(doc.sections[0].columns[0].cards[-1].text, "KBT-0040 typed")
        self.assertIn("KBT-0041", doc.lines[0])

    def test_bad_counter_and_duplicates_are_reported_and_repaired_on_add(self):
        doc = parse_markdown("<!-- kbtd next-id: oops -->\n# T\n## B\n- [ ] KBT-0007 a\n- [ ] KBT-0007 b\n")
        self.assertIn("malformed ID counter on line 1", doc.id_warnings[0])
        doc = doc.add(doc.sections[0].columns[0], "c", "KBT")
        self.assertEqual(doc.lines[0], "<!-- kbtd next-id: KBT-0009 -->\n")
        self.assertEqual(doc.sections[0].columns[0].cards[-1].text, "KBT-0008 c")
        self.assertEqual(doc.id_warnings, ("duplicate card IDs: KBT-0007 (2 cards); "
                                           "give one a free ID such as KBT-0009 with 'kbtd edit'",))
        self.assertEqual(parse_markdown("<!-- kbtd next-id: KBT-0001 -->  \n# T\n").id_warnings, ())
        twice = parse_markdown("<!-- kbtd next-id: KBT-0001 -->\n<!-- kbtd next-id: ABC-0001 -->\n# T\n")
        self.assertEqual(twice.id_warnings, ("more than one ID counter line; using line 1",))

    def test_edits_keep_the_id(self):
        doc = parse_markdown("<!-- kbtd next-id: KBT-0003 -->\n# T\n## B\n- [ ] KBT-0001 Old @ada\n")
        card = doc.sections[0].columns[0].cards[0]
        self.assertEqual(doc.edit(card, "New @bob").lines[3], "- [ ] KBT-0001 New @bob\n")
        self.assertEqual(doc.edit(card, "KBT-0002 Renumbered").lines[3], "- [ ] KBT-0002 Renumbered\n")
        self.assertEqual(doc.edit(card, "CVE-2024 patch").lines[3], "- [ ] KBT-0001 CVE-2024 patch\n")
        self.assertEqual(doc.set_owner(card, None).lines[3], "- [ ] KBT-0001 Old\n")
        self.assertEqual(doc.set_due(card, "2027-01-02").lines[3], "- [ ] KBT-0001 Old @ada #due-2027-01-02\n")
        self.assertEqual(doc.toggle(card).lines[3], "- [x] KBT-0001 Old @ada\n")

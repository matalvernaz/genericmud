"""Deterministic policy tests; native editing is covered separately."""

from genericmud.ui.command_document import CommandDocument


def enter(doc, row):
    command = doc.text.split("\n")[row]
    doc.complete(row)
    return command


def type_tail(doc, text):
    doc.sync(doc.text + text, edited=True)


def test_draft_stays_between_later_commands_and_old_execution_preserves_it():
    doc = CommandDocument()
    type_tail(doc, "north")
    enter(doc, 0)
    type_tail(doc, "look target\nsouth")
    enter(doc, 2)
    type_tail(doc, "east")
    enter(doc, 3)
    assert doc.text == "north\nlook target\nsouth\neast\n"
    doc.sync(doc.text.replace("look target", "look dragon"), edited=True)
    assert enter(doc, 1) == "look dragon"
    assert doc.text == "north\nlook dragon\nsouth\neast\nlook dragon\n"


def test_newest_recall_still_executes_without_an_adjacent_copy():
    doc = CommandDocument()
    type_tail(doc, "north")
    assert enter(doc, 0) == "north"
    assert enter(doc, 0) == "north"
    assert doc.text == "north\n"


def test_manual_repetitions_retain_both_entries():
    doc = CommandDocument()
    for row in (0, 1):
        type_tail(doc, "north")
        enter(doc, row)
    assert doc.text == "north\nnorth\n"
    enter(doc, 1)
    assert doc.text == "north\nnorth\n"


def test_unsent_tail_matching_recalled_command_is_not_deduplicated():
    doc = CommandDocument()
    type_tail(doc, "north")
    enter(doc, 0)
    type_tail(doc, "north")
    enter(doc, 0)
    assert doc.text == "north\nnorth\nnorth\n"


def test_manually_edited_old_line_is_not_mistaken_for_recall():
    doc = CommandDocument()
    for row, text in enumerate(("north", "south")):
        type_tail(doc, text)
        enter(doc, row)
    doc.sync("south\nsouth\n", edited=True)
    enter(doc, 0)
    assert doc.text == "south\nsouth\nsouth\n"


def test_identical_text_replacement_invalidates_origin():
    doc = CommandDocument()
    type_tail(doc, "north")
    enter(doc, 0)
    doc.sync(doc.text, edited=True)
    enter(doc, 0)
    assert doc.text == "north\nnorth\n"


def test_empty_enter_leaves_document_for_engine_autoretype():
    doc = CommandDocument()
    assert enter(doc, 0) == ""
    assert doc.text == ""
    type_tail(doc, "north")
    enter(doc, 0)
    assert enter(doc, 1) == ""
    assert doc.text == "north\n"


def test_deleting_and_undoing_lines_cannot_revive_guessed_origin():
    doc = CommandDocument()
    type_tail(doc, "north\nsouth")
    enter(doc, 1)
    doc.sync("north\n", edited=True)
    doc.sync("north\nsouth\n", edited=True)
    enter(doc, 1)
    assert doc.text == "north\nsouth\nsouth\n"


def test_native_edit_of_identical_multiline_text_forgets_all_origin():
    doc = CommandDocument()
    type_tail(doc, "draft\nnorth")
    enter(doc, 1)
    doc.sync(doc.text, edited=True)
    enter(doc, 1)
    assert doc.text == "draft\nnorth\nnorth\n"


def test_edit_elsewhere_conservatively_permits_an_extra_recalled_copy():
    doc = CommandDocument()
    type_tail(doc, "draft\nnorth")
    enter(doc, 1)
    doc.sync("changed draft\nnorth\n", edited=True)
    enter(doc, 1)
    assert doc.text == "changed draft\nnorth\nnorth\n"

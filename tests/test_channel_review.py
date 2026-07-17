"""Channel browsing: cycling between channels and scrolling within one."""

from __future__ import annotations

from genericmud.model.buffer import Buffer, Line
from genericmud.review.channels import ChannelReview


def _buffer(*entries: tuple[str, str]) -> Buffer:
    buffer = Buffer()
    for channel, text in entries:
        buffer.append(Line(text, channel=channel))
    return buffer


def test_cycles_channels_alphabetically_and_reads_the_newest_line():
    review = ChannelReview(_buffer(
        ("chat", "hi all"), ("tell", "psst"), ("chat", "anyone around?"),
    ))
    assert review.next_channel() == "chat: anyone around?"
    assert review.next_channel() == "tell: psst"
    assert review.next_channel() == "chat: anyone around?"  # wraps
    assert review.prev_channel() == "tell: psst"


def test_main_system_and_review_channels_are_not_browsable():
    review = ChannelReview(_buffer(("main", "a room"), ("system", "logging on")))
    assert review.channels() == []
    assert review.next_channel() == "no channels"


def test_policy_declared_channels_are_listed_even_before_any_line():
    review = ChannelReview(_buffer(), known=lambda: ["combat"])
    assert review.channels() == ["combat"]
    assert review.next_channel() == "combat: no messages"


def test_scrolling_within_a_channel_clamps_at_both_ends():
    review = ChannelReview(_buffer(
        ("chat", "one"), ("chat", "two"), ("chat", "three"),
    ))
    review.next_channel()  # lands on newest ("three")
    assert review.older() == "two"
    assert review.older() == "one"
    assert review.older() == "one"  # clamped at the oldest
    assert review.newer() == "two"
    assert review.newer() == "three"
    assert review.newer() == "three"  # clamped at the newest


def test_scrolling_with_no_channel_selected_enters_the_first():
    review = ChannelReview(_buffer(("chat", "hello")))
    assert review.older() == "chat: hello"


def test_recent_recall_is_one_based_from_the_newest():
    review = ChannelReview(_buffer(("chat", "one"), ("chat", "two")))
    review.next_channel()
    assert review.recent(1) == "two"
    assert review.recent(2) == "one"
    assert review.recent(3) == "no message"


def test_word_navigation_within_the_current_channel_line():
    review = ChannelReview(_buffer(("tell", "Bob tells you hello")))
    review.next_channel()
    assert review.next_word() == "tells"
    assert review.next_word() == "you"
    assert review.prev_word() == "tells"


def test_switching_channels_resets_the_scroll_position():
    review = ChannelReview(_buffer(
        ("chat", "old chat"), ("chat", "new chat"), ("tell", "a tell"),
    ))
    review.next_channel()  # chat
    review.older()  # at "old chat"
    review.next_channel()  # tell
    assert review.prev_channel() == "chat: new chat"  # back on chat, at the newest again

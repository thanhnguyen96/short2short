from __future__ import annotations

from pathlib import Path

import pytest

from short2short.core.models import Scene, WordCue
from short2short.core.subtitle import build_srt, format_srt_time, write_srt


def test_format_srt_time_exact() -> None:
    assert format_srt_time(0.0) == "00:00:00,000"
    assert format_srt_time(1.0) == "00:00:01,000"
    assert format_srt_time(1.234) == "00:00:01,234"
    assert format_srt_time(1.2344) == "00:00:01,234"
    assert format_srt_time(1.2346) == "00:00:01,235"
    assert format_srt_time(3661.5) == "01:01:01,500"
    assert format_srt_time(-5.0) == "00:00:00,000"


def test_format_srt_time_rounding_carries() -> None:
    # 59.9996s rounds to 60.000s -> carry to minute
    assert format_srt_time(59.9996) == "00:01:00,000"
    # 3599.9996s rounds to 3600.000s -> carry to hour
    assert format_srt_time(3599.9996) == "01:00:00,000"
    # 59.9999s
    assert format_srt_time(59.9999) == "00:01:00,000"


def test_build_srt_empty() -> None:
    assert build_srt([]) == ""


def test_build_srt_no_cues_golden() -> None:
    scene = Scene(
        index=0,
        narration="Welcome to short2short.",
        visual_prompt="Logo animation",
        duration_seconds=3.25,
    )
    expected = "1\n00:00:00,000 --> 00:00:03,250\nWelcome to short2short.\n"
    assert build_srt([scene]) == expected


def test_build_srt_multiple_scenes_cumulative_offset_golden() -> None:
    scene0 = Scene(
        index=0,
        narration="First scene narration.",
        visual_prompt="Sunrise",
        duration_seconds=2.5,
    )
    scene1 = Scene(
        index=1,
        narration="Second scene narration.",
        visual_prompt="Noon",
        duration_seconds=3.0,
    )
    scene2 = Scene(
        index=2,
        narration="Third scene narration.",
        visual_prompt="Sunset",
        duration_seconds=1.5,
    )
    expected = (
        "1\n"
        "00:00:00,000 --> 00:00:02,500\n"
        "First scene narration.\n"
        "\n"
        "2\n"
        "00:00:02,500 --> 00:00:05,500\n"
        "Second scene narration.\n"
        "\n"
        "3\n"
        "00:00:05,500 --> 00:00:07,000\n"
        "Third scene narration.\n"
    )
    assert build_srt([scene0, scene1, scene2]) == expected


def test_build_srt_punctuation_split_golden() -> None:
    cues = [
        WordCue(text="Xin", start=0.0, end=0.3),
        WordCue(text="chào", start=0.3, end=0.6),
        WordCue(text="bạn!", start=0.6, end=1.0),
        WordCue(text="Hôm", start=1.2, end=1.5),
        WordCue(text="nay", start=1.5, end=1.8),
        WordCue(text="thế", start=1.8, end=2.0),
        WordCue(text="nào?", start=2.0, end=2.4),
        WordCue(text="Tốt…", start=2.6, end=3.0),
    ]
    scene = Scene(
        index=0,
        narration="Xin chào bạn! Hôm nay thế nào? Tốt…",
        visual_prompt="Talking person",
        duration_seconds=3.5,
        word_cues=cues,
    )
    expected = (
        "1\n"
        "00:00:00,000 --> 00:00:01,000\n"
        "Xin chào bạn!\n"
        "\n"
        "2\n"
        "00:00:01,200 --> 00:00:02,400\n"
        "Hôm nay thế nào?\n"
        "\n"
        "3\n"
        "00:00:02,600 --> 00:00:03,000\n"
        "Tốt…\n"
    )
    assert build_srt([scene]) == expected


def test_build_srt_cjk_punctuation_split_golden() -> None:
    cues = [
        WordCue(text="你好", start=0.0, end=0.4),
        WordCue(text="世界。", start=0.4, end=0.8),
        WordCue(text="早上", start=1.0, end=1.4),
        WordCue(text="好！", start=1.4, end=1.8),  # noqa: RUF001
        WordCue(text="吃饭了", start=2.0, end=2.4),
        WordCue(text="吗？", start=2.4, end=2.8),  # noqa: RUF001
    ]
    scene = Scene(
        index=0,
        narration="你好 世界。 早上 好！ 吃饭了 吗？",  # noqa: RUF001
        visual_prompt="City street",
        duration_seconds=3.0,
        word_cues=cues,
    )
    expected = (
        "1\n"
        "00:00:00,000 --> 00:00:00,800\n"
        "你好 世界。\n"
        "\n"
        "2\n"
        "00:00:01,000 --> 00:00:01,800\n"
        "早上 好！\n"  # noqa: RUF001
        "\n"
        "3\n"
        "00:00:02,000 --> 00:00:02,800\n"
        "吃饭了 吗？\n"  # noqa: RUF001
    )
    assert build_srt([scene]) == expected


def test_build_srt_max_chars_split_golden() -> None:
    # max_chars=15
    # "The quick brown" is 15 chars (<= 15)
    # Next word "fox" makes it 19 chars (> 15) -> emits group
    # Next group: "jumps over the lazy" is 19 chars (> 15) -> emits group
    # Next group: "dog" emitted at end of cues
    cues = [
        WordCue(text="The", start=0.0, end=0.4),
        WordCue(text="quick", start=0.4, end=0.8),
        WordCue(text="brown", start=0.8, end=1.2),
        WordCue(text="fox", start=1.2, end=1.6),
        WordCue(text="jumps", start=1.8, end=2.2),
        WordCue(text="over", start=2.2, end=2.6),
        WordCue(text="the", start=2.6, end=2.9),
        WordCue(text="lazy", start=2.9, end=3.2),
        WordCue(text="dog", start=3.4, end=3.8),
    ]
    scene = Scene(
        index=0,
        narration="The quick brown fox jumps over the lazy dog",
        visual_prompt="Running animal",
        duration_seconds=4.0,
        word_cues=cues,
    )
    expected = (
        "1\n"
        "00:00:00,000 --> 00:00:01,600\n"
        "The quick brown fox\n"
        "\n"
        "2\n"
        "00:00:01,800 --> 00:00:03,200\n"
        "jumps over the lazy\n"
        "\n"
        "3\n"
        "00:00:03,400 --> 00:00:03,800\n"
        "dog\n"
    )
    assert build_srt([scene], max_chars=15) == expected


def test_build_srt_mixed_scenes_with_and_without_cues() -> None:
    scene0 = Scene(
        index=0,
        narration="Scene with cues.",
        visual_prompt="Visual 0",
        duration_seconds=2.0,
        word_cues=[
            WordCue(text="First", start=0.0, end=0.8),
            WordCue(text="part.", start=0.8, end=1.6),
        ],
    )
    scene1 = Scene(
        index=1,
        narration="Scene without cues.",
        visual_prompt="Visual 1",
        duration_seconds=2.5,
        word_cues=[],
    )
    expected = (
        "1\n"
        "00:00:00,000 --> 00:00:01,600\n"
        "First part.\n"
        "\n"
        "2\n"
        "00:00:02,000 --> 00:00:04,500\n"
        "Scene without cues.\n"
    )
    assert build_srt([scene0, scene1]) == expected


def test_build_srt_fallback_duration_from_cues() -> None:
    scene = Scene(
        index=0,
        narration="No explicit duration.",
        visual_prompt="Visual",
        duration_seconds=None,
        word_cues=[
            WordCue(text="Auto", start=0.0, end=1.0),
            WordCue(text="duration.", start=1.0, end=2.2),
        ],
    )
    expected = "1\n00:00:00,000 --> 00:00:02,200\nAuto duration.\n"
    assert build_srt([scene]) == expected


def test_write_srt_utf8_no_bom_and_lf(tmp_path: Path) -> None:
    scene = Scene(
        index=0,
        narration="Tiếng Việt có dấu: Cà phê sữa đá.",
        visual_prompt="Coffee cup",
        duration_seconds=2.5,
    )
    out_path = tmp_path / "subtitles" / "vietnamese.srt"
    result = write_srt([scene], out_path)
    assert result == out_path
    assert out_path.exists()

    raw_bytes = out_path.read_bytes()
    # UTF-8 with NO BOM
    assert not raw_bytes.startswith(b"\xef\xbb\xbf")
    # LF newlines only, no CRLF (\r\n)
    assert b"\r\n" not in raw_bytes
    assert b"\n" in raw_bytes

    text = raw_bytes.decode("utf-8")
    assert text == build_srt([scene])
    assert "Tiếng Việt có dấu: Cà phê sữa đá." in text


def test_write_srt_argument_order_swap(tmp_path: Path) -> None:
    scene = Scene(
        index=0,
        narration="Argument swap test.",
        visual_prompt="Test",
        duration_seconds=1.0,
    )
    out_path = tmp_path / "swap.srt"
    result = write_srt(out_path, [scene])
    assert result == out_path
    assert out_path.exists()
    assert "Argument swap test." in out_path.read_text(encoding="utf-8")


def test_write_srt_invalid_arguments() -> None:
    with pytest.raises(TypeError):
        write_srt("invalid", "invalid")  # type: ignore[call-overload]

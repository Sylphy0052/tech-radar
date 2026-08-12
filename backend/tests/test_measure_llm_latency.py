"""本文長ごとの LLM 応答時間の計測（`techradar.measure.llm_latency`）のテスト（Issue #73）。

`MAX_ANALYSIS_BODY_CHARACTERS` を確定するには、本文をどこまで渡すと応答時間がどれだけ
伸びるかを知る必要がある。ここでは計測の骨組み（切り出し・集計・失敗の扱い）を固定する。
実際の応答時間はサブスク枠の混み具合に左右されるため、値そのものはテストで固定しない。
"""

from __future__ import annotations

import pytest

from techradar.llm.errors import LLMError, LLMManagedPolicyDetectedError, LLMToolUseDetectedError
from techradar.llm.fake import FakeLLMProvider
from techradar.measure.llm_latency import (
    LatencySample,
    measure_latency,
    summarize_latencies,
    take_prefixes,
)

_RESPONSE = (
    '{"translated_title": "題", "summary_ja": "要約", "domain": "AI", "category": "LLM", '
    '"topics": ["t"], "technologies": ["x"], "content_type": "news", '
    '"difficulty": "beginner", "technical_quality": 0.5}'
)


class _StepClock:
    """呼ばれるたびに一定量進む時計。経過時間を決め打ちにする。"""

    def __init__(self, step: float) -> None:
        self._now = 0.0
        self._step = step

    def __call__(self) -> float:
        current = self._now
        self._now += self._step
        return current


class TestTakePrefixes:
    def test_cuts_the_text_to_each_length(self) -> None:
        assert take_prefixes("abcdef", [2, 4]) == ((2, "ab"), (4, "abcd"))

    def test_skips_lengths_longer_than_the_text(self) -> None:
        """本文より長い指定は測っても意味が無い。同じ入力を二重に測らない。"""
        assert take_prefixes("abc", [2, 10]) == ((2, "ab"),)

    def test_keeps_the_requested_order(self) -> None:
        assert [length for length, _ in take_prefixes("abcdef", [4, 2])] == [4, 2]

    def test_returns_empty_for_empty_text(self) -> None:
        assert take_prefixes("", [10]) == ()


class TestMeasureLatency:
    def test_records_the_elapsed_seconds(self) -> None:
        provider = FakeLLMProvider([_RESPONSE])

        sample = measure_latency(provider, text="body", length=4, clock=_StepClock(1.5))

        assert sample == LatencySample(length=4, seconds=1.5, ok=True)

    def test_marks_a_failed_call(self) -> None:
        """失敗も所要時間つきで残す。失敗が続く長さは上限判断の材料になる。"""
        provider = FakeLLMProvider([LLMError("失敗")])

        sample = measure_latency(provider, text="body", length=4, clock=_StepClock(2.0))

        assert sample.ok is False
        assert sample.seconds == 2.0

    def test_records_the_failure_content(self) -> None:
        """原因の切り分けに使うため、例外の型とメッセージの両方を残す（握りつぶさない）。"""
        provider = FakeLLMProvider([LLMError("失敗しました")])

        sample = measure_latency(provider, text="body", length=4, clock=_StepClock(1.0))

        assert sample.exception_type == "LLMError"
        assert sample.message == "失敗しました"

    def test_succeeded_sample_has_no_failure_content(self) -> None:
        provider = FakeLLMProvider([_RESPONSE])

        sample = measure_latency(provider, text="body", length=4, clock=_StepClock(1.0))

        assert sample.exception_type is None
        assert sample.message is None

    def test_reraises_tool_use_detected_error(self) -> None:
        """隔離破りの検知シグナルは握りつぶさず、そのまま送出して計測を止める（ADR 0002）。"""
        provider = FakeLLMProvider([LLMToolUseDetectedError("ツール使用を検知")])

        with pytest.raises(LLMToolUseDetectedError):
            measure_latency(provider, text="body", length=4, clock=_StepClock(1.0))

    def test_reraises_managed_policy_detected_error(self) -> None:
        provider = FakeLLMProvider([LLMManagedPolicyDetectedError("管理者ポリシーを検知")])

        with pytest.raises(LLMManagedPolicyDetectedError):
            measure_latency(provider, text="body", length=4, clock=_StepClock(1.0))

    def test_passes_the_text_to_the_provider(self) -> None:
        provider = FakeLLMProvider([_RESPONSE])

        measure_latency(provider, text="body", length=4, clock=_StepClock(1.0))

        assert provider.calls[0]["untrusted_content"].endswith("body")


class TestSummarizeLatencies:
    def test_groups_by_length(self) -> None:
        samples = [
            LatencySample(length=1000, seconds=1.0, ok=True),
            LatencySample(length=1000, seconds=3.0, ok=True),
            LatencySample(length=2000, seconds=5.0, ok=True),
        ]

        stats = summarize_latencies(samples)

        assert [s.length for s in stats] == [1000, 2000]
        assert stats[0].samples == 2
        assert stats[1].samples == 1

    def test_reports_median_min_max(self) -> None:
        samples = [
            LatencySample(length=1000, seconds=1.0, ok=True),
            LatencySample(length=1000, seconds=2.0, ok=True),
            LatencySample(length=1000, seconds=6.0, ok=True),
        ]

        stats = summarize_latencies(samples)

        assert stats[0].median_seconds == pytest.approx(2.0)
        assert stats[0].min_seconds == pytest.approx(1.0)
        assert stats[0].max_seconds == pytest.approx(6.0)

    def test_excludes_failures_from_the_timing(self) -> None:
        """失敗の所要時間は成功の統計へ混ぜない。途中で落ちた分だけ短く出るため。"""
        samples = [
            LatencySample(length=1000, seconds=2.0, ok=True),
            LatencySample(length=1000, seconds=0.1, ok=False),
        ]

        stats = summarize_latencies(samples)

        assert stats[0].samples == 1
        assert stats[0].failures == 1
        assert stats[0].median_seconds == pytest.approx(2.0)

    def test_keeps_a_length_with_only_failures(self) -> None:
        """全部失敗した長さも残す。「測れなかった」ことが結果である。"""
        samples = [LatencySample(length=99999, seconds=0.5, ok=False)]

        stats = summarize_latencies(samples)

        assert stats[0].samples == 0
        assert stats[0].failures == 1
        assert stats[0].median_seconds is None

    def test_returns_empty_without_samples(self) -> None:
        assert summarize_latencies([]) == ()

    def test_reports_the_failure_breakdown_by_exception_type(self) -> None:
        """どの例外が何回起きたかを人に見えるようにする。"""
        samples = [
            LatencySample(length=1000, seconds=1.0, ok=True),
            LatencySample(
                length=1000,
                seconds=0.1,
                ok=False,
                exception_type="LLMInvalidResponseError",
                message="不正な応答",
            ),
            LatencySample(
                length=1000,
                seconds=0.2,
                ok=False,
                exception_type="LLMInvalidResponseError",
                message="不正な応答2",
            ),
            LatencySample(
                length=1000,
                seconds=0.3,
                ok=False,
                exception_type="LLMTimeoutError",
                message="タイムアウト",
            ),
        ]

        stats = summarize_latencies(samples)

        assert stats[0].failure_breakdown == {
            "LLMInvalidResponseError": 2,
            "LLMTimeoutError": 1,
        }

    def test_failure_breakdown_is_empty_when_nothing_failed(self) -> None:
        samples = [LatencySample(length=1000, seconds=1.0, ok=True)]

        stats = summarize_latencies(samples)

        assert stats[0].failure_breakdown == {}

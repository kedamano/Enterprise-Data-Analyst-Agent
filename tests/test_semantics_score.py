"""core/rag/confidence.py — RAG 检索置信度评分函数。

待测模块：app/core/rag/confidence.py（confidence_of / threshold / _is_echo_chunk）

覆盖链路：
- confidence_of：有高分 chunks → "high" + basis=phrase_affinity。
- confidence_of：chunks 文本与 query 无关 → "low"。
- confidence_of：空 chunks → "none" + basis=no_chunks。
- _is_echo_chunk：首条 = query 字面复述 → 强制判 low。
- CE 路径：cross_encoder_available 时判据切到 rerank_score。

mock 策略：cross_encoder_available 用 mocker.patch 切到 False 以走 phrase_affinity 默认路径
（确定性、离线）；CE 路径单独一个测试 mock _cross_encoder_available=True。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.core.rag import confidence
from app.core.rag.confidence import (
    Confidence,
    _echo_ratio,
    _is_echo_chunk,
    _echo_strip,
    confidence_of,
    threshold,
)


# --------------------------------------------------------------------------- #
# 1. confidence_of 主链路
# --------------------------------------------------------------------------- #
class TestConfidenceOf:
    """confidence_of 凭首条 chunk 文本与 query 的字面亲和度判级。"""

    def test_high_confidence_when_chunks_match_query(self):
        """首条文本高度包含 query 词 → high + phrase_affinity。"""
        query = "企业营收分析"
        chunks = [
            {"id": 1, "text": "企业营收分析是指对企业在一定时期内通过销售商品或提供服务所获得的总收入进行系统性评估。"},
        ]
        result = confidence_of(query, chunks)
        assert result.level == "high"
        assert result.basis == "phrase_affinity"
        assert result.score > 0

    def test_low_confidence_when_chunks_unrelated(self):
        """首条文本与 query 几乎无关 → low。"""
        query = "营收毛利分析"
        chunks = [
            {"id": 1, "text": "今天天气晴朗，适合户外活动。"},
        ]
        result = confidence_of(query, chunks)
        assert result.level == "low"
        assert result.basis == "phrase_affinity"

    def test_none_when_no_chunks(self):
        """没有候选 chunk → none + score 0。"""
        result = confidence_of("营收", None)
        assert result.level == "none"
        assert result.score == 0.0
        assert result.basis == "no_chunks"

    def test_none_when_empty_list(self):
        result = confidence_of("营收", [])
        assert result.level == "none"

    def test_low_when_chunk_text_is_empty_string(self):
        """chunks 列表非空但 text 为空 → no_signal → low。"""
        result = confidence_of("营收", [{"id": 1, "text": ""}])
        assert result.level == "low"
        assert result.basis == "no_signal"


# --------------------------------------------------------------------------- #
# 2. 阈值辅助函数
# --------------------------------------------------------------------------- #
class TestThreshold:
    def test_threshold_returns_default_when_not_configured(self):
        """settings 无 rag_min_confidence 时返回 DEFAULT_THRESHOLD (0.15)。"""
        mock_settings = MagicMock(spec=[])  # 没有任何属性 → getattr 走默认值
        with patch("app.core.rag.confidence.get_settings", return_value=mock_settings):
            assert threshold() == 0.15


# --------------------------------------------------------------------------- #
# 3. Echo 检测（D62 字面复述降级）
# --------------------------------------------------------------------------- #
class TestEchoDetection:
    def _cfg(self, enabled=True, min_chars=4, ratio_max=2.0):
        """构造 mock settings，让 _is_echo_chunk 读到 echo 配置。"""
        s = type("S", (), {})()
        s.rag_echo_demotion_enabled = enabled
        s.rag_echo_min_chars = min_chars
        s.rag_echo_ratio_max = ratio_max
        return s

    def test_not_echo_when_too_short(self):
        """query 太短（< rag_echo_min_chars）→ 不触发 echo 降级。"""
        cfg = self._cfg()
        assert _is_echo_chunk("营收", "营收") is False

    def test_echo_detected_when_chunk_restates_query(self):
        """首条 = query 加少量语气 → echo 区间内 → 判 low。"""
        query = "企业营收分析方法"
        chunk_text = "企业营收分析方法是什么"
        cfg = self._cfg()
        # ratio = len(strip(text))/len(strip(query)); ≈ 12/9 ≈ 1.33 ∈ [1.0, 2.0]
        import app.core.rag.confidence as _c

        orig = _c.get_settings
        _c.get_settings = lambda: cfg
        try:
            assert _is_echo_chunk(query, chunk_text) is True
        finally:
            _c.get_settings = orig

    def test_echo_forces_low_confidence(self):
        """echo chunk 出现在首条时，即使词重叠高也被强制判 low。"""
        query = "企业营收分析的完整方法论"
        chunks = [{"id": 1, "text": "企业营收分析的完整方法论是什么"}]
        import app.core.rag.confidence as _c

        cfg = self._cfg()
        orig = _c.get_settings
        _c.get_settings = lambda: cfg
        try:
            result = confidence_of(query, chunks)
            assert result.level == "low"
            assert result.basis == "echo_question"
        finally:
            _c.get_settings = orig


# --------------------------------------------------------------------------- #
# 4. Cross-Encoder 路径
# --------------------------------------------------------------------------- #
class TestCrossEncoderPath:
    def test_ce_path_uses_rerank_score(self):
        """_cross_encoder_available → True 且首条含 rerank_score → basis=cross_encoder。"""
        with (
            patch("app.core.rag.confidence._cross_encoder_available", return_value=True),
            patch("app.core.rag.confidence.ce_threshold", return_value=0.5),
        ):
            query = "营收"
            chunks = [{"id": 1, "text": "营收分析", "rerank_score": 0.85}]
            result = confidence_of(query, chunks)
            assert result.basis == "cross_encoder"
            assert result.level == "high"

    def test_ce_low_when_rerank_score_below_threshold(self):
        with (
            patch("app.core.rag.confidence._cross_encoder_available", return_value=True),
            patch("app.core.rag.confidence.ce_threshold", return_value=0.5),
        ):
            query = "营收"
            chunks = [{"id": 1, "text": "营收数据", "rerank_score": 0.2}]
            result = confidence_of(query, chunks)
            assert result.basis == "cross_encoder"
            assert result.level == "low"

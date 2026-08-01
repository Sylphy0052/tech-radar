"""記事解析層。

言語判定・要約・分類をこのパッケージへ隔離する。
本文は非信頼入力のまま `techradar.llm` へ渡し、防御はその層が担う。
"""

from techradar.analysis.language import detect_language, normalize_language_tag, resolve_language
from techradar.analysis.prompt import ANALYSIS_INSTRUCTION
from techradar.analysis.schema import ArticleAnalysis
from techradar.analysis.service import (
    AnalysisResult,
    analyze_article,
    needs_analysis,
)

__all__ = [
    "ANALYSIS_INSTRUCTION",
    "AnalysisResult",
    "ArticleAnalysis",
    "analyze_article",
    "detect_language",
    "needs_analysis",
    "normalize_language_tag",
    "resolve_language",
]

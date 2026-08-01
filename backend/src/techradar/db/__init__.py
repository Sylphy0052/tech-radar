"""DB 層。モデル定義・セッション管理・列挙値を提供する。"""

from techradar.db.base import Base
from techradar.db.models import (
    EMBEDDING_DIMENSIONS,
    Article,
    ArticleFeedback,
    Job,
    OperationLog,
    Recommendation,
    RecommendationRun,
    SourceRegistry,
    UserArticle,
    UserInterestCluster,
    UserTopicPreference,
)
from techradar.db.session import get_engine, get_session_factory, session_scope

__all__ = [
    "EMBEDDING_DIMENSIONS",
    "Article",
    "ArticleFeedback",
    "Base",
    "Job",
    "OperationLog",
    "Recommendation",
    "RecommendationRun",
    "SourceRegistry",
    "UserArticle",
    "UserInterestCluster",
    "UserTopicPreference",
    "get_engine",
    "get_session_factory",
    "session_scope",
]

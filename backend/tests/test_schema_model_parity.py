"""SQLAlchemy モデルと Pydantic スキーマの列/フィールド整合性テスト（Issue #18）。

「モデルに列を追加したのに API スキーマへ反映し忘れる」「API スキーマを追加したのに
parity 宣言へ反映し忘れる」を機械的に検出するため、モデルの列を「API へ公開する列
（exposed）」と「意図的に公開しない列（internal）」へ完全に分類する宣言をこのファイルに
持たせ、実際のモデル定義・スキーマ定義と突き合わせる。宣言漏れ・宣言先の誤り
（存在しない列/フィールド）はいずれもテスト失敗として検出する。

検証ロジックは `tests/schema_parity.py` の `verify_*` / `assert_*` ヘルパーへ切り出し、
ヘルパー自体の RED/GREEN は `tests/test_schema_parity_helpers.py` で独立に検証する。

**このテスト機構が保証する範囲（限界）**: 詳細は `tests/schema_parity.py` の
モジュール docstring / `ModelParitySpec` の docstring を参照。要点のみ書くと、
green は「列・フィールドの分類漏れが無い」という構造的完全性の保証であって、
その分類がセキュリティ上妥当かの保証ではない（機微な列を `exposed` へ追加・
移動する差分は security-auditor レビュー必須）。また値の詰め替えロジックの
正しさは検証しない（それは `test_api_*.py` の責務）。
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel
from sqlalchemy.orm import DeclarativeBase

from techradar import api as api_package
from techradar import main as main_module
from techradar.api.articles import (
    ArticleRegistrationCreate,
    ArticleRegistrationResponse,
    InterestArticleItem,
    InterestArticleListResponse,
)
from techradar.api.crawl import CrawlRunCreate, CrawlRunResponse
from techradar.api.feedback import ArticleFeedbackCreate, ArticleFeedbackResponse
from techradar.api.interests import (
    InterestClusterItem,
    InterestClusterListResponse,
    InterestContentTypeItem,
    InterestDifficultyItem,
    InterestFeedbackRatio,
    InterestGenreItem,
    InterestPrimarySourceRatio,
    InterestSummaryResponse,
    InterestTechnologyItem,
    InterestTimelineBucket,
    InterestTimelineResponse,
    InterestTimelineTopicStats,
    InterestTopicItem,
    InterestTopicListResponse,
    SuppressedTopicItem,
)
from techradar.api.jobs import JobResponse
from techradar.api.rate_limit import RateLimitedResponse
from techradar.api.recommendations import (
    ArticleRecommendationsResponse,
    FeedResponse,
    RecommendationItem,
)
from techradar.api.sources import SourceCreate, SourceResponse, SourceUpdate
from techradar.db import Base
from techradar.db.models import (
    Article,
    ArticleFeedback,
    ArticleRegistration,
    Job,
    OperationLog,
    Recommendation,
    RecommendationRun,
    SourceRegistry,
    UserArticle,
    UserInterestCluster,
    UserTopicPreference,
)
from techradar.main import HealthResponse
from tests.schema_parity import (
    DerivedField,
    ExposedField,
    ModelParitySpec,
    all_mapped_models,
    assert_parity,
    assert_schema_coverage,
    basemodel_subclasses_in_package,
    schemas_reachable_from_app,
    verify_all_api_schemas_classified,
    verify_all_classified,
    verify_all_models_classified,
)

# 実モデルの parity 宣言。
# 列名 → その値を公開している (スキーマ, フィールド名) の組。
# 1 列が複数スキーマに出る場合は複数組を書く（例: Article.id は
# RecommendationItem.article_id と InterestArticleItem.article_id の両方）。

ARTICLE_SPEC = ModelParitySpec(
    model=Article,
    exposed={
        "id": (
            ExposedField(InterestArticleItem, "article_id"),
            ExposedField(RecommendationItem, "article_id"),
        ),
        "canonical_url": (
            ExposedField(InterestArticleItem, "canonical_url"),
            ExposedField(RecommendationItem, "canonical_url"),
        ),
        "original_url": (
            ExposedField(InterestArticleItem, "original_url"),
            ExposedField(RecommendationItem, "original_url"),
        ),
        "title": (
            ExposedField(InterestArticleItem, "title"),
            ExposedField(RecommendationItem, "title"),
        ),
        "translated_title": (
            ExposedField(InterestArticleItem, "translated_title"),
            ExposedField(RecommendationItem, "translated_title"),
        ),
        "summary_ja": (ExposedField(RecommendationItem, "summary_ja"),),
        "source_domain": (
            ExposedField(InterestArticleItem, "source_domain"),
            ExposedField(RecommendationItem, "source_domain"),
        ),
        "language": (
            ExposedField(InterestArticleItem, "language"),
            ExposedField(RecommendationItem, "language"),
        ),
        "published_at": (
            ExposedField(InterestArticleItem, "published_at"),
            ExposedField(RecommendationItem, "published_at"),
        ),
        "domain": (ExposedField(InterestArticleItem, "domain"),),
        "category": (ExposedField(InterestArticleItem, "category"),),
        "topics": (
            ExposedField(InterestArticleItem, "topics"),
            ExposedField(RecommendationItem, "topics"),
        ),
        "technologies": (ExposedField(RecommendationItem, "technologies"),),
        "content_type": (ExposedField(InterestArticleItem, "content_type"),),
        "is_primary_source": (
            ExposedField(InterestArticleItem, "is_primary_source"),
            ExposedField(RecommendationItem, "is_primary_source"),
        ),
    },
    internal={
        # ADR 0001 により、本文は内部保存のみで外部には表示しない。
        "body": "ADR 0001により外部非表示（本文は内部保存のみ）",
        "author": "現状どのAPIレスポンスにも未採用（公開設計待ち）",
        "fetched_at": "取得日時は内部管理用（公開APIではpublished_atのみ使用）",
        "body_hash": "本文再解析要否判定用の内部キャッシュキー",
        "difficulty": "難易度分類は推薦スコアリング内部でのみ使用、API未公開",
        "source_type": "情報源種別は推薦スコアリング内部でのみ使用、API未公開",
        "source_authority": "情報源信頼度スコアは推薦スコアリングの内部指標、API未公開",
        "technical_quality": "技術的品質スコアは推薦スコアリングの内部指標、API未公開",
        "is_dead": "リンク切れ記事のソフト削除フラグ、内部フィルタ条件専用",
        "embedding": "埋め込みベクトルは類似検索の内部表現、API未公開",
        "embedding_body_hash": "embedding再生成要否判定用の内部キャッシュキー",
        "analyzed_body_hash": "LLM解析要否判定用の内部キャッシュキー",
        "analysis_status": "記事解析パイプラインの内部進行状態",
        "duplicate_of_article_id": "重複記事クラスタリングの内部参照、API未公開",
        "duplicate_penalty": "重複記事の推薦スコア減点、内部スコアリング専用",
        "unique_value_judged_body_hash": "独自価値判定要否判定用の内部キャッシュキー",
        "has_unique_value": "独自価値判定結果は推薦スコアリング内部でのみ使用、API未公開",
    },
)

USER_ARTICLE_SPEC = ModelParitySpec(
    model=UserArticle,
    exposed={
        "origin": (ExposedField(InterestArticleItem, "origin"),),
        "created_at": (ExposedField(InterestArticleItem, "registered_at"),),
    },
    internal={
        "id": "cursorページングにのみ使う内部識別子、APIフィールドとしては非公開",
        "user_id": "ユーザー識別子はAPI非公開（他のuser_id列と同じ方針）",
        "article_id": (
            "JOINキー。値はArticle.idとしてRecommendationItem/InterestArticleItem"
            "へ公開される（重複列のため二重宣言しない）"
        ),
        "interest_weight": "関心度は推薦の内部スコアリング用途、API未公開",
    },
)

ARTICLE_FEEDBACK_SPEC = ModelParitySpec(
    model=ArticleFeedback,
    exposed={
        "action": (
            ExposedField(ArticleFeedbackCreate, "action"),
            ExposedField(ArticleFeedbackResponse, "action"),
        ),
        "reason": (
            ExposedField(ArticleFeedbackCreate, "reason"),
            ExposedField(ArticleFeedbackResponse, "reason"),
        ),
        "created_at": (ExposedField(ArticleFeedbackResponse, "created_at"),),
    },
    internal={
        "user_id": "ユーザー識別子はAPI非公開（他のuser_id列と同じ方針）",
        "article_id": "URLパスのarticle_idに紐づくためレスポンス本体には含めない",
    },
)

RECOMMENDATION_SPEC = ModelParitySpec(
    model=Recommendation,
    exposed={
        "score": (ExposedField(RecommendationItem, "score"),),
        "reasons": (ExposedField(RecommendationItem, "reasons"),),
        "rank": (ExposedField(RecommendationItem, "rank"),),
    },
    internal={
        "run_id": (
            "run_idは上位のRecommendationRun.idとしてArticleRecommendationsResponse.run_id"
            "へ公開される（重複列のため二重宣言しない）"
        ),
        "article_id": (
            "JOINキー。値はArticle.idとしてRecommendationItem.article_idへ公開される"
            "（重複列のため二重宣言しない）"
        ),
    },
)

RECOMMENDATION_RUN_SPEC = ModelParitySpec(
    model=RecommendationRun,
    exposed={
        "id": (ExposedField(ArticleRecommendationsResponse, "run_id"),),
        "mode": (ExposedField(ArticleRecommendationsResponse, "mode"),),
        "generated_at": (ExposedField(ArticleRecommendationsResponse, "generated_at"),),
    },
    internal={
        "user_id": "ユーザー識別子はAPI非公開（他のuser_id列と同じ方針）",
        "source_article_id": (
            "記事起点推薦の起点はリクエストのpathパラメータで既知のため、レスポンスには含めない"
        ),
    },
)

SOURCE_REGISTRY_SPEC = ModelParitySpec(
    model=SourceRegistry,
    exposed={
        "id": (ExposedField(SourceResponse, "id"),),
        "entity_name": (
            ExposedField(SourceResponse, "entity_name"),
            ExposedField(SourceCreate, "entity_name"),
            ExposedField(SourceUpdate, "entity_name"),
        ),
        "domain": (
            ExposedField(SourceResponse, "domain"),
            ExposedField(SourceCreate, "domain"),
        ),
        "path_pattern": (
            ExposedField(SourceResponse, "path_pattern"),
            ExposedField(SourceCreate, "path_pattern"),
        ),
        "github_org": (
            ExposedField(SourceResponse, "github_org"),
            ExposedField(SourceCreate, "github_org"),
        ),
        "source_type": (
            ExposedField(SourceResponse, "source_type"),
            ExposedField(SourceCreate, "source_type"),
            ExposedField(SourceUpdate, "source_type"),
        ),
        "authority_score": (
            ExposedField(SourceResponse, "authority_score"),
            ExposedField(SourceCreate, "authority_score"),
            ExposedField(SourceUpdate, "authority_score"),
        ),
        "verified": (
            ExposedField(SourceResponse, "verified"),
            ExposedField(SourceCreate, "verified"),
            ExposedField(SourceUpdate, "verified"),
        ),
    },
    internal={},
)

ARTICLE_REGISTRATION_SPEC = ModelParitySpec(
    model=ArticleRegistration,
    exposed={
        "id": (ExposedField(ArticleRegistrationResponse, "id"),),
        "url": (
            ExposedField(ArticleRegistrationCreate, "url"),
            ExposedField(ArticleRegistrationResponse, "url"),
        ),
        "status": (ExposedField(ArticleRegistrationResponse, "status"),),
        "article_id": (ExposedField(ArticleRegistrationResponse, "article_id"),),
        "error_reason": (ExposedField(ArticleRegistrationResponse, "error_reason"),),
        "created_at": (ExposedField(ArticleRegistrationResponse, "created_at"),),
        "updated_at": (ExposedField(ArticleRegistrationResponse, "updated_at"),),
    },
    internal={
        "user_id": "ユーザー識別子はAPI非公開（jobs.pyのJobResponseと同じ方針）",
        "normalized_url": "内部の正規化結果は無用に露出させない（jobs.pyのJobResponseと同じ方針）",
        "job_id": (
            "進行中ジョブの内部トラッキング用参照。UIはregistration idからのポーリングのみで、"
            "job_id自体は公開しない"
        ),
    },
)

JOB_SPEC = ModelParitySpec(
    model=Job,
    exposed={
        # id / status は JobResponse に加え、crawl.py の CrawlRunResponse からも公開される。
        "id": (
            ExposedField(JobResponse, "id"),
            ExposedField(CrawlRunResponse, "job_id"),
        ),
        "type": (ExposedField(JobResponse, "type"),),
        "status": (
            ExposedField(JobResponse, "status"),
            ExposedField(CrawlRunResponse, "status"),
        ),
        "attempts": (ExposedField(JobResponse, "attempts"),),
        "created_at": (ExposedField(JobResponse, "created_at"),),
        "available_at": (ExposedField(JobResponse, "available_at"),),
        "started_at": (ExposedField(JobResponse, "started_at"),),
        "finished_at": (ExposedField(JobResponse, "finished_at"),),
    },
    internal={
        "payload": "ジョブ投入内容には将来URL等の内部情報が入りうるため無条件に露出させない",
        "last_error": (
            "失敗理由は例外メッセージそのものであり、アクセス先URLやAPIキーが入りうるため非公開"
        ),
    },
)

USER_TOPIC_PREFERENCE_SPEC = ModelParitySpec(
    model=UserTopicPreference,
    exposed={
        # topic/negative_weight/effective_weight は GET /api/interests/summary の
        # suppressed_topics でも公開する（ORM の単純な filter+order+limit で
        # 取得した行の列をそのまま詰めるだけの直接対応のため、DerivedField
        # ではなく ExposedField として二重登録する）。
        "topic": (
            ExposedField(InterestTopicItem, "topic"),
            ExposedField(SuppressedTopicItem, "topic"),
        ),
        "positive_weight": (ExposedField(InterestTopicItem, "positive_weight"),),
        "negative_weight": (
            ExposedField(InterestTopicItem, "negative_weight"),
            ExposedField(SuppressedTopicItem, "negative_weight"),
        ),
        "effective_weight": (
            ExposedField(InterestTopicItem, "effective_weight"),
            ExposedField(SuppressedTopicItem, "effective_weight"),
        ),
        "updated_at": (ExposedField(InterestTopicItem, "updated_at"),),
    },
    internal={
        "user_id": "ユーザー識別子はAPI非公開（他のuser_id列と同じ方針）",
    },
)

USER_INTEREST_CLUSTER_SPEC = ModelParitySpec(
    model=UserInterestCluster,
    exposed={
        "label": (ExposedField(InterestClusterItem, "label"),),
        "weight": (ExposedField(InterestClusterItem, "weight"),),
        "topics": (ExposedField(InterestClusterItem, "topics"),),
        "updated_at": (ExposedField(InterestClusterItem, "updated_at"),),
    },
    internal={
        "id": "クラスタ行の内部PK。閲覧用途ではlabelで十分識別でき、APIレスポンスには含めない",
        "user_id": "ユーザー識別子はAPI非公開（他のuser_id列と同じ方針）",
        "centroid_embedding": (
            "埋め込みベクトル（1024次元）はレスポンス肥大化のため非公開"
            "（api/interests.pyのInterestClusterItemのdocstring参照）"
        ),
    },
)

MODEL_SPECS: tuple[ModelParitySpec, ...] = (
    ARTICLE_SPEC,
    USER_ARTICLE_SPEC,
    ARTICLE_FEEDBACK_SPEC,
    RECOMMENDATION_SPEC,
    RECOMMENDATION_RUN_SPEC,
    SOURCE_REGISTRY_SPEC,
    ARTICLE_REGISTRATION_SPEC,
    JOB_SPEC,
    USER_TOPIC_PREFERENCE_SPEC,
    USER_INTEREST_CLUSTER_SPEC,
)

# API 公開スキーマを一切持たない内部専用モデル。
INTERNAL_ONLY_MODELS: tuple[type[DeclarativeBase], ...] = (OperationLog,)

# モデル列由来ではない、スキーマ側の派生フィールド。
DERIVED_FIELDS: tuple[DerivedField, ...] = (
    DerivedField(
        RecommendationItem,
        "is_read",
        "user_articlesのoriginがread_full/clickedのいずれかを含むかどうかの派生判定"
        "（recommendation/service.pyのREAD_ORIGIN_VALUES基準）",
    ),
    DerivedField(
        RecommendationItem,
        "feedback",
        "ArticleFeedbackのオプショナルなネスト表現（未設定ならNone）",
    ),
    DerivedField(
        ArticleRecommendationsResponse,
        "items",
        "RecommendationItemのリスト。単一のモデル列由来ではない構造フィールド",
    ),
    DerivedField(
        FeedResponse,
        "items",
        "RecommendationItemのリスト。単一のモデル列由来ではない構造フィールド",
    ),
    DerivedField(
        FeedResponse,
        "next_cursor",
        "run_id+rankを符号化した不透明なページングcursor。モデル列の値そのものではない",
    ),
    DerivedField(
        InterestArticleListResponse,
        "items",
        "InterestArticleItemのリスト。単一のモデル列由来ではない構造フィールド",
    ),
    DerivedField(
        InterestArticleListResponse,
        "next_cursor",
        "user_articles.created_at+idを符号化した不透明なページングcursor",
    ),
    DerivedField(
        CrawlRunCreate,
        "source_domain",
        "巡回起点を絞る任意の入力値。Job.payload（内部情報のため非公開）へ書き込まれる"
        "だけで、公開列を読み出すフィールドではない",
    ),
    DerivedField(
        HealthResponse,
        "status",
        '稼働確認用の固定文字列 "ok"。モデル列に基づかない',
    ),
    DerivedField(
        HealthResponse,
        "version",
        "techradar.__version__定数由来。モデル列に基づかない",
    ),
    DerivedField(
        HealthResponse,
        "brave_search_enabled",
        "Settings.is_brave_search_enabledプロパティ由来。モデル列に基づかない",
    ),
    DerivedField(
        RateLimitedResponse,
        "detail",
        "429エラーの固定メッセージ（rate_limit.pyのRATE_LIMIT_DETAIL）。モデル列に基づかない",
    ),
    DerivedField(
        InterestTopicListResponse,
        "items",
        "InterestTopicItemのリスト。単一のモデル列由来ではない構造フィールド",
    ),
    DerivedField(
        InterestClusterListResponse,
        "items",
        "InterestClusterItemのリスト。単一のモデル列由来ではない構造フィールド",
    ),
    DerivedField(
        InterestTimelineTopicStats,
        "topic",
        "article_feedbackとarticles.topicsをJOINしたSQL集計結果のトピック名。"
        "articles.topics列の値そのものではなく、jsonb_array_elements_textで行展開した"
        "1要素であり、フィードバック日時（article_feedback.created_at）による週次集計"
        "の単位でもあるため、単一モデル列の直接公開ではない",
    ),
    DerivedField(
        InterestTimelineTopicStats,
        "positive_count",
        "週次バケット内でaction が good/save のarticle_feedback件数を集計したSQL集計値。"
        "モデル列の直接公開ではない",
    ),
    DerivedField(
        InterestTimelineTopicStats,
        "negative_count",
        "週次バケット内でaction が badのarticle_feedback件数を集計したSQL集計値。"
        "モデル列の直接公開ではない",
    ),
    DerivedField(
        InterestTimelineBucket,
        "week_start",
        "article_feedback.created_at/user_articles.created_atをdate_trunc('week', ...)で"
        "UTC週単位に丸めたSQL集計値。モデル列の値そのものではない",
    ),
    DerivedField(
        InterestTimelineBucket,
        "interest_article_count",
        "週次バケット内のuser_articles件数を集計したSQL集計値。モデル列の直接公開ではない",
    ),
    DerivedField(
        InterestTimelineBucket,
        "topics",
        "InterestTimelineTopicStatsのリスト。単一のモデル列由来ではない構造フィールド",
    ),
    DerivedField(
        InterestTimelineResponse,
        "buckets",
        "InterestTimelineBucketのリスト。article_feedback/user_articlesの日時から"
        "週単位で集計した派生データであり、単一のモデル列由来ではない構造フィールド",
    ),
    # GET /api/interests/summary（Issue #16）。集計値は article_feedback と
    # articles をJOINしたSQL集約クエリの結果であり、InterestTimelineTopicStats/
    # InterestTimelineBucketと同じ理由でDerivedFieldに分類する
    # （suppressed_topicsだけはuser_topic_preferencesの単純な行読み出しのため、
    # USER_TOPIC_PREFERENCE_SPEC側にExposedFieldとして登録済み）。
    DerivedField(
        InterestGenreItem,
        "domain",
        "article_feedbackとarticlesをJOINし、domainでGROUP BYした集計結果のグループキー。"
        "単一行のArticleの列読み出しではなく複数行の集計結果のため、"
        "単一モデル列の直接公開ではない",
    ),
    DerivedField(
        InterestGenreItem,
        "positive_count",
        "ジャンル別にaction が good/save のarticle_feedback件数を集計したSQL集計値。"
        "モデル列の直接公開ではない",
    ),
    DerivedField(
        InterestGenreItem,
        "negative_count",
        "ジャンル別にaction が badのarticle_feedback件数を集計したSQL集計値。"
        "モデル列の直接公開ではない",
    ),
    DerivedField(
        InterestFeedbackRatio,
        "good_count",
        "action別にarticle_feedback件数を集計したSQL集計値。モデル列の直接公開ではない",
    ),
    DerivedField(
        InterestFeedbackRatio,
        "bad_count",
        "action別にarticle_feedback件数を集計したSQL集計値。モデル列の直接公開ではない",
    ),
    DerivedField(
        InterestFeedbackRatio,
        "save_count",
        "action別にarticle_feedback件数を集計したSQL集計値。モデル列の直接公開ではない",
    ),
    DerivedField(
        InterestTechnologyItem,
        "technology",
        "articles.technologies（JSONB配列）をjsonb_array_elements_textで行展開した1要素。"
        "articles.technologies列の値そのものではなく展開結果のため、"
        "単一モデル列の直接公開ではない",
    ),
    DerivedField(
        InterestTechnologyItem,
        "count",
        "技術タグ別にaction が good/save のarticle_feedback件数を集計したSQL集計値。"
        "モデル列の直接公開ではない",
    ),
    DerivedField(
        InterestPrimarySourceRatio,
        "primary_count",
        "is_primary_source別にaction が good/save のarticle_feedback件数を"
        "集計したSQL集計値。モデル列の直接公開ではない",
    ),
    DerivedField(
        InterestPrimarySourceRatio,
        "secondary_count",
        "is_primary_source別にaction が good/save のarticle_feedback件数を"
        "集計したSQL集計値。モデル列の直接公開ではない",
    ),
    DerivedField(
        InterestContentTypeItem,
        "content_type",
        "article_feedbackとarticlesをJOINし、content_typeでGROUP BYした集計結果の"
        "グループキー。DerivedField扱いの理由はInterestGenreItem.domainと同じ",
    ),
    DerivedField(
        InterestContentTypeItem,
        "count",
        "content_type別にaction が good/save のarticle_feedback件数を集計したSQL集計値。"
        "モデル列の直接公開ではない",
    ),
    DerivedField(
        InterestDifficultyItem,
        "difficulty",
        "article_feedbackとarticlesをJOINし、difficultyでGROUP BYした集計結果の"
        "グループキー。DerivedField扱いの理由はInterestGenreItem.domainと同じ",
    ),
    DerivedField(
        InterestDifficultyItem,
        "count",
        "difficulty別にaction が good/save のarticle_feedback件数を集計したSQL集計値。"
        "モデル列の直接公開ではない",
    ),
    DerivedField(
        InterestSummaryResponse,
        "genres",
        "InterestGenreItemのリスト。単一のモデル列由来ではない構造フィールド",
    ),
    DerivedField(
        InterestSummaryResponse,
        "feedback_ratio",
        "InterestFeedbackRatioのネスト表現。単一のモデル列由来ではない構造フィールド",
    ),
    DerivedField(
        InterestSummaryResponse,
        "technologies",
        "InterestTechnologyItemのリスト。単一のモデル列由来ではない構造フィールド",
    ),
    DerivedField(
        InterestSummaryResponse,
        "primary_source_ratio",
        "InterestPrimarySourceRatioのネスト表現。単一のモデル列由来ではない構造フィールド",
    ),
    DerivedField(
        InterestSummaryResponse,
        "content_types",
        "InterestContentTypeItemのリスト。単一のモデル列由来ではない構造フィールド",
    ),
    DerivedField(
        InterestSummaryResponse,
        "difficulties",
        "InterestDifficultyItemのリスト。単一のモデル列由来ではない構造フィールド",
    ),
    DerivedField(
        InterestSummaryResponse,
        "suppressed_topics",
        "SuppressedTopicItemのリスト。単一のモデル列由来ではない構造フィールド",
    ),
)

# 対象 API スキーマ。個別の parity 宣言（exposed/derived）で網羅する。
TARGET_SCHEMAS: tuple[type[BaseModel], ...] = (
    ArticleRegistrationCreate,
    ArticleRegistrationResponse,
    InterestArticleItem,
    InterestArticleListResponse,
    ArticleFeedbackCreate,
    ArticleFeedbackResponse,
    RecommendationItem,
    ArticleRecommendationsResponse,
    FeedResponse,
    CrawlRunCreate,
    CrawlRunResponse,
    SourceResponse,
    SourceCreate,
    SourceUpdate,
    JobResponse,
    HealthResponse,
    RateLimitedResponse,
    InterestTopicItem,
    InterestTopicListResponse,
    InterestClusterItem,
    InterestClusterListResponse,
    InterestTimelineTopicStats,
    InterestTimelineBucket,
    InterestTimelineResponse,
    InterestGenreItem,
    InterestFeedbackRatio,
    InterestTechnologyItem,
    InterestPrimarySourceRatio,
    InterestContentTypeItem,
    InterestDifficultyItem,
    SuppressedTopicItem,
    InterestSummaryResponse,
)

# API 入出力スキーマではないため TARGET_SCHEMAS の対象外とするクラス（現状は無し）。
# 新しい BaseModel を techradar.api / techradar.main に追加した場合、実際に
# エンドポイントの入出力であれば TARGET_SCHEMAS へ、そうでなければ理由付きでここへ追加する。
EXCLUDED_API_SCHEMAS: tuple[type[BaseModel], ...] = ()


# =============================================================================
# 実モデル・実スキーマに対する parity テスト
# =============================================================================


@pytest.mark.parametrize("spec", MODEL_SPECS, ids=lambda spec: spec.model.__name__)
def test_model_columns_match_parity_declaration(spec: ModelParitySpec) -> None:
    """各モデルの実列集合が、exposed/internal 宣言と過不足なく一致することを検証する。"""
    assert_parity(spec)


@pytest.mark.parametrize("schema", TARGET_SCHEMAS, ids=lambda schema: schema.__name__)
def test_schema_fields_trace_back_to_model_columns(schema: type[BaseModel]) -> None:
    """各スキーマの全フィールドが、モデル列由来か派生フィールド宣言のいずれかであることを検証する。"""
    assert_schema_coverage(schema, MODEL_SPECS, DERIVED_FIELDS)


def test_every_db_model_is_classified() -> None:
    """`techradar.db` の全モデルが parity 宣言か内部専用分類のどちらかに属することを検証する。

    新しいモデルを追加してこの分類を忘れると、ここで検出される。
    """
    errors = verify_all_models_classified(
        all_mapped_models(Base), MODEL_SPECS, INTERNAL_ONLY_MODELS
    )
    assert not errors, "\n".join(errors)


def test_every_api_schema_is_classified() -> None:
    """`techradar.api` 配下と `techradar.main` の全 BaseModel が TARGET_SCHEMAS か
    EXCLUDED_API_SCHEMAS のどちらかに属することを検証する。

    新しい API スキーマを定義して TARGET_SCHEMAS へ足し忘れると、逆方向検証
    （`test_schema_fields_trace_back_to_model_columns`）を素通りしてしまう。
    この網羅漏れ自体をここで検出する。
    """
    all_schemas = basemodel_subclasses_in_package(api_package, extra_modules=(main_module,))
    errors = verify_all_api_schemas_classified(all_schemas, TARGET_SCHEMAS, EXCLUDED_API_SCHEMAS)
    assert not errors, "\n".join(errors)


def test_every_route_schema_is_classified() -> None:
    """`techradar.main.app` の全ルート（response_model・リクエストボディ・
    responses=）から実際に参照されている BaseModel が TARGET_SCHEMAS か
    EXCLUDED_API_SCHEMAS のどちらかに属することを検証する。

    `test_every_api_schema_is_classified` のパッケージ走査はモジュール配置に
    依存するため、新しいエンドポイントのスキーマを techradar.api /
    techradar.main 以外のモジュール（別パッケージの router、共通 DTO
    モジュール等）へ置くと素通りしうる。FastAPI の実際のルーティング情報から
    直接辿るこのテストは、モジュール配置に依存しない網羅性を提供する。
    """
    route_schemas = schemas_reachable_from_app(main_module.app)
    errors = verify_all_classified(
        route_schemas,
        declared=TARGET_SCHEMAS,
        excluded=EXCLUDED_API_SCHEMAS,
        label="ルーティングスキーマ",
    )
    assert not errors, "\n".join(errors)


# ルート走査が壊れたときに空集合を返して黙って無効化されないよう、
# 必ず拾えていなければならない代表スキーマ。サブルーター配下
# （`/api/sources`、`/api/feed`）・エラー応答（`responses=` の 429）・
# リクエストボディの3経路をそれぞれ1つずつ含める。
ROUTE_SCAN_SENTINEL_SCHEMAS: tuple[type[BaseModel], ...] = (
    SourceResponse,
    FeedResponse,
    RateLimitedResponse,
    ArticleRegistrationCreate,
)


def test_route_scan_reaches_known_schemas() -> None:
    """ルート走査そのものが機能していることを、代表スキーマの検出で確認する。

    `test_every_route_schema_is_classified` は「見つかったスキーマが全て分類済みか」
    しか見ないため、FastAPI 側の非互換で `_iter_api_routes` が何も拾えなくなっても
    空集合が全て分類済み扱いとなり素通りしてしまう。走査の退化をここで検出する。
    """
    route_schemas = schemas_reachable_from_app(main_module.app)
    missing = [
        schema.__name__ for schema in ROUTE_SCAN_SENTINEL_SCHEMAS if schema not in route_schemas
    ]
    assert not missing, (
        f"ルート走査が代表スキーマを拾えていません: {sorted(missing)}。"
        "FastAPI のルーティング構造が変わった可能性があるため "
        "`schema_parity._iter_api_routes` を見直すこと"
    )

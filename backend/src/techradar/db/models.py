"""DB モデル定義（`PROJECT_SPEC.md` §19 に対応）。

ユーザー固有のデータを持つテーブル（`user_articles` / `article_feedback` /
`recommendation_runs` / `user_interest_clusters` / `user_topic_preferences`）は
すべて `user_id` を持つ。MVP は単一ユーザーだが、将来のマルチユーザー化を妨げない
ため（`PROJECT_SPEC.md` §4）。

`articles` / `source_registry` / `jobs` / `operation_logs` はユーザー横断で共有する
データのため `user_id` を持たない。

列挙値は text 列として保持する。値の追加でマイグレーションが必要にならないようにするため、
検証は `techradar.db.enums` を使ってアプリ側で行う。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from techradar.db.base import Base

# Embedding の次元は採用モデル（Qwen3-Embedding-0.6B）に合わせて固定する。
# 変更する場合は再 embedding を伴うマイグレーションが必要になる。
EMBEDDING_DIMENSIONS = 1024


class Article(Base):
    """取得・解析済みの記事。

    本文（`body`）は内部保存のみで外部には表示しない。プロンプト改善時の再解析と
    重複判定に必要なため破棄しない（ADR 0001）。
    """

    __tablename__ = "articles"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    original_url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    translated_title: Mapped[str | None] = mapped_column(Text)
    summary_ja: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    source_domain: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str | None] = mapped_column(Text)
    # BCP-47 の拡張タグ (例: zh-Hans-CN-x-...) は長くなりうるため長さを制限しない。
    language: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    body_hash: Mapped[str | None] = mapped_column(String(64))
    domain: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    topics: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    technologies: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    content_type: Mapped[str | None] = mapped_column(Text)
    difficulty: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str | None] = mapped_column(Text)
    source_authority: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    technical_quality: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    is_primary_source: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # リンク切れ・削除済み記事はソフト削除する。履歴と関心プロファイルを壊さないため。
    is_dead: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    # embedding を生成した時点の body_hash。本文が更新されたら作り直すために使う。
    embedding_body_hash: Mapped[str | None] = mapped_column(String(64))
    # 解析した時点の body_hash。同じ本文を二度 LLM へ渡さないために使う。
    analyzed_body_hash: Mapped[str | None] = mapped_column(String(64))
    # 解析の進行状態（pending / analyzing / completed / failed）。
    analysis_status: Mapped[str | None] = mapped_column(Text)
    # 代表記事への自己参照。代表記事は duplicate_of_article_id IS NULL であり、
    # クラスタは同じ代表 ID でグループ化する。代表記事が削除されても重複記事自体は
    # 残すため ondelete は SET NULL にする。
    duplicate_of_article_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("articles.id", ondelete="SET NULL")
    )
    # 重複と判定された記事の推薦スコアを下げるための減点。代表記事は 0。
    duplicate_penalty: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    # 独自価値判定 (LLM) を行った時点の body_hash。本文が更新されたら
    # 判定し直すために使う（analyzed_body_hash / embedding_body_hash と同じ役割）。
    unique_value_judged_body_hash: Mapped[str | None] = mapped_column(String(64))
    # 直近の独自価値判定結果。本文が変わっていなければこの値を使い回し、
    # 同じ記事へ再実行のたびに LLM を呼ばないようにする。
    has_unique_value: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    __table_args__ = (
        # 7 日フィルターと新着順の取得で使う。
        Index("ix_articles_published_at", "published_at"),
        # 同一本文の再解析を避けるためのキャッシュ判定に使う。
        Index("ix_articles_body_hash", "body_hash"),
        Index("ix_articles_source_domain", "source_domain"),
        # クラスタ単位の取得と、代表記事（IS NULL）だけを絞り込む用途で使う。
        Index("ix_articles_duplicate_of_article_id", "duplicate_of_article_id"),
        # 近傍検索。コサイン距離で使うため vector_cosine_ops を指定する。
        Index(
            "ix_articles_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class UserArticle(Base):
    """ユーザーの関心記事（手動登録・Good・保存など）。"""

    __tablename__ = "user_articles"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    article_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), nullable=False
    )
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    interest_weight: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("user_id", "article_id", name="uq_user_articles_user_id_article_id"),
        Index("ix_user_articles_user_id", "user_id"),
    )


class ArticleFeedback(Base):
    """記事への Good / Bad / 保存。

    1 ユーザー 1 記事につき 1 行。保存と Good は別アクションだが、
    最新の意思表示を 1 行で保持する（`action` を更新する）。
    """

    __tablename__ = "article_feedback"

    user_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    article_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        PrimaryKeyConstraint("user_id", "article_id", name="pk_article_feedback"),
        Index("ix_article_feedback_article_id", "article_id"),
    )


class RecommendationRun(Base):
    """推薦の実行単位。"""

    __tablename__ = "recommendation_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    source_article_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("articles.id", ondelete="SET NULL")
    )
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_recommendation_runs_user_id", "user_id"),)


class Recommendation(Base):
    """推薦結果 1 件。`reasons` にスコア内訳を格納する（`PROJECT_SPEC.md` §26-15）。"""

    __tablename__ = "recommendations"

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recommendation_runs.id", ondelete="CASCADE"), nullable=False
    )
    article_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), nullable=False
    )
    score: Mapped[float] = mapped_column(Float, nullable=False)
    reasons: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    rank: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("run_id", "article_id", name="pk_recommendations"),
        Index("ix_recommendations_run_id_rank", "run_id", "rank"),
    )


class SourceRegistry(Base):
    """公式ソースレジストリ（`PROJECT_SPEC.md` §11）。

    コードに埋め込まず DB で管理する。誤判定は `authority_score` の更新で修正でき、
    手動確認済みかどうかを `verified` で区別する。
    """

    __tablename__ = "source_registry"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    entity_name: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str] = mapped_column(Text, nullable=False)
    path_pattern: Mapped[str | None] = mapped_column(Text)
    github_org: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    authority_score: Mapped[float] = mapped_column(Float, nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    __table_args__ = (
        # 同一ドメイン・同一パスパターン・同一 github_org の重複登録を防ぐ。
        # PostgreSQL は既定で NULL 同士を別の値として扱うため、そのままでは
        # path_pattern を持たないドメイン (ドメイン全体にかかる規則) を何度でも
        # 登録できてしまう。NULLS NOT DISTINCT で NULL も同値として扱う。
        #
        # github_org を含めるのは、github.com の Release 規則が org 単位で
        # 別物のため (github.com + /*/*/releases が組織の数だけ存在する)。
        UniqueConstraint(
            "domain",
            "path_pattern",
            "github_org",
            name="uq_source_registry_domain",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_source_registry_domain", "domain"),
    )


class UserInterestCluster(Base):
    """関心クラスタ（`PROJECT_SPEC.md` §8）。

    単一の平均 Embedding ではなく複数クラスタで関心を表現する。
    """

    __tablename__ = "user_interest_clusters"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    topics: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    centroid_embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_user_interest_clusters_user_id", "user_id"),)


class UserTopicPreference(Base):
    """トピック単位の選好。Bad は Good の単純な負数として扱わないため、正負を分けて保持する。"""

    __tablename__ = "user_topic_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    positive_weight: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    negative_weight: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    effective_weight: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (PrimaryKeyConstraint("user_id", "topic", name="pk_user_topic_preferences"),)


class Job(Base):
    """ジョブキュー（`FOR UPDATE SKIP LOCKED` で取得する）。"""

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # このジョブを実行してよくなる時刻。リトライの指数バックオフはこの列を将来へ
    # 進めて表現する。待機をワーカーのメモリに置くと、プロセス再起動で待機が失われ、
    # 他のワーカーが即座に拾ってしまう。
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # ワーカーが次のジョブを引くときの検索条件
        # (status = 'pending' かつ available_at <= now() を available_at 順に取る)。
        Index("ix_jobs_status_available_at", "status", "available_at"),
    )


class OperationLog(Base):
    """構造化ログ（`PROJECT_SPEC.md` §24 可観測性）。保持期間は 90 日。"""

    __tablename__ = "operation_logs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    article_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("articles.id", ondelete="SET NULL")
    )
    model: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error_reason: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # 保持期間 90 日の削除バッチで使う。
        Index("ix_operation_logs_created_at", "created_at"),
        Index("ix_operation_logs_operation_status", "operation", "status"),
    )

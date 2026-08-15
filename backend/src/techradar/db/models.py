"""DB モデル定義。現行のスキーマはこのファイルと `backend/migrations/` が持つ。

`PROJECT_SPEC.md` §19 は初期設計時の案で、テーブルも列もここまで追随していない。

ユーザー固有のデータを持つテーブル（`user_articles` / `article_feedback` /
`recommendation_runs` / `user_interest_clusters` / `user_topic_preferences` /
`user_source_preferences` / `article_registrations`）はすべて `user_id` を持つ。
MVP は単一ユーザーだが、将来のマルチユーザー化を妨げないため（`PROJECT_SPEC.md` §4）。

`articles` / `source_registry` は記事本体と公式ソースレジストリをユーザー横断で
共有するため、`jobs` / `operation_logs` は特定の利用者に属さないため `user_id` を
持たない。`recommendations` も持たないが、こちらは `run_id` 経由で
`recommendation_runs.user_id` へ辿れるため列を重ねていないだけで、所有者は定まる。

`discovered_feeds` も `user_id` を持たない。自動発見の入力はユーザーの登録記事だが、
出力は `feeds.yaml` と並ぶ巡回対象の一覧であり、`articles` と同じくユーザー横断で
共有するため（Issue #93）。マルチユーザー化するときは、あるユーザーの登録記事から
発見したフィードを全員が巡回してよいかを再検討し、必要なら `user_id` を足して
ユニーク制約を `(user_id, domain)` へ変える。

この内訳は `docs/decisions.md` の認証節から一次情報として参照される。テーブルを
追加・削除するときはここを更新すること。

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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from techradar.db.base import Base
from techradar.db.enums import JobStatus, JobType

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
    # 同一ニュースイベントのクラスタ ID（`PROJECT_SPEC.md` §17、Issue #20）。
    # `duplicate_of_article_id` が「どの代表記事の重複か」を表すのに対し、こちらは
    # 「どの出来事についての記事か」を表す。独自価値ありと判定されて別記事として
    # 残した記事（`duplicate_of_article_id` が NULL）も同じ ID を持つため、重複には
    # 畳まないが同一ニュースではある記事同士を後から辿れる。単独記事（同一イベント
    # の記事が他に無い記事）は NULL。
    news_event_id: Mapped[uuid.UUID | None] = mapped_column()
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
        # 同一ニュースイベントの記事をまとめて引く用途で使う（Issue #20）。
        Index("ix_articles_news_event_id", "news_event_id"),
        # 近傍検索。コサイン距離で使うため vector_cosine_ops を指定する。
        Index(
            "ix_articles_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        # トピック単位の選好更新（`interest.service._load_recent_topic_actions`）が
        # `Article.topics.contains([topic])`（JSONB `@>`）で記事を絞り込む。この関数は
        # フィードバックのたびに記事のトピック数だけ実行されるホットパスのため、
        # インデックス無しでは記事が増えるほど全件スキャンのコストが線形に増える
        # （Issue #15 自己レビュー 4）。`@>` の containment 演算子は等価判定
        # （キーの存在・値の一致）だけを使い、範囲検索や存在演算子（`?`）は使わない
        # ため、汎用の `jsonb_ops` より軽量な `jsonb_path_ops` を選ぶ。
        Index(
            "ix_articles_topics_gin",
            "topics",
            postgresql_using="gin",
            postgresql_ops={"topics": "jsonb_path_ops"},
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
    # 検索・絞り込み条件（`recommendation.service.FeedFilters`）を正規化して
    # ハッシュ化した値（Issue #90）。DISCOVER モードは常に値を持ち（条件無しは
    # 空条件のフィンガープリント）、run 再利用判定（`_resolve_discover_run_id`）が
    # user_id + mode に加えてこの列でも絞り込む。ARTICLE_BASED モードは条件という
    # 概念自体が無いため NULL のまま。
    filter_fingerprint: Mapped[str | None] = mapped_column(Text)

    # 直近 run の再利用判定（`recommendation.service.build_latest_run_select`）は
    # user_id + mode（+ filter_fingerprint、Issue #90）で絞って generated_at 降順の
    # 先頭を取るため、ソート順まで含めた複合インデックスにする。user_id 単独の
    # インデックスはこの前方一致で代替できるため持たない（Issue #32）。
    # filter_fingerprint は複合インデックスへ含めない。1 ユーザーのローカル実行を
    # 前提とした run 件数では、user_id + mode の絞り込み後にこの列だけフィルタで
    # 弾いても実害が無いため（インデックス変更は既存の実行計画検証テストへの
    # 影響が大きく、得られる効果に見合わないと判断した）。
    # 保持期間ジョブ（`jobs.handlers.purge_recommendation_runs`）の DELETE は
    # user_id で絞らず generated_at だけで範囲を切るため、単独のインデックスを別に持つ。
    __table_args__ = (
        Index(
            "ix_recommendation_runs_user_id_mode_generated_at",
            "user_id",
            "mode",
            text("generated_at DESC"),
            text("id DESC"),
        ),
        Index("ix_recommendation_runs_generated_at", "generated_at"),
    )


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


class UserSourcePreference(Base):
    """情報源単位の選好（`PROJECT_SPEC.md` §7.1 手順 4、Issue #34）。

    `articles.source_authority`（`source_registry.authority_score` 由来）が
    ユーザー横断で静的なスコアなのに対し、こちらは「この情報源は自分にとって
    当たりが多い / 外れが多い」を Good / Bad の履歴から学習するユーザー固有の
    値。キーは `source_registry.id` ではなく `articles.source_domain` にする
    （レジストリに載らない一般の情報源にも選好を持たせるため。詳細は
    `interest/sources.py` の docstring 参照）。

    `effective_weight` は `positive_weight - negative_weight` の符号付きの値で、
    トピック側（`user_topic_preferences`）の非負の値とは意味が異なる
    （`interest/sources.py` の `compute_effective_weight` 参照）。
    """

    __tablename__ = "user_source_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    source_domain: Mapped[str] = mapped_column(Text, nullable=False)
    positive_weight: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    negative_weight: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    effective_weight: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        PrimaryKeyConstraint("user_id", "source_domain", name="pk_user_source_preferences"),
    )


class ArticleRegistration(Base):
    """ユーザーによる URL 登録の状態（`PROJECT_SPEC.md` §6.2）。

    `articles` は取得できて初めて `canonical_url` / `title` が確定するユーザー横断の
    共有データであり、登録直後の状態をそこに持たせると、取得に失敗した URL の
    ゴミ行が残り続け推薦クエリ側で常に除外条件が要る。登録はユーザー操作単位の
    関心事のため、`articles` とは別テーブルに分離する。
    """

    __tablename__ = "article_registrations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    # ユーザーが入力した元の URL（表示用）。
    url: Mapped[str] = mapped_column(Text, nullable=False)
    # 重複登録判定に使う正規化済み URL（`techradar.fetcher.url.normalize_url`）。
    normalized_url: Mapped[str] = mapped_column(Text, nullable=False)
    # 処理状態（`JobStatus` の値）。初期値は pending。
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    # 取得完了後に確定する。取得前は未確定のため nullable。
    article_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("articles.id", ondelete="SET NULL")
    )
    # 進行中ジョブの追跡用。ジョブが完了・削除されても登録行自体は残す。
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    # ユーザーに見せてよい分類済みの理由。例外メッセージそのものは入れない
    # （`jobs.last_error` と異なり、この列は登録状態確認 API から直接返す前提のため）。
    error_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # 状態が進むたびに書き換える。server_default だけでは INSERT 時の値が残り続け、
    # 登録がどこまで進んだかを時刻から追えない。
    #
    # 更新時刻に now() ではなく statement_timestamp() を使うのは、now() が
    # トランザクション開始時刻を返すため。1つのトランザクションで複数回状態を
    # 進めても同じ値になってしまい、「最後に動いたのはいつか」を表さない。
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.statement_timestamp(),
    )

    __table_args__ = (
        # 同じ URL を何度も登録して fetch ジョブを積み増さないための重複登録判定。
        UniqueConstraint(
            "user_id", "normalized_url", name="uq_article_registrations_user_id_normalized_url"
        ),
        # 関心記事一覧（§6.3）でユーザー単位の一覧取得に使う。
        Index("ix_article_registrations_user_id", "user_id"),
    )


# 巡回ジョブの重複起動を1件に制限する部分ユニークインデックスの述語。
#
# DDL の述語は単なる文字列で型チェックが効かないため、enum の値をリネームしても
# 気付けない。値そのものは列挙から組み立て、リネームには追随させる。
# crawl_sources の実行中 status が searching であることは `jobs/status.py` の写像が
# 持つ知識だが、db 層から jobs 層へ依存させたくないためここでは直接指定し、
# 写像との一致は tests/test_api_crawl.py で検証する。
ACTIVE_CRAWL_JOB_INDEX_PREDICATE = (
    f"type = '{JobType.CRAWL_SOURCES.value}' "
    f"AND status IN ('{JobStatus.PENDING.value}', '{JobStatus.SEARCHING.value}')"
)


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
        # 巡回ジョブの重複起動を DB 側で1件に制限する (Issue #26)。API 側の事前確認だけでは
        # 確認と INSERT の間に別リクエストが割り込む TOCTOU レースを塞げないため、
        # 一意制約を最終的な防衛線に置く。crawl_sources が取りうる実行中 status は
        # searching だけなので、pending と合わせた2つを「まだ終わっていない」とみなす。
        Index(
            "ux_jobs_active_crawl_sources",
            "type",
            unique=True,
            postgresql_where=text(ACTIVE_CRAWL_JOB_INDEX_PREDICATE),
        ),
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


class DiscoveredFeed(Base):
    """登録記事のドメイン集計から自動発見した巡回対象（Issue #93）。

    `source_registry` は情報源の権威スコア判定用の規則であり巡回対象リストでは
    ないため、ここを流用せず専用テーブルを持つ（Issue #93 ヒアリングでの決定）。

    ドメイン集計は `interest.service._load_interest_article_population` とは
    共用しない。あちらは関心スコア計算用の母集団（`article_feedback` の
    action='good' による補完・重み計算を含む）で戻り値の型・責務が異なり、
    無理に共用するとインターフェースが歪むため、
    `techradar.collectors.discovery.aggregate_domain_counts` として単純な
    GROUP BY 集計を別に持つ。
    """

    __tablename__ = "discovered_feeds"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    domain: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # 発見できた場合のみ値を持つ。`FeedEntryConfig` の制約に合わせ https 限定
    # （発見処理側で https 以外の候補を採用しない）。
    feed_url: Mapped[str | None] = mapped_column(Text)
    # DiscoveredFeedStatus の値。
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # 発見を試みた時点の、このドメインの登録記事件数（再試行のたびに更新する）。
    article_count: Mapped[int] = mapped_column(Integer, nullable=False)
    last_attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # status=FOUND のときのみ True にする（発見処理側が明示的に立てる。
    # feed_url が無いのに enabled でも意味が無いため既定は False）。
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # 巡回の連続失敗回数（Issue #105）。1 回でも成功したら 0 へリセットする。
    # `MAX_CONSECUTIVE_FEED_FAILURES` に達すると status=DISABLED / enabled=False にする。
    # `feeds.yaml` 由来の手動フィードは対象外（`collectors.discovery` docstring 参照）。
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # 直近の巡回成功時刻（Issue #105）。一度も成功していなければ NULL のまま。
    last_succeeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 取得・パースには成功したが記事を1件も配信しなかった連続回数（Issue #108）。
    # 1件でも配信できたら 0 へリセットする。取得・パース自体に失敗した回は
    # 「0件だった」と数えない（`consecutive_failures` の方でのみ数え、この列には
    # 触れない）。`MAX_CONSECUTIVE_EMPTY_FETCHES` に達すると status=DISABLED /
    # enabled=False にする。`feeds.yaml` 由来の手動フィードは対象外
    # （`collectors.discovery` docstring 参照）。
    consecutive_empty_fetches: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.statement_timestamp(),
    )

    __table_args__ = (
        # 巡回時に「発見済みで有効なフィードだけ」を抽出するのに使う
        # （`collectors.discovery.load_enabled_discovered_feeds`）。
        Index("ix_discovered_feeds_status_enabled", "status", "enabled"),
    )

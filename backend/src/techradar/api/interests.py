"""関心プロファイル閲覧 API（`PROJECT_SPEC.md` §7, §8, Issue #15 段階 3）。

書き込みは `interest/service.py`（`update_topic_preferences` / `rebuild_interest_clusters`）
が担い、ここでは `user_topic_preferences` / `user_interest_clusters` と、既存の
`article_feedback` / `user_articles` を読み取り専用で公開するだけに徹する。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from techradar.api.deps import get_current_user_id, get_now, get_session
from techradar.db.models import UserInterestCluster, UserTopicPreference
from techradar.recommendation.config import get_scoring_config

router = APIRouter(prefix="/api/interests", tags=["interests"])

SessionDep = Annotated[Session, Depends(get_session)]
UserIdDep = Annotated[uuid.UUID, Depends(get_current_user_id)]
NowDep = Annotated[datetime, Depends(get_now)]

# GET /api/interests のページングは既存 API（`api/recommendations.py` の
# GET /api/feed）と同じく config/scoring.yaml の limits に従う
# （運用しながら調整するため、コードに埋め込まない）。
_page_size_limits = get_scoring_config().limits
DEFAULT_PAGE_SIZE = _page_size_limits.default_page_size
MAX_PAGE_SIZE = _page_size_limits.max_page_size

# GET /api/interests/timeline の週数の既定値・上限値。
_timeline_limits = get_scoring_config().interest_timeline
DEFAULT_TIMELINE_WEEKS = _timeline_limits.default_weeks
MAX_TIMELINE_WEEKS = _timeline_limits.max_weeks


class InterestTopicItem(BaseModel):
    """トピック単位の関心 1 件（`user_topic_preferences` 1 行）のレスポンス。"""

    topic: str
    positive_weight: float
    negative_weight: float
    effective_weight: float
    updated_at: datetime


class InterestTopicListResponse(BaseModel):
    """`GET /api/interests` のレスポンス。"""

    items: list[InterestTopicItem]


@router.get("", response_model=InterestTopicListResponse)
def list_interest_topics(
    session: SessionDep,
    user_id: UserIdDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE, description="取得件数")] = (
        DEFAULT_PAGE_SIZE
    ),
    offset: Annotated[int, Query(ge=0, description="スキップする件数")] = 0,
) -> InterestTopicListResponse:
    """トピック単位の関心一覧を返す（`PROJECT_SPEC.md` §7.1, §7.2）。

    `effective_weight` 降順（同値は `topic` 昇順）で返す。並び順を安定させる
    ためにタイブレークを必ず付ける（`recommendation/service.py` の他の並び順と
    同じ方針）。件数はトピックの語彙数に比例し高々数百件程度に収まる見込み
    のため、`cursor` ではなく単純な `offset`/`limit` にする（`api/articles.py`
    の関心記事一覧のような無限スクロール想定の cursor ページングは過剰）。
    """
    rows = session.scalars(
        select(UserTopicPreference)
        .where(UserTopicPreference.user_id == user_id)
        .order_by(UserTopicPreference.effective_weight.desc(), UserTopicPreference.topic.asc())
        .offset(offset)
        .limit(limit)
    ).all()
    return InterestTopicListResponse(
        items=[
            InterestTopicItem(
                topic=row.topic,
                positive_weight=row.positive_weight,
                negative_weight=row.negative_weight,
                effective_weight=row.effective_weight,
                updated_at=row.updated_at,
            )
            for row in rows
        ]
    )


class InterestClusterItem(BaseModel):
    """関心クラスタ 1 件のレスポンス。

    `centroid_embedding` は 1024 次元でレスポンスが肥大化するため返さない
    （閲覧用途では不要な内部表現のため）。
    """

    label: str
    weight: float
    topics: list[str]
    updated_at: datetime


class InterestClusterListResponse(BaseModel):
    """`GET /api/interests/clusters` のレスポンス。"""

    items: list[InterestClusterItem]


@router.get("/clusters", response_model=InterestClusterListResponse)
def list_interest_clusters(
    session: SessionDep,
    user_id: UserIdDep,
) -> InterestClusterListResponse:
    """関心クラスタ一覧を返す（`PROJECT_SPEC.md` §8）。

    `weight` 降順（同値は `label` 昇順）で返す。クラスタ数は
    `config/scoring.yaml` の `clustering.max_clusters` で上限が付いており
    （既定 8）小規模なため、ページングは設けない。
    """
    rows = session.scalars(
        select(UserInterestCluster)
        .where(UserInterestCluster.user_id == user_id)
        .order_by(UserInterestCluster.weight.desc(), UserInterestCluster.label.asc())
    ).all()
    return InterestClusterListResponse(
        items=[
            InterestClusterItem(
                label=row.label,
                weight=row.weight,
                topics=list(row.topics),
                updated_at=row.updated_at,
            )
            for row in rows
        ]
    )


class InterestTimelineTopicStats(BaseModel):
    """タイムラインの 1 週バケット内、1 トピックぶんの集計。"""

    topic: str
    positive_count: int
    negative_count: int


class InterestTimelineBucket(BaseModel):
    """タイムラインの 1 週バケット。"""

    # そのバケット（週）の開始時刻（UTC、月曜日 00:00）。
    week_start: datetime
    # そのバケット内に `user_articles` へ追加された関心記事の件数（origin 問わず合算）。
    interest_article_count: int
    # そのバケット内のフィードバックから求めたトピック別集計。
    topics: list[InterestTimelineTopicStats]


class InterestTimelineResponse(BaseModel):
    """`GET /api/interests/timeline` のレスポンス。"""

    buckets: list[InterestTimelineBucket]


# トピック別の週次集計（`article_feedback` と `articles.topics` を突き合わせる）。
# JSONB 配列 `topics` を `jsonb_array_elements_text` で行展開してからトピック単位に
# 集約する。集計は SQL 側で行い、Python 側へ全件ロードしない。
#
# 週の境界は UTC 基準。`date_trunc('week', ...)` は ISO 8601 に従い月曜日 00:00 を
# 週の開始とする。`timezone('UTC', created_at)` で timestamptz を UTC の壁時計時刻へ
# 変換してから `date_trunc` することで、DB セッションのタイムゾーン設定に依存せず
# 常に UTC 基準の週境界になるようにする。
#
# WHERE 句の `created_at >= :since` は timestamptz 同士の比較のまま行う
# （`timezone('UTC', ...)` を挟むと naive timestamp と tz-aware パラメータの
# 型推論があいまいになるため、SELECT/GROUP BY 側だけに閉じる）。
_TOPIC_TIMELINE_QUERY = text(
    """
    SELECT
        date_trunc('week', timezone('UTC', af.created_at)) AS week_start,
        topic_value.value AS topic,
        COUNT(*) FILTER (WHERE af.action IN ('good', 'save')) AS positive_count,
        COUNT(*) FILTER (WHERE af.action = 'bad') AS negative_count
    FROM article_feedback af
    JOIN articles a ON a.id = af.article_id
    CROSS JOIN LATERAL jsonb_array_elements_text(a.topics) AS topic_value(value)
    WHERE af.user_id = :user_id
      AND af.created_at >= :since
    GROUP BY week_start, topic_value.value
    ORDER BY week_start ASC, topic_value.value ASC
    """
)

# 週次の関心記事追加件数（`user_articles`）。トピックには依らない全体件数。
_INTEREST_ARTICLE_COUNT_TIMELINE_QUERY = text(
    """
    SELECT
        date_trunc('week', timezone('UTC', created_at)) AS week_start,
        COUNT(*) AS interest_article_count
    FROM user_articles
    WHERE user_id = :user_id
      AND created_at >= :since
    GROUP BY week_start
    ORDER BY week_start ASC
    """
)


@router.get("/timeline", response_model=InterestTimelineResponse)
def get_interest_timeline(
    session: SessionDep,
    user_id: UserIdDep,
    now: NowDep,
    weeks: Annotated[
        int, Query(ge=1, le=MAX_TIMELINE_WEEKS, description="遡って集計する週数")
    ] = DEFAULT_TIMELINE_WEEKS,
) -> InterestTimelineResponse:
    """関心の推移を週単位のバケットへ集計して返す。

    履歴テーブルは新設せず、`user_articles.created_at` と
    `article_feedback.created_at` を集計元にする。`weeks` は `now` から
    `weeks * 7日` 遡った時刻以降を対象にする目安であり、境界週は部分週に
    なりうる（ちょうど週の途中から `since` が始まるため）。

    データが1件も無い週はバケットを作らない（0 件のバケットを並べても
    情報量が無いため）。トピック別集計とは独立に、関心記事の追加件数
    （`user_articles`）も同じ週バケットへ載せて返す。
    """
    since = now - timedelta(weeks=weeks)

    topic_rows = session.execute(_TOPIC_TIMELINE_QUERY, {"user_id": user_id, "since": since}).all()
    count_rows = session.execute(
        _INTEREST_ARTICLE_COUNT_TIMELINE_QUERY, {"user_id": user_id, "since": since}
    ).all()

    topics_by_week: dict[datetime, list[InterestTimelineTopicStats]] = {}
    for week_start, topic, positive_count, negative_count in topic_rows:
        topics_by_week.setdefault(week_start, []).append(
            InterestTimelineTopicStats(
                topic=topic, positive_count=positive_count, negative_count=negative_count
            )
        )
    count_by_week: dict[datetime, int] = {}
    for week_start, count in count_rows:
        count_by_week[week_start] = count

    all_weeks = sorted(set(topics_by_week) | set(count_by_week))
    return InterestTimelineResponse(
        buckets=[
            InterestTimelineBucket(
                # date_trunc の戻り値は timestamp without time zone（naive）のため、
                # UTC 基準であることをレスポンス上でも明示する。
                week_start=week_start.replace(tzinfo=UTC),
                interest_article_count=count_by_week.get(week_start, 0),
                topics=topics_by_week.get(week_start, []),
            )
            for week_start in all_weeks
        ]
    )

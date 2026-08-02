"""関心プロファイルの DB 連携（`PROJECT_SPEC.md` §7, §8, Issue #15 段階 3）。

`interest/` 配下の純粋関数（`topics.py` / `clusters.py` / `weights.py`）と DB の
橋渡しだけを担う（`recommendation/service.py` と同じ責務分割）。`commit` はせず、
呼び出し側（`api/feedback.py` / `jobs/handlers/rebuild_interest_clusters.py`）に委ねる。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from techradar.db.enums import ArticleOrigin, FeedbackAction
from techradar.db.errors import is_unique_violation
from techradar.db.models import (
    Article,
    ArticleFeedback,
    UserArticle,
    UserInterestCluster,
    UserTopicPreference,
)
from techradar.interest.clusters import ClusteringSettings, ClusterSource, build_interest_clusters
from techradar.interest.topics import (
    TopicPreferenceSettings,
    TopicWeights,
    apply_bad_feedback,
    compute_effective_weight,
    compute_negative_weight,
    increase_positive_weight,
)
from techradar.interest.weights import (
    DEFAULT_CONFIDENCE,
    FeedbackWeights,
    compute_effective_interest,
    compute_recency_decay,
    explicit_weight_for_origin,
)
from techradar.recommendation.config import ScoringConfig, get_scoring_config

logger = logging.getLogger(__name__)

# 経過日数を求めるための秒数。`recommendation/ranking.py` / `recommendation/service.py`
# の同名の私有定数と同じ値（モジュールをまたいで private 定数を共有しないという
# 既存の方針、`recommendation/service.py` の同名定数のコメント参照）。
_SECONDS_PER_DAY = 86400

# `interest/weights.py` の `compute_effective_interest` に渡す feedback_weight の
# 既定値。`explicit_weight_for_origin` が返す値（`user_articles.origin` から得る
# 経路の重み）が既に `article_feedback.action` の Good/Bad シグナルを反映済みの
# ため、ここでさらに `feedback_weights.good` 等を掛けると二重計上になる。詳細は
# `load_weighted_interest_articles` の docstring 参照（`recommendation/service.py`
# の同名定数と同じ理由）。
_NEUTRAL_FEEDBACK_WEIGHT = 1.0

# 関心プロファイル構築の対象にする origin（`PROJECT_SPEC.md` §7.1 の重み表の経路すべて）。
_INTEREST_ORIGIN_VALUES = frozenset(
    origin.value
    for origin in (
        ArticleOrigin.MANUAL,
        ArticleOrigin.GOOD,
        ArticleOrigin.SAVED,
        ArticleOrigin.READ_FULL,
        ArticleOrigin.CLICKED,
    )
)

# トピック単位の選好更新で positive_weight を増やす action（Good / 保存、`PROJECT_SPEC.md` §7.1）。
_POSITIVE_FEEDBACK_ACTIONS = frozenset({FeedbackAction.GOOD, FeedbackAction.SAVE})


def order_by_recency_and_truncate(
    created_at_by_article_id: dict[uuid.UUID, datetime], limit: int, label: str
) -> tuple[uuid.UUID, ...]:
    """`created_at` 降順（同時刻は article_id 昇順）で並べ、`limit` 件で打ち切る。

    `load_weighted_interest_articles`（関心プロファイル構築対象）と
    `recommendation.service._load_bad_embeddings`（Bad プロファイル構築対象）の
    両方で使う共通の安全弁ロジック（新しい順に打ち切ることで、履歴が伸びても
    計算コストが際限なく増えないようにする）。打ち切りが発生した場合は
    `label` を含む警告ログを出す。
    """
    ordered = sorted(
        created_at_by_article_id,
        key=lambda article_id: (-created_at_by_article_id[article_id].timestamp(), article_id),
    )
    truncated_count = len(ordered) - limit
    if truncated_count > 0:
        logger.warning(
            "%sの記事数が上限を超えたため切り捨てました: total=%s limit=%s truncated_count=%s",
            label,
            len(ordered),
            limit,
            truncated_count,
        )
    return tuple(ordered[:limit])


@dataclass(frozen=True)
class WeightedInterestArticle:
    """関心プロファイル構築対象の記事 1 件（重み付き）。

    `recommendation.service.build_interest_profile`（Discover の関心プロファイル）と
    `rebuild_interest_clusters`（関心クラスタ再構築）が同じ対象・同じ重み計算を
    必要とするため、共有の値型として `load_weighted_interest_articles` が返す。
    """

    embedding: tuple[float, ...] | None
    topics: tuple[str, ...]
    weight: float


def load_weighted_interest_articles(
    session: Session, user_id: uuid.UUID, now: datetime
) -> tuple[WeightedInterestArticle, ...]:
    """ユーザーの関心記事に `effective_interest`（`PROJECT_SPEC.md` §8）で重みを付けて読み込む。

    `user_articles`（origin が manual/good/saved/read_full/clicked のいずれか）と
    `article_feedback`（action='good'）を突き合わせ、対象記事それぞれに重みを付ける。
    `recommendation.service.build_interest_profile`（Discover の関心プロファイル）と
    `rebuild_interest_clusters`（関心クラスタ再構築）の両方から呼ばれる共通処理（DRY）。

    `effective_interest = explicit_weight × feedback_weight × recency_decay ×
    confidence`（`interest/weights.py`）の各項の採用値:

    * `explicit_weight`: `user_articles.origin` から `explicit_weight_for_origin`
      で引く（手動登録 > Good > 保存 > 全文閲覧 > クリックの順、§7.1）。
      `article_feedback` にしか記録が無い記事（§7.1 手順 1 の反映前に読んだ
      場合の取りこぼし）は Good 相当（`ArticleOrigin.GOOD`）として扱う
    * `feedback_weight`: 常に `_NEUTRAL_FEEDBACK_WEIGHT`（1.0）に固定する。
      `_upsert_owned_user_article`（`api/feedback.py`）は同じ記事に複数の
      経路が重なった場合、最も重み（関心度）の強い経路の origin を残す
      仕様のため、`explicit_weight` は既にその記事で観測された最大の
      Good/Bad シグナルを反映済みである。ここでさらに
      `feedback_weights.good` 等を掛けると同じ Good シグナルを二重計上して
      しまうため、`feedback_weight` は現状プレースホルダとして 1.0 に
      固定し、二重計上を避ける
    * `recency_decay`: `compute_recency_decay(age_days, half_life_days)`。
      `age_days` は `now` と記録日時（`user_articles.created_at` /
      `article_feedback.created_at`、user_articles 側優先で下記と同じ規則）
      の差
    * `confidence`: `DEFAULT_CONFIDENCE`（1.0、実値算出は Issue #20 のスコープ）

    embedding が無い記事も戻り値には含める（`topics` は既知トピック集合の算出や
    クラスタのラベル付けに使えるため）。embedding が無い記事を対象から外すかどうか
    （KMeans の入力にできない等）は呼び出し側の判断に委ねる。
    """
    config = get_scoring_config()
    max_articles = config.interest.max_profile_articles
    feedback_weights = FeedbackWeights(
        manual=config.feedback_weights.manual,
        good=config.feedback_weights.good,
        save=config.feedback_weights.save,
        read_full=config.feedback_weights.read_full,
        clicked=config.feedback_weights.clicked,
        bad=config.feedback_weights.bad,
    )
    half_life_days = config.interest_decay.half_life_days

    user_article_rows = session.execute(
        select(UserArticle.article_id, UserArticle.origin, UserArticle.created_at).where(
            UserArticle.user_id == user_id,
            UserArticle.origin.in_(_INTEREST_ORIGIN_VALUES),
        )
    ).all()
    good_feedback_rows = session.execute(
        select(ArticleFeedback.article_id, ArticleFeedback.created_at).where(
            ArticleFeedback.user_id == user_id,
            ArticleFeedback.action == FeedbackAction.GOOD.value,
        )
    ).all()

    # article_feedback の action='good' は §7.1 手順 1 で user_articles にも
    # 追加されるため、通常は両方に同じ記事が現れる。user_articles 側の記録が
    # 優先されるよう後から上書きし、article_feedback にしか記録が無い場合の
    # 取りこぼしだけを補う（origin は Good 相当として扱う）。
    origin_by_article_id: dict[uuid.UUID, ArticleOrigin] = {}
    created_at_by_article_id: dict[uuid.UUID, datetime] = {}
    for article_id, created_at in good_feedback_rows:
        origin_by_article_id[article_id] = ArticleOrigin.GOOD
        created_at_by_article_id[article_id] = created_at
    for article_id, origin, created_at in user_article_rows:
        origin_by_article_id[article_id] = ArticleOrigin(origin)
        created_at_by_article_id[article_id] = created_at

    target_article_ids = order_by_recency_and_truncate(
        created_at_by_article_id, max_articles, "関心プロファイル構築対象"
    )
    articles = (
        session.scalars(select(Article).where(Article.id.in_(target_article_ids))).all()
        if target_article_ids
        else ()
    )

    result: list[WeightedInterestArticle] = []
    for article in articles:
        age_days = (now - created_at_by_article_id[article.id]).total_seconds() / _SECONDS_PER_DAY
        weight = compute_effective_interest(
            explicit_weight=explicit_weight_for_origin(
                origin_by_article_id[article.id], feedback_weights
            ),
            feedback_weight=_NEUTRAL_FEEDBACK_WEIGHT,
            recency_decay=compute_recency_decay(age_days, half_life_days),
            confidence=DEFAULT_CONFIDENCE,
        )
        result.append(
            WeightedInterestArticle(
                embedding=tuple(article.embedding) if article.embedding is not None else None,
                topics=tuple(article.topics),
                weight=weight,
            )
        )
    return tuple(result)


def _current_topic_weights(preference: UserTopicPreference | None) -> TopicWeights:
    """既存行から `TopicWeights` を組み立てる。行が無ければ全て 0（初期状態）。"""
    if preference is None:
        return TopicWeights(positive=0.0, negative=0.0, effective=0.0)
    return TopicWeights(
        positive=preference.positive_weight,
        negative=preference.negative_weight,
        effective=preference.effective_weight,
    )


def _upsert_topic_preference(
    session: Session, user_id: uuid.UUID, topic: str, weights: TopicWeights, now: datetime
) -> None:
    """`TopicWeights` を `user_topic_preferences` へ upsert する。

    事前の存在確認と INSERT の間の TOCTOU は `api/feedback.py` の
    `_upsert_feedback` と同じ理由で SAVEPOINT（`session.begin_nested`）に
    閉じ込めて吸収する。

    既存行の更新（UPDATE 分岐）は「読んでから上書き」であり、同じトピックを
    共有する別記事へのフィードバックが同時に来ると、一方の更新が他方の読み
    取った古い値を上書きして消えうる（lost update、Issue #15 自己レビュー
    5）。INSERT 側と異なり `SELECT ... FOR UPDATE` 等のロックは取っていない。

    `negative_weight`（`interest/topics.py` の `compute_negative_weight`）は
    「直近フィードバック集合から毎回導出し直す」設計のため、1 回分の更新が
    失われても次にこのトピックへフィードバックが来た時点で正しい値へ収束
    する（自己修復する）。一方 `positive_weight`（`increase_positive_weight`）
    は単純な累積加算のため、失われた分は自己修復せず永続的に欠落する。

    それでも原子的な更新（行ロック等）へは変更していない。本プロジェクトは
    単一ユーザー・ローカル実行が前提（`CLAUDE.md` の制約）であり、同じ
    トピックを共有する複数記事へのフィードバックが本当に同時に届く状況は
    通常発生しない（UI からの操作は 1 リクエストずつ順に発生する）ため、
    実害が小さいと判断した。
    """
    existing = session.get(UserTopicPreference, (user_id, topic))
    if existing is not None:
        existing.positive_weight = weights.positive
        existing.negative_weight = weights.negative
        existing.effective_weight = weights.effective
        existing.updated_at = now
        session.flush()
        return

    preference = UserTopicPreference(
        user_id=user_id,
        topic=topic,
        positive_weight=weights.positive,
        negative_weight=weights.negative,
        effective_weight=weights.effective,
        updated_at=now,
    )
    try:
        with session.begin_nested():
            session.add(preference)
    except IntegrityError as exc:
        if not is_unique_violation(exc):
            raise
        # 同時リクエストで既に他方が挿入済み。既存行を読み直して更新する。
        existing = session.get(UserTopicPreference, (user_id, topic))
        if existing is None:
            # 一意制約違反の直後のため理論上ここには来ない。到達した場合に
            # 原因を追えるよう対象を記録する（`_upsert_feedback` と同じ方針）。
            logger.error(
                "一意制約違反の後に対象のトピック選好を再取得できませんでした: user_id=%s topic=%s",
                user_id,
                topic,
            )
            raise
        existing.positive_weight = weights.positive
        existing.negative_weight = weights.negative
        existing.effective_weight = weights.effective
        existing.updated_at = now
        session.flush()


def _load_recent_topic_actions(
    session: Session, user_id: uuid.UUID, topic: str, limit: int
) -> tuple[FeedbackAction, ...]:
    """そのトピックに関する直近 `limit` 件のフィードバックを新しい順で読む。

    「そのトピックに関する」は記事の `topics`（JSONB 配列）に `topic` を含むかで
    判定する。PostgreSQL の JSONB containment 演算子（`@>`、SQLAlchemy の
    `Comparator.contains()`）を使い、SQL 側で `LIMIT` まで絞ってから読むため、
    Python 側へ全件ロードしない。
    """
    rows = session.execute(
        select(ArticleFeedback.action)
        .join(Article, Article.id == ArticleFeedback.article_id)
        .where(
            ArticleFeedback.user_id == user_id,
            Article.topics.contains([topic]),
        )
        .order_by(ArticleFeedback.created_at.desc(), ArticleFeedback.article_id.desc())
        .limit(limit)
    ).all()
    return tuple(FeedbackAction(action) for (action,) in rows)


def update_topic_preferences(
    session: Session,
    user_id: uuid.UUID,
    article_id: uuid.UUID,
    action: FeedbackAction,
    now: datetime,
) -> None:
    """フィードバックを対象記事のトピック単位の選好へ反映する（`PROJECT_SPEC.md` §7.1, §7.2）。

    対象は記事の `topics` のみとする（`technologies` は対象にしない）。
    `PROJECT_SPEC.md` §7.1 の Good の効果は「記事トピックの正の重みを増加」であり
    `technologies` には言及が無く、クラスタのラベル付け（`interest/clusters.py`）も
    トピックの頻度だけを見る設計のため、`technologies` を混ぜると選好とラベルの
    基準がずれてしまう。

    * Good / 保存: それぞれの `topics` の `positive_weight` を増やす
      （増分は `config/scoring.yaml` の `feedback_weights.good` / `save`、
      `interest/topics.py` の `increase_positive_weight`）。
    * Bad: `interest/topics.py` の `should_penalize_topic` の条件
      （同一トピックの直近 `topic_preference.recent_window` 件中
      `topic_preference.bad_threshold` 件以上が Bad）を満たしたときだけ、
      そのトピックの `negative_weight` を `decay_step` 分だけ増やす
      （`apply_bad_feedback`）。条件を満たさない場合は書き込みすら行わない
      （無用な行を作らない）。

    呼び出し側（`api/feedback.py`）は、この記事の `article_feedback` 行を
    upsert 済みの状態で呼ぶ想定にする。Bad の閾値判定はこの記事自身の
    フィードバックも含めた「直近 N 件」を見るため、先に upsert しておかないと
    今回の Bad が判定に含まれない。

    フィードバック取り消し（DELETE）からはこの関数を呼ばない。この関数は
    「新しいフィードバックが 1 件増えた」ことを前提に、その記事自身の
    `article_feedback` 行が既に upsert 済みの状態から差分を適用する設計
    のため、取り消し（行の削除）には向かない。取り消し時のトピック選好の
    再計算は `recompute_topic_preferences_after_removal` が別に担う
    （Issue #15 自己レビュー 1）。`negative_weight` はどちらの関数も
    `interest/topics.py` の `compute_negative_weight` を経由するため、
    増加方向（この関数）と取り消し後の再計算とで結果が食い違うことはない。

    記事が見つからない、または `topics` が空の場合は何もしない。
    """
    article = session.get(Article, article_id)
    if article is None or not article.topics:
        return

    config = get_scoring_config()

    if action in _POSITIVE_FEEDBACK_ACTIONS:
        increment = (
            config.feedback_weights.good
            if action is FeedbackAction.GOOD
            else config.feedback_weights.save
        )
        for topic in article.topics:
            current = _current_topic_weights(session.get(UserTopicPreference, (user_id, topic)))
            updated = increase_positive_weight(current, increment)
            _upsert_topic_preference(session, user_id, topic, updated, now)
        return

    if action is not FeedbackAction.BAD:
        return

    topic_preference_settings = _topic_preference_settings(config)
    for topic in article.topics:
        current = _current_topic_weights(session.get(UserTopicPreference, (user_id, topic)))
        recent_actions = _load_recent_topic_actions(
            session, user_id, topic, topic_preference_settings.recent_window
        )
        updated = apply_bad_feedback(current, recent_actions, topic_preference_settings)
        if updated == current:
            continue
        _upsert_topic_preference(session, user_id, topic, updated, now)


def _topic_preference_settings(config: ScoringConfig) -> TopicPreferenceSettings:
    """`get_scoring_config()` の `topic_preference` を `TopicPreferenceSettings` へ変換する。

    `update_topic_preferences` と `recompute_topic_preferences_after_removal`
    の両方が同じ変換を必要とするための共有ヘルパー（DRY）。
    """
    return TopicPreferenceSettings(
        recent_window=config.topic_preference.recent_window,
        bad_threshold=config.topic_preference.bad_threshold,
        decay_step=config.topic_preference.decay_step,
    )


def recompute_topic_preferences_after_removal(
    session: Session, user_id: uuid.UUID, article_id: uuid.UUID, now: datetime
) -> None:
    """フィードバック取り消し後にトピック選好を再計算する（`PROJECT_SPEC.md` §7.2）。

    Issue #15 自己レビュー 1。フィードバック取り消し
    （`DELETE /api/articles/{article_id}/feedback`）は、一度 Bad の閾値
    （直近 `recent_window` 件中 `bad_threshold` 件以上）を
    満たして `negative_weight` が上がった後にすべて取り消しても、それまで
    `update_topic_preferences` が増加方向にしか更新してこなかったため抑制が
    永続的に残ってしまう問題があった。この関数は「上がった分を差し引く」の
    ではなく、取り消し後の直近フィードバック集合から `negative_weight` を
    ゼロから再計算する（状態が履歴から一意に定まるようにするため、加算方式
    にはしない）。

    呼び出し側（`api/feedback.py` の `delete_article_feedback`）は、対象の
    `article_feedback` 行を削除した **後** に呼ぶこと。`_load_recent_topic_actions`
    が読む「直近フィードバック」から、削除対象自身が除外されている必要が
    あるため（先に削除しておかないと、取り消したはずのフィードバックが
    直近集合に残ったまま再計算されてしまう）。ORM の `session.delete()` は
    autoflush により後続の SELECT 前に反映されるが、呼び出し側では
    `session.flush()` を挟んで明示的に確定させてから呼ぶ想定にする。

    対象は削除した記事の `topics`。各トピックについて、削除後の直近
    フィードバック集合から `interest/topics.py` の `compute_negative_weight`
    で `negative_weight` を再計算し上書きする。`apply_bad_feedback`
    （Bad 追加時）と同じ `compute_negative_weight` を使うため、増加方向と
    取り消し後の再計算とで結果が食い違わない。

    `positive_weight` は据え置く（変更しない）。`negative_weight` は直近
    `recent_window` 件という限られた範囲から再計算できるが、`positive_weight`
    （`increase_positive_weight` による無制限の累積加算）を取り消し後の
    状態から一意に復元するには、そのトピックに関する全履歴（直近 N 件では
    なく）を洗い出して再集計する必要があり、この Issue 全体で使っている
    「直近 N 件で打ち切る」安全弁（`order_by_recency_and_truncate` 等）の
    設計から外れて件数無制限のクエリになってしまう。加えて Good（0.8）と
    保存（0.5）で増分が異なるため、どの行がどちらの増分だったかまで復元
    する必要があり、影響範囲が `negative_weight` の再計算より大きい。その
    ため本 Issue のスコープでは `positive_weight` は取り消しても変化させ
    ない。

    そのトピックのフィードバックが（取り消しの結果）1 件も無くなった場合も
    行は削除せず、`negative_weight` / `effective_weight` を 0 へ更新する
    に留める。`positive_weight` を据え置く方針と一貫させるため（行ごと
    消すと `positive_weight` の履歴も一緒に失われる）。また
    `user_topic_preferences` には他にレコード削除の運用が無く、この関数
    だけのために削除の分岐を新たに増やさないため。

    記事が見つからない、または `topics` が空の場合は何もしない
    （`update_topic_preferences` と同じ扱い）。
    """
    article = session.get(Article, article_id)
    if article is None or not article.topics:
        return

    config = get_scoring_config()
    topic_preference_settings = _topic_preference_settings(config)
    for topic in article.topics:
        current = _current_topic_weights(session.get(UserTopicPreference, (user_id, topic)))
        recent_actions = _load_recent_topic_actions(
            session, user_id, topic, topic_preference_settings.recent_window
        )
        new_negative = compute_negative_weight(recent_actions, topic_preference_settings)
        if new_negative == current.negative:
            continue
        updated = TopicWeights(
            positive=current.positive,
            negative=new_negative,
            effective=compute_effective_weight(current.positive, new_negative),
        )
        _upsert_topic_preference(session, user_id, topic, updated, now)


def _load_cluster_sources(
    session: Session, user_id: uuid.UUID, now: datetime
) -> tuple[ClusterSource, ...]:
    """関心クラスタ構築対象の記事を `ClusterSource` に変換する。

    `load_weighted_interest_articles` と同じ対象・同じ重み計算を再利用する
    （DRY）。embedding が無い記事は KMeans の入力にできないため除外する
    （`recommendation.service.build_interest_profile` が `weighted_embeddings` を
    組み立てる際の除外と同じ扱い）。
    """
    weighted_articles = load_weighted_interest_articles(session, user_id, now)
    return tuple(
        ClusterSource(embedding=record.embedding, topics=record.topics, weight=record.weight)
        for record in weighted_articles
        if record.embedding is not None
    )


def rebuild_interest_clusters(session: Session, user_id: uuid.UUID, now: datetime) -> int:
    """関心クラスタを再構築し、`user_interest_clusters` を置き換える（`PROJECT_SPEC.md` §8）。

    既存のその user の行を全削除してから新しいクラスタを挿入する（置き換え）。
    Good 連打のたびに全関心記事の embedding をクラスタリングすると API が遅くなる
    ため、`jobs/handlers/rebuild_interest_clusters.py` から非同期に呼ばれる想定で、
    `commit` はしない（呼び出し側に委ねる）。

    Returns:
        生成したクラスタ数。対象記事（embedding 付きの関心記事）が無ければ 0。
    """
    sources = _load_cluster_sources(session, user_id, now)
    config = get_scoring_config()
    clustering_settings = ClusteringSettings(
        min_clusters=config.clustering.min_clusters,
        max_clusters=config.clustering.max_clusters,
        min_articles_per_cluster=config.clustering.min_articles_per_cluster,
        label_topic_count=config.clustering.label_topic_count,
        random_state=config.clustering.random_state,
    )
    clusters = build_interest_clusters(sources, clustering_settings)

    session.execute(delete(UserInterestCluster).where(UserInterestCluster.user_id == user_id))
    for cluster in clusters:
        session.add(
            UserInterestCluster(
                user_id=user_id,
                label=cluster.label,
                weight=cluster.weight,
                topics=list(cluster.topics),
                centroid_embedding=list(cluster.centroid),
                updated_at=now,
            )
        )
    session.flush()
    return len(clusters)

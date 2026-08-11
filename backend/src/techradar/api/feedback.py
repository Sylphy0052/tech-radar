"""記事フィードバック API（`PROJECT_SPEC.md` §7, Issue #13, Issue #15）。

Good / Bad / 保存の意思表示を記録する。`article_feedback` は 1 ユーザー 1 記事に
つき 1 行で最新の意思表示を保持し（`db.models.ArticleFeedback`）、Good / 保存は
さらに `user_articles`（関心記事）へも反映する（`PROJECT_SPEC.md` §7.1 の重み表）。

Issue #15（関心プロファイル更新）により、フィードバックのたびにトピック単位の
選好（`user_topic_preferences`）を同期更新し、関心クラスタ（`user_interest_clusters`）
の再構築ジョブを積む。クラスタ再構築自体は同期実行しない（`_enqueue_interest_cluster_rebuild`
docstring 参照）。Issue #34 では同じ経路で情報源単位の選好
（`user_source_preferences`、`PROJECT_SPEC.md` §7.1 手順 4）も同期更新する。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from techradar.api.deps import get_current_user_id, get_now, get_session
from techradar.db.enums import ArticleOrigin, BadReason, FeedbackAction, JobType
from techradar.db.errors import is_unique_violation
from techradar.db.models import Article, ArticleFeedback, UserArticle
from techradar.interest.service import (
    recompute_source_preferences_after_removal,
    recompute_topic_preferences_after_removal,
    update_source_preferences,
    update_topic_preferences,
)
from techradar.jobs.queue import enqueue

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/articles", tags=["feedback"])

SessionDep = Annotated[Session, Depends(get_session)]
UserIdDep = Annotated[uuid.UUID, Depends(get_current_user_id)]
NowDep = Annotated[datetime, Depends(get_now)]

# Good / 保存の重みの初期値（`PROJECT_SPEC.md` §7.1）。手動登録（1.0）は
# `jobs/handlers/fetch_article.py` の `MANUAL_REGISTRATION_INTEREST_WEIGHT` で
# 別管理する（登録経路が異なり、このモジュールからは書き込まないため）。
GOOD_INTEREST_WEIGHT = 0.8
SAVED_INTEREST_WEIGHT = 0.5

# action=good / save が `user_articles` へ反映するときの (origin, interest_weight)。
_INTEREST_EFFECTS: dict[FeedbackAction, tuple[ArticleOrigin, float]] = {
    FeedbackAction.GOOD: (ArticleOrigin.GOOD, GOOD_INTEREST_WEIGHT),
    FeedbackAction.SAVE: (ArticleOrigin.SAVED, SAVED_INTEREST_WEIGHT),
}

# Bad やフィードバック取り消しのときに `user_articles` から取り除く origin。
# manual はユーザーが明示的に登録した記事であり、Good/Bad の意思表示とは
# 独立した関心事のため対象外にする。
_FEEDBACK_DERIVED_ORIGIN_VALUES = frozenset(
    origin.value for origin in (ArticleOrigin.GOOD, ArticleOrigin.SAVED)
)


class ArticleFeedbackCreate(BaseModel):
    """フィードバック登録リクエスト。"""

    model_config = ConfigDict(extra="forbid")

    action: FeedbackAction
    reason: BadReason | None = None

    @model_validator(mode="after")
    def _validate_reason_is_bad_only(self) -> ArticleFeedbackCreate:
        """`reason` は action=bad 専用の任意項目（`PROJECT_SPEC.md` §7.2）。

        action が bad 以外なのに reason が指定されていると、意味の無い reason が
        そのまま保存されてしまう（Issue #13 自己レビュー D）。
        """
        if self.action is not FeedbackAction.BAD and self.reason is not None:
            message = "reason は action=bad のときのみ指定できます"
            raise ValueError(message)
        return self


class ArticleFeedbackResponse(BaseModel):
    """フィードバックの公開表現。

    `action` / `reason` は enum 型にする。`str` のままだと OpenAPI に enum が出ず、
    生成される `frontend/src/lib/api-schema.d.ts` でも単なる `string` になり、
    フロント側の文字列リテラル比較が型で守られない（Issue #13 自己レビュー B）。
    DB の `action` / `reason` 列は text 型だが、`from_attributes=True` により
    pydantic がその文字列値から enum メンバーへ変換する。
    """

    model_config = ConfigDict(from_attributes=True)

    action: FeedbackAction
    reason: BadReason | None
    created_at: datetime


def _upsert_feedback(
    session: Session,
    user_id: uuid.UUID,
    article_id: uuid.UUID,
    payload: ArticleFeedbackCreate,
) -> ArticleFeedback:
    """`article_feedback` を upsert する。

    PK が (user_id, article_id) のため、既存行があれば `action` / `reason` を
    上書きし、最新の意思表示だけを 1 行で保持する。

    事前の存在確認（`session.get`）と INSERT の間には、同一記事への二重クリック
    等で別リクエストが割り込みうる（TOCTOU）。INSERT を SAVEPOINT
    （`session.begin_nested`）に閉じ込め、一意制約違反（`is_unique_violation`）
    だけを捕捉して既存行を読み直し、更新に切り替えて処理を続ける
    （`jobs/handlers/fetch_article.py` の `_link_user_article` と同じ方針）。
    `articles.py` の `_create_registration` はセッション全体を `rollback` するが、
    ここでは `_apply_user_article_effect` 側の書き込みがこの後に続くため、
    SAVEPOINT だけを巻き戻し、その書き込みを道連れにしないようにする。
    """
    reason_value = payload.reason.value if payload.reason is not None else None
    feedback = session.get(ArticleFeedback, (user_id, article_id))
    if feedback is not None:
        feedback.action = payload.action.value
        feedback.reason = reason_value
        session.flush()
        return feedback

    feedback = ArticleFeedback(
        user_id=user_id,
        article_id=article_id,
        action=payload.action.value,
        reason=reason_value,
    )
    try:
        with session.begin_nested():
            session.add(feedback)
    except IntegrityError as exc:
        if not is_unique_violation(exc):
            raise
        # 同時リクエストで既に他方が挿入済み。既存行を読み直して更新する。
        feedback = session.get(ArticleFeedback, (user_id, article_id))
        if feedback is None:
            # 一意制約違反の直後のため理論上ここには来ない。到達した場合は
            # 元の IntegrityError のまま 500 になるので、原因を追えるように
            # 対象を記録しておく。
            logger.error(
                "一意制約違反の後に対象のフィードバックを再取得できませんでした: article_id=%s",
                article_id,
            )
            raise
        feedback.action = payload.action.value
        feedback.reason = reason_value
        session.flush()
    return feedback


def _upsert_owned_user_article(
    session: Session,
    user_id: uuid.UUID,
    article_id: uuid.UUID,
    origin: ArticleOrigin,
    interest_weight: float,
) -> None:
    """Good / 保存を `user_articles` へ反映する。

    `user_articles` は (user_id, article_id) が一意で origin を 1 行でしか
    持てない。複数の経路（手動登録・Good・保存等）が同じ記事に辿り着いた場合、
    最も重み（関心度）の強い経路を残す方針にする（`PROJECT_SPEC.md` §7.1）。
    例えば手動登録（1.0）済みの記事に Good（0.8）しても、origin・重みとも
    手動登録のまま維持する。

    事前の存在確認と INSERT の間の TOCTOU は `_upsert_feedback` と同じ理由で
    SAVEPOINT に閉じ込めて吸収する。
    """
    existing = session.scalar(
        select(UserArticle).where(
            UserArticle.user_id == user_id, UserArticle.article_id == article_id
        )
    )
    if existing is None:
        try:
            with session.begin_nested():
                session.add(
                    UserArticle(
                        user_id=user_id,
                        article_id=article_id,
                        origin=origin.value,
                        interest_weight=interest_weight,
                    )
                )
        except IntegrityError as exc:
            if not is_unique_violation(exc):
                raise
            # 同時リクエストで既に他方が挿入済み。既存行を読み直して続行する。
            existing = session.scalar(
                select(UserArticle).where(
                    UserArticle.user_id == user_id, UserArticle.article_id == article_id
                )
            )
            if existing is None:
                # 一意制約違反の直後のため理論上ここには来ない。`_upsert_feedback`
                # と同じく、到達した場合に原因を追えるよう対象を記録する。
                logger.error(
                    "一意制約違反の後に対象の関心記事を再取得できませんでした: article_id=%s",
                    article_id,
                )
                raise
        else:
            return

    if existing.interest_weight >= interest_weight:
        return
    existing.origin = origin.value
    existing.interest_weight = interest_weight


def _remove_feedback_derived_user_article(
    session: Session, user_id: uuid.UUID, article_id: uuid.UUID
) -> None:
    """Good / 保存に由来する `user_articles` 行を削除する。

    手動登録（origin=manual）の行は、ユーザーが明示的に登録した記事のため、
    Bad やフィードバック削除の影響を受けず残す。
    """
    session.execute(
        delete(UserArticle).where(
            UserArticle.user_id == user_id,
            UserArticle.article_id == article_id,
            UserArticle.origin.in_(_FEEDBACK_DERIVED_ORIGIN_VALUES),
        )
    )


def _apply_user_article_effect(
    session: Session, user_id: uuid.UUID, article_id: uuid.UUID, action: FeedbackAction
) -> None:
    """フィードバックの `user_articles` への副作用を適用する。"""
    effect = _INTEREST_EFFECTS.get(action)
    if effect is not None:
        origin, interest_weight = effect
        _upsert_owned_user_article(session, user_id, article_id, origin, interest_weight)
        return
    # action=bad: Good/保存由来の関心記事としての採用を取り消す。
    _remove_feedback_derived_user_article(session, user_id, article_id)


def _enqueue_interest_cluster_rebuild(session: Session, user_id: uuid.UUID) -> None:
    """関心クラスタ再構築ジョブ（`rebuild_interest_clusters`）を積む（Issue #15）。

    Good 連打のたびに全関心記事の embedding をクラスタリングすると API が
    遅くなるため、同期実行せずジョブとして積むだけに留める（実際の再構築は
    `jobs/handlers/rebuild_interest_clusters.py` が非同期に行う）。

    同じ user の未処理（pending）のジョブが既にあれば積み増さない。重複抑制は
    「pending 存在確認 → INSERT」という事前確認ではなく、DB 側の部分ユニーク
    インデックス（`ux_jobs_active_rebuild_interest_clusters`、payload の
    user_id ごとに1件、`migrations/versions/..._add_unique_index_for_active_
    rebuild_.py`）を使う。`ux_jobs_active_crawl_sources`（Issue #26）と同じ
    理由で、事前の SELECT だけでは同時に届いた2件のフィードバックがどちらも
    「まだ無い」と判断して両方 INSERT してしまう TOCTOU レースをアプリの
    コードだけでは塞げないため。ここでは事前確認すら行わず、まず INSERT を
    試み、一意制約違反を捕捉して何もしない（既に同じ user の再構築ジョブが
    積まれているとみなせる）。

    INSERT は SAVEPOINT（`session.begin_nested`）に閉じ込め、違反時もこの
    呼び出しより前の変更（フィードバック本体・トピック選好の更新）を道連れに
    しない（`_upsert_feedback` と同じ方針）。
    """
    try:
        with session.begin_nested():
            enqueue(session, JobType.REBUILD_INTEREST_CLUSTERS, {"user_id": str(user_id)})
    except IntegrityError as exc:
        if not is_unique_violation(exc):
            raise
        logger.info("rebuild_interest_clusters.enqueue_skipped_already_pending user_id=%s", user_id)


@router.post("/{article_id}/feedback", response_model=ArticleFeedbackResponse)
def create_article_feedback(
    article_id: uuid.UUID,
    payload: ArticleFeedbackCreate,
    session: SessionDep,
    user_id: UserIdDep,
    now: NowDep,
) -> ArticleFeedback:
    """記事へ Good / Bad / 保存を記録する（`PROJECT_SPEC.md` §7）。

    合わせてトピック単位の選好（`user_topic_preferences`）を同期更新し
    （`update_topic_preferences`）、関心クラスタの再構築ジョブを積む
    （`_enqueue_interest_cluster_rebuild`、Issue #15）。
    """
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="記事が見つかりません")

    feedback = _upsert_feedback(session, user_id, article_id, payload)
    _apply_user_article_effect(session, user_id, article_id, payload.action)
    # トピック選好の Bad 判定は、この記事自身のフィードバックも含めた「直近 N 件」を
    # 見る（`update_topic_preferences` の docstring 参照）ため、必ず
    # `_upsert_feedback` の後に呼ぶ。
    update_topic_preferences(session, user_id, article_id, payload.action, now)
    # 情報源選好も同じ理由（Bad の閾値判定に今回のフィードバックを含める）で
    # `_upsert_feedback` の後に呼ぶ（Issue #34）。
    update_source_preferences(session, user_id, article_id, payload.action, now)
    _enqueue_interest_cluster_rebuild(session, user_id)
    session.commit()
    return feedback


@router.delete("/{article_id}/feedback", status_code=status.HTTP_204_NO_CONTENT)
def delete_article_feedback(
    article_id: uuid.UUID,
    session: SessionDep,
    user_id: UserIdDep,
    now: NowDep,
) -> Response:
    """記事へのフィードバックを取り消す。

    Good / 保存に由来する `user_articles` 行も合わせて削除する
    （手動登録由来の行は残す）。関心記事の構成が変わりうるため、関心クラスタの
    再構築ジョブも積む。

    トピック単位の選好（`user_topic_preferences`）は、取り消し後の直近
    フィードバック集合から `negative_weight` を再計算する
    （`recompute_topic_preferences_after_removal`、Issue #15 自己レビュー 1）。
    `update_topic_preferences`（POST 側）が使う増加方向のみの更新関数は
    取り消しには使わない（「上がった分を差し引く」処理を持たないため）。

    呼び出し順序が重要: `session.delete(feedback)` の後に
    `session.flush()` で DELETE を確定させてから
    `recompute_topic_preferences_after_removal` を呼ぶ。先に確定させないと、
    再計算が読む「直近フィードバック」に削除対象自身が残ったままになり、
    取り消したはずのフィードバックが再計算結果に混入してしまう
    （`recompute_topic_preferences_after_removal` の docstring 参照）。
    """
    feedback = session.get(ArticleFeedback, (user_id, article_id))
    if feedback is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="フィードバックが見つかりません"
        )

    session.delete(feedback)
    session.flush()
    recompute_topic_preferences_after_removal(session, user_id, article_id, now)
    recompute_source_preferences_after_removal(session, user_id, article_id, now)
    _remove_feedback_derived_user_article(session, user_id, article_id)
    _enqueue_interest_cluster_rebuild(session, user_id)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

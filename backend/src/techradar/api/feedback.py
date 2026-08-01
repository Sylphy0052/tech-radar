"""記事フィードバック API（`PROJECT_SPEC.md` §7, Issue #13）。

Good / Bad / 保存の意思表示を記録する。`article_feedback` は 1 ユーザー 1 記事に
つき 1 行で最新の意思表示を保持し（`db.models.ArticleFeedback`）、Good / 保存は
さらに `user_articles`（関心記事）へも反映する（`PROJECT_SPEC.md` §7.1 の重み表）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from techradar.api.deps import get_current_user_id, get_session
from techradar.db.enums import ArticleOrigin, BadReason, FeedbackAction
from techradar.db.models import Article, ArticleFeedback, UserArticle

router = APIRouter(prefix="/api/articles", tags=["feedback"])

SessionDep = Annotated[Session, Depends(get_session)]
UserIdDep = Annotated[uuid.UUID, Depends(get_current_user_id)]

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


class ArticleFeedbackResponse(BaseModel):
    """フィードバックの公開表現。"""

    model_config = ConfigDict(from_attributes=True)

    action: str
    reason: str | None
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
    """
    reason_value = payload.reason.value if payload.reason is not None else None
    feedback = session.get(ArticleFeedback, (user_id, article_id))
    if feedback is None:
        feedback = ArticleFeedback(
            user_id=user_id,
            article_id=article_id,
            action=payload.action.value,
            reason=reason_value,
        )
        session.add(feedback)
    else:
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
    """
    existing = session.scalar(
        select(UserArticle).where(
            UserArticle.user_id == user_id, UserArticle.article_id == article_id
        )
    )
    if existing is None:
        session.add(
            UserArticle(
                user_id=user_id,
                article_id=article_id,
                origin=origin.value,
                interest_weight=interest_weight,
            )
        )
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


@router.post("/{article_id}/feedback", response_model=ArticleFeedbackResponse)
def create_article_feedback(
    article_id: uuid.UUID,
    payload: ArticleFeedbackCreate,
    session: SessionDep,
    user_id: UserIdDep,
) -> ArticleFeedback:
    """記事へ Good / Bad / 保存を記録する（`PROJECT_SPEC.md` §7）。"""
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="記事が見つかりません")

    feedback = _upsert_feedback(session, user_id, article_id, payload)
    _apply_user_article_effect(session, user_id, article_id, payload.action)
    session.commit()
    return feedback


@router.delete("/{article_id}/feedback", status_code=status.HTTP_204_NO_CONTENT)
def delete_article_feedback(
    article_id: uuid.UUID,
    session: SessionDep,
    user_id: UserIdDep,
) -> Response:
    """記事へのフィードバックを取り消す。

    Good / 保存に由来する `user_articles` 行も合わせて削除する
    （手動登録由来の行は残す）。
    """
    feedback = session.get(ArticleFeedback, (user_id, article_id))
    if feedback is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="フィードバックが見つかりません"
        )

    session.delete(feedback)
    _remove_feedback_derived_user_article(session, user_id, article_id)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

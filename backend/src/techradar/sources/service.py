"""公式ソースレジストリの DB 連携（`PROJECT_SPEC.md` §11）。

判定規則の実体は DB（`source_registry`）に置く。設定ファイルは初期値の供給元で、
運用中の修正は `PATCH /api/sources/{id}` で DB 側へ入れる。

判定のたびに DB から規則を読み直す。行数は多くても数百で、キャッシュを持つと
修正が反映されない不具合を招きやすいため（受入基準「修正が以降の判定に反映される」）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from techradar.db import SourceRegistry
from techradar.db.enums import SourceType
from techradar.sources.classifier import classify
from techradar.sources.config import RegistryConfig
from techradar.sources.rules import SourceClassification, SourceRule

logger = logging.getLogger(__name__)

MIN_AUTHORITY_SCORE = 0.0
MAX_AUTHORITY_SCORE = 1.0


@dataclass(frozen=True)
class SeedResult:
    """シードの結果。"""

    created: int
    updated: int
    skipped_verified: int


def _rule_key(
    domain: str, path_pattern: str | None, github_org: str | None
) -> tuple[str, str, str]:
    """規則の同一性を決めるキー。

    大文字小文字の違いで重複行が生まれないよう正規化する。
    """
    return (
        domain.strip().lower(),
        (path_pattern or "").strip(),
        (github_org or "").strip().lower(),
    )


def seed_source_registry(session: Session, config: RegistryConfig) -> SeedResult:
    """設定ファイルの規則を `source_registry` へ投入する。

    冪等に動く。既存行のうち `verified` が立っているものは手動確認済みとして
    上書きしない。誤判定を手で直した内容を、起動のたびに巻き戻さないため。

    Raises:
        ValueError: 設定ファイル内に同じ規則が重複している場合。
    """
    rules = config.to_rules()
    _assert_no_duplicates(rules)

    existing = {
        _rule_key(row.domain, row.path_pattern, row.github_org): row
        for row in session.scalars(select(SourceRegistry)).all()
    }

    created = 0
    updated = 0
    skipped = 0
    for rule in rules:
        row = existing.get(_rule_key(rule.domain, rule.path_pattern, rule.github_org))
        if row is None:
            session.add(
                SourceRegistry(
                    entity_name=rule.entity_name,
                    domain=rule.domain,
                    path_pattern=rule.path_pattern,
                    github_org=rule.github_org,
                    source_type=rule.source_type.value,
                    authority_score=rule.authority_score,
                    verified=False,
                )
            )
            created += 1
            continue
        if row.verified:
            skipped += 1
            continue
        if _apply_rule(row, rule):
            updated += 1

    session.flush()
    return SeedResult(created=created, updated=updated, skipped_verified=skipped)


def _assert_no_duplicates(rules: tuple[SourceRule, ...]) -> None:
    """設定ファイル内の重複を検出する。

    DB の一意制約に任せると、どの規則が衝突したのか分からないまま失敗する。
    """
    seen: set[tuple[str, str, str]] = set()
    for rule in rules:
        key = _rule_key(rule.domain, rule.path_pattern, rule.github_org)
        if key in seen:
            message = (
                "ソースレジストリ設定に重複した規則があります: "
                f"domain={rule.domain} path={rule.path_pattern} github_org={rule.github_org}"
            )
            raise ValueError(message)
        seen.add(key)


def _apply_rule(row: SourceRegistry, rule: SourceRule) -> bool:
    """設定側の値を行へ反映する。変更があれば True を返す。"""
    changed = (
        row.entity_name != rule.entity_name
        or row.source_type != rule.source_type.value
        or row.authority_score != rule.authority_score
    )
    if not changed:
        return False
    row.entity_name = rule.entity_name
    row.source_type = rule.source_type.value
    row.authority_score = rule.authority_score
    return True


def load_rules(session: Session) -> tuple[SourceRule, ...]:
    """DB から判定規則を読み込む。

    列挙外の種別や範囲外の authority を持つ行は読み飛ばす。1 行の不備で
    判定全体を止めないため。読み飛ばしは警告として残す。
    """
    rows = session.scalars(select(SourceRegistry)).all()
    rules: list[SourceRule] = []
    for row in rows:
        rule = _to_rule(row)
        if rule is None:
            continue
        rules.append(rule)
    return tuple(rules)


def _to_rule(row: SourceRegistry) -> SourceRule | None:
    """DB の行を判定規則へ変換する。不正な行は None を返す。"""
    try:
        source_type = SourceType(row.source_type)
    except ValueError:
        logger.warning(
            "source_registry に未知の source_type があります: id=%s domain=%s source_type=%s",
            row.id,
            row.domain,
            row.source_type,
        )
        return None

    if not MIN_AUTHORITY_SCORE <= row.authority_score <= MAX_AUTHORITY_SCORE:
        logger.warning(
            "source_registry の authority_score が範囲外です: id=%s domain=%s score=%s",
            row.id,
            row.domain,
            row.authority_score,
        )
        return None

    return SourceRule(
        entity_name=row.entity_name,
        domain=row.domain,
        source_type=source_type,
        authority_score=row.authority_score,
        path_pattern=row.path_pattern,
        github_org=row.github_org,
        verified=row.verified,
    )


def classify_with_registry(
    session: Session,
    url: str,
    config: RegistryConfig,
) -> SourceClassification:
    """DB のレジストリと設定ファイルの推定規則で URL を分類する。

    レジストリは DB を正とし、未登録ドメインの推定と authority の重みは
    設定ファイルを使う。
    """
    return classify(url, load_rules(session), config.to_fallback_config(), config.to_weights())

"""重複排除の DB 反映を検証する結合テスト（`PROJECT_SPEC.md` §17 受入基準）。

LLM は `FakeLLMProvider` に差し替えて呼ぶ。Embedding は使わない（対象の判定は
canonical URL・正規化 URL・タイトル・本文ハッシュだけで再現できるため）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from techradar.db import Article
from techradar.db.enums import ContentType, SourceType
from techradar.dedup import service as dedup_service
from techradar.dedup.config import LimitsConfig, get_dedup_config
from techradar.dedup.rules import ArticleCluster, ArticleSignature
from techradar.dedup.service import (
    MAX_LOGGED_VALUE_LENGTH,
    _best_match_for,
    _needs_unique_value_judgment,
    _sanitize_for_log,
    _to_signature,
    deduplicate_articles,
)
from techradar.llm import FakeLLMProvider

DUMMY_LLM_RESPONSE = [{"has_unique_value": False, "reason": "公式発表の要約に過ぎない"}]


def capture_warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`dedup.service.logger.warning` の呼び出し内容を記録する。

    `migrations/env.py` は alembic 標準の `fileConfig` を既定
    （`disable_existing_loggers=True`）で呼ぶ。これは呼び出し時点で
    存在する（かつ ini に明示されていない）ロガーを disabled にする仕様のため、
    DB を使うテストが 1 件でも先に実行されると、モジュール import 時に
    生成済みの `techradar.dedup.service` のロガーがプロセス内でずっと
    disabled になり、`caplog` では以降検知できなくなる。ロガーの
    `warning` メソッドを直接差し替えて disabled の影響を受けずに呼び出し
    内容を拾う。
    """
    messages: list[str] = []

    def _warning(message: str, *args: object, **kwargs: object) -> None:
        messages.append(message % args if args else message)

    monkeypatch.setattr(dedup_service.logger, "warning", _warning)
    return messages


def no_sleep(_seconds: float) -> None:
    """バックオフを待たない。テストを実時間で遅くしないため。"""


class _Unset:
    """`published_at` の「未指定」を表す番兵。

    `None`（NULL を明示したい）と「指定しなかった（既定値の現在時刻を使う）」を
    区別するために使う。
    """


_UNSET_PUBLISHED_AT = _Unset()


def make_article(
    session: Session,
    *,
    title: str = "記事タイトル",
    canonical_url: str | None = None,
    original_url: str | None = None,
    body: str | None = None,
    body_hash: str | None = None,
    source_authority: float = 0.5,
    source_type: SourceType | None = None,
    content_type: ContentType | None = None,
    technical_quality: float = 0.5,
    published_at: datetime | _Unset | None = _UNSET_PUBLISHED_AT,
    fetched_at: datetime | None = None,
    is_dead: bool = False,
) -> Article:
    """判定対象記事を DB へ保存する。指定しない項目は無難な既定値にする。

    `published_at=None` を明示的に渡せば NULL（日付を取得できなかった記事）を
    再現できる。未指定なら対象期間内に収まる現在時刻を使う。
    """
    resolved_canonical = canonical_url or f"https://example.com/{uuid.uuid4().hex[:10]}"
    resolved_published_at = datetime.now(UTC) if isinstance(published_at, _Unset) else published_at
    article = Article(
        canonical_url=resolved_canonical,
        original_url=original_url or resolved_canonical,
        title=title,
        body=body,
        body_hash=body_hash,
        source_domain="example.com",
        source_authority=source_authority,
        source_type=source_type.value if source_type is not None else None,
        content_type=content_type.value if content_type is not None else None,
        technical_quality=technical_quality,
        published_at=resolved_published_at,
        fetched_at=fetched_at or datetime.now(UTC),
        is_dead=is_dead,
    )
    session.add(article)
    session.flush()
    return article


class TestExactContentDuplicate:
    def test_marks_the_lower_authority_article_as_a_full_penalty_duplicate(
        self, db_session: Session
    ):
        # Arrange — 受入基準: canonical URL が一致する記事は片方に
        # duplicate_of_article_id が付き duplicate_penalty が満額になる。
        #
        # `articles.canonical_url` には DB の一意制約があり（`db/models.py`）、
        # 同一 canonical_url を持つ 2 行は物理的に作れない
        # （取得層が canonical_url で upsert する前提のため）。
        # この一致段の判定自体は `tests/test_dedup_rules.py` が純粋関数として
        # 検証済みなので、ここでは DB 上で再現できる「本文ハッシュ一致」
        # （ミラー掲載など canonical_url が異なる完全一致コンテンツ）で
        # 同じ「満額減点」の反映を確認する。penalties.body_hash も
        # penalties.canonical_url と同じ満額（1.0）に設定されている。
        shared_body_hash = "identical-content-hash"
        official = make_article(
            db_session,
            title="記事A",
            source_authority=0.9,
            body_hash=shared_body_hash,
        )
        mirror = make_article(
            db_session,
            title="記事B",
            source_authority=0.3,
            body_hash=shared_body_hash,
        )
        provider = FakeLLMProvider(DUMMY_LLM_RESPONSE)
        expected_penalty = get_dedup_config().to_penalties().body_hash

        # Act
        deduplicate_articles(db_session, provider, sleep=no_sleep)

        # Assert
        assert official.duplicate_of_article_id is None
        assert official.duplicate_penalty == 0.0
        assert mirror.duplicate_of_article_id == official.id
        assert mirror.duplicate_penalty == expected_penalty


class TestTrackingParameterDuplicate:
    def test_treats_urls_differing_only_by_tracking_parameters_as_duplicate(
        self, db_session: Session
    ):
        # Arrange — 受入基準: トラッキングパラメータ違いの同一記事が重複と判定される
        primary = make_article(
            db_session,
            title="発表内容の詳細",
            canonical_url="https://example.com/article?utm_source=twitter",
            source_authority=0.9,
        )
        secondary = make_article(
            db_session,
            title="発表内容の詳細",
            canonical_url="https://example.com/article?utm_source=newsletter",
            source_authority=0.3,
        )
        provider = FakeLLMProvider(DUMMY_LLM_RESPONSE)
        expected_penalty = get_dedup_config().to_penalties().normalized_url

        # Act
        deduplicate_articles(db_session, provider, sleep=no_sleep)

        # Assert
        assert secondary.duplicate_of_article_id == primary.id
        assert secondary.duplicate_penalty == expected_penalty


class TestTitleRepostDuplicate:
    def test_treats_a_repost_with_an_almost_identical_title_as_duplicate(self, db_session: Session):
        # Arrange — 受入基準: タイトルがほぼ同一の転載記事が重複と判定される
        official = make_article(
            db_session,
            title="アンソロピックが新しいAIモデルを発表しました",
            canonical_url="https://official.example/post",
            source_authority=0.9,
            source_type=SourceType.OFFICIAL_BLOG,
        )
        repost = make_article(
            db_session,
            title="［転載］アンソロピックが新しいAIモデルを発表しました",
            canonical_url="https://repost.example/mirror/post",
            source_authority=0.2,
            source_type=SourceType.NEWS_REPOST,
        )
        provider = FakeLLMProvider(DUMMY_LLM_RESPONSE)

        # Act
        deduplicate_articles(db_session, provider, sleep=no_sleep)

        # Assert
        assert repost.duplicate_of_article_id == official.id


class TestOfficialArticleWinsRepresentative:
    def test_selects_the_official_article_as_representative_over_a_repost(
        self, db_session: Session
    ):
        # Arrange — 受入基準: 公式記事 (source_authority 高) が代表になる。
        # タイトル類似度 (閾値 0.90) を超えるよう、接頭辞の付与が全体長に対して
        # 十分短くなる長さのタイトルにする（`test_dedup_rules.py` と同じ配慮）。
        official = make_article(
            db_session,
            title="アンソロピックが新しいAIモデルを発表しました",
            canonical_url="https://official.example/announcement",
            source_authority=0.9,
            source_type=SourceType.OFFICIAL_BLOG,
        )
        repost = make_article(
            db_session,
            title="【転載】アンソロピックが新しいAIモデルを発表しました",
            canonical_url="https://repost.example/mirror/announcement",
            source_authority=0.2,
            source_type=SourceType.NEWS_REPOST,
        )
        provider = FakeLLMProvider(DUMMY_LLM_RESPONSE)

        # Act
        deduplicate_articles(db_session, provider, sleep=no_sleep)

        # Assert
        assert official.duplicate_of_article_id is None
        assert repost.duplicate_of_article_id == official.id


class TestUniqueValueArticleStaysSeparate:
    def test_keeps_an_analysis_article_with_unique_value_as_a_separate_article(
        self, db_session: Session
    ):
        # Arrange — 受入基準: 独自検証を含む解説記事 (LLM が has_unique_value: true)
        # は duplicate_of_article_id を付けられずに残る
        official = make_article(
            db_session,
            title="新製品について解説する記事",
            canonical_url="https://official.example/post",
            source_authority=0.9,
            body="公式発表の内容をそのまま紹介する記事。",
        )
        analysis = make_article(
            db_session,
            title="新製品について解説する記事",
            canonical_url="https://blogger.example/analysis",
            source_authority=0.65,
            content_type=ContentType.IMPLEMENTATION,
            technical_quality=0.85,
            body="実際に触って独自に計測したベンチマーク結果とコードを掲載する記事。",
        )
        provider = FakeLLMProvider(
            [{"has_unique_value": True, "reason": "独自のベンチマーク結果とコードがある"}]
        )

        # Act
        result = deduplicate_articles(db_session, provider, sleep=no_sleep)

        # Assert
        assert official.duplicate_of_article_id is None
        assert analysis.duplicate_of_article_id is None
        assert analysis.duplicate_penalty == 0.0
        assert result.llm_call_count == 1
        assert len(provider.calls) == 1


class TestUniqueValueCandidateCap:
    def test_does_not_call_the_llm_more_than_the_configured_candidate_limit(
        self, db_session: Session
    ):
        # Arrange — 受入基準: LLM 呼び出し回数がコスト管理の上限を超えない
        max_candidates = get_dedup_config().to_unique_value_settings().max_candidates_per_cluster
        shared_body_hash = "shared-hash-for-clustering"
        official = make_article(
            db_session,
            title="公式発表記事",
            source_authority=0.9,
            body_hash=shared_body_hash,
        )
        candidates = [
            make_article(
                db_session,
                title=f"解説記事{index}",
                source_authority=0.65,
                content_type=ContentType.IMPLEMENTATION,
                technical_quality=quality,
                body_hash=shared_body_hash,
            )
            for index, quality in enumerate((0.95, 0.85, 0.75), start=1)
        ]
        provider = FakeLLMProvider(DUMMY_LLM_RESPONSE)

        # Act
        result = deduplicate_articles(db_session, provider, sleep=no_sleep)

        # Assert
        assert len(provider.calls) == max_candidates
        assert result.llm_call_count == max_candidates
        # 上限に収まらなかった最下位候補（technical_quality 最小）は
        # LLM を呼ばれず、そのまま重複として畳まれる
        assert candidates[-1].duplicate_of_article_id == official.id


class TestIdempotency:
    def test_produces_the_same_result_when_run_twice(self, db_session: Session):
        # Arrange — 受入基準: 再実行しても結果が変わらない
        shared_body_hash = "identical-content-hash"
        official = make_article(
            db_session, title="記事A", source_authority=0.9, body_hash=shared_body_hash
        )
        mirror = make_article(
            db_session, title="記事B", source_authority=0.3, body_hash=shared_body_hash
        )
        provider = FakeLLMProvider(DUMMY_LLM_RESPONSE)

        # Act
        first = deduplicate_articles(db_session, provider, sleep=no_sleep)
        second = deduplicate_articles(db_session, provider, sleep=no_sleep)

        # Assert
        assert first == second
        assert official.duplicate_of_article_id is None
        assert official.duplicate_penalty == 0.0
        assert mirror.duplicate_of_article_id == official.id
        assert mirror.duplicate_penalty == get_dedup_config().to_penalties().body_hash


class TestMissingPublishedAtUsesFetchedAt:
    """`published_at` が NULL の記事も `fetched_at` で対象期間を判定する。

    RSS/HTML から公開日を取得できなかった記事（まとめ・転載系サイトに多い）は
    `published_at` が NULL になる。`fetched_at` で代替しないと、まさに重複判定
    したい記事が対象からごっそり漏れる。
    """

    def test_includes_an_article_with_no_published_at_when_fetched_at_is_within_the_window(
        self, db_session: Session
    ):
        # Arrange
        shared_body_hash = "identical-content-hash"
        official = make_article(
            db_session, title="記事A", source_authority=0.9, body_hash=shared_body_hash
        )
        undated = make_article(
            db_session,
            title="記事B",
            source_authority=0.3,
            body_hash=shared_body_hash,
            published_at=None,
            fetched_at=datetime.now(UTC),
        )
        provider = FakeLLMProvider(DUMMY_LLM_RESPONSE)

        # Act
        result = deduplicate_articles(db_session, provider, sleep=no_sleep)

        # Assert
        assert result.processed_articles == 2
        assert undated.duplicate_of_article_id == official.id

    def test_excludes_an_article_with_no_published_at_when_fetched_at_is_outside_the_window(
        self, db_session: Session
    ):
        # Arrange — fetched_at も対象期間より古い記事は、これまで通り除外される
        shared_body_hash = "identical-content-hash"
        official = make_article(
            db_session, title="記事A", source_authority=0.9, body_hash=shared_body_hash
        )
        old_undated = make_article(
            db_session,
            title="記事B",
            source_authority=0.3,
            body_hash=shared_body_hash,
            published_at=None,
            fetched_at=datetime.now(UTC) - timedelta(days=30),
        )
        provider = FakeLLMProvider(DUMMY_LLM_RESPONSE)

        # Act
        result = deduplicate_articles(db_session, provider, sleep=no_sleep)

        # Assert — 対象は official だけになり、期間外の記事とは比較すらされない
        assert result.processed_articles == 1
        assert official.duplicate_of_article_id is None
        assert old_undated.duplicate_of_article_id is None


class TestArticleToSignatureConversion:
    """`_to_signature` の変換規則を検証する（DB は enum を強制しない text 列）。"""

    def test_maps_an_unknown_source_type_to_unknown_instead_of_raising(self):
        # Arrange — DB 上は自由文字列のため、列挙外の値が紛れることがある
        article = Article(
            id=uuid.uuid4(),
            canonical_url="https://example.com/a",
            original_url="https://example.com/a",
            title="タイトル",
            source_domain="example.com",
            source_authority=0.5,
            source_type="not-a-real-source-type",
            technical_quality=0.5,
        )

        # Act
        signature = _to_signature(article)

        # Assert — 不正な値は落とさず UNKNOWN へ寄せる
        assert signature.source_type == SourceType.UNKNOWN

    def test_maps_a_missing_content_type_to_none(self):
        # Arrange — 未解析でまだ content_type が入っていない記事
        article = Article(
            id=uuid.uuid4(),
            canonical_url="https://example.com/b",
            original_url="https://example.com/b",
            title="タイトル",
            source_domain="example.com",
            source_authority=0.5,
            technical_quality=0.5,
        )

        # Act
        signature = _to_signature(article)

        # Assert
        assert signature.content_type is None

    def test_maps_an_unknown_content_type_to_none_instead_of_raising(self):
        # Arrange — DB 上は自由文字列のため、列挙外の値が紛れることがある
        article = Article(
            id=uuid.uuid4(),
            canonical_url="https://example.com/c",
            original_url="https://example.com/c",
            title="タイトル",
            source_domain="example.com",
            source_authority=0.5,
            content_type="not-a-real-content-type",
            technical_quality=0.5,
        )

        # Act
        signature = _to_signature(article)

        # Assert — 不正な値は落とさず None へ寄せる
        assert signature.content_type is None

    def test_converts_the_embedding_list_to_a_tuple(self):
        # Arrange
        article = Article(
            id=uuid.uuid4(),
            canonical_url="https://example.com/b",
            original_url="https://example.com/b",
            title="タイトル",
            source_domain="example.com",
            source_authority=0.5,
            technical_quality=0.5,
            embedding=[0.1, 0.2, 0.3],
        )

        # Act
        signature = _to_signature(article)

        # Assert
        assert signature.embedding == (0.1, 0.2, 0.3)
        assert isinstance(signature.embedding, tuple)

    def test_source_type_warning_includes_the_article_id_and_a_sanitized_value(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange — 改行混入によるログ行偽装を試みる不正値
        article = Article(
            id=uuid.uuid4(),
            canonical_url="https://example.com/d",
            original_url="https://example.com/d",
            title="タイトル",
            source_domain="example.com",
            source_authority=0.5,
            source_type="not-a-real-type\n[FAKE] injected line",
            technical_quality=0.5,
        )
        messages = capture_warnings(monkeypatch)

        # Act
        _to_signature(article)

        # Assert
        assert len(messages) == 1
        assert str(article.id) in messages[0]
        assert "\n" not in messages[0]

    def test_content_type_warning_includes_the_article_id_and_a_sanitized_value(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange
        article = Article(
            id=uuid.uuid4(),
            canonical_url="https://example.com/e",
            original_url="https://example.com/e",
            title="タイトル",
            source_domain="example.com",
            source_authority=0.5,
            content_type="not-a-real-content-type\n[FAKE] injected line",
            technical_quality=0.5,
        )
        messages = capture_warnings(monkeypatch)

        # Act
        _to_signature(article)

        # Assert
        assert len(messages) == 1
        assert str(article.id) in messages[0]
        assert "\n" not in messages[0]


class TestSanitizeForLog:
    """ログへ出す外部由来の値のサニタイズ（制御文字除去・長さ上限）。"""

    def test_removes_control_characters(self):
        # Arrange / Act / Assert
        assert _sanitize_for_log("bad\nvalue\x00here") == "badvaluehere"

    def test_truncates_to_the_maximum_length(self):
        # Arrange
        long_value = "a" * (MAX_LOGGED_VALUE_LENGTH + 50)

        # Act
        result = _sanitize_for_log(long_value)

        # Assert
        assert len(result) == MAX_LOGGED_VALUE_LENGTH


def _make_minimal_signature(article_id: uuid.UUID) -> ArticleSignature:
    """`_best_match_for` の検証だけに使う最小限の `ArticleSignature`。"""
    return ArticleSignature(
        id=article_id,
        canonical_url="https://example.com/a",
        original_url="https://example.com/a",
        title="タイトル",
        body_hash=None,
        embedding=None,
        source_authority=0.5,
        source_type=SourceType.UNKNOWN,
        content_type=None,
        technical_quality=0.5,
        published_at=None,
    )


class TestBestMatchFor:
    def test_raises_a_value_error_when_the_cluster_has_no_match_for_the_member(self):
        # Arrange — 不変条件違反: 2 件以上のクラスタなのに対象記事が関わる
        # 一致情報が matches に無い状態
        member_id = uuid.uuid4()
        other_id = uuid.uuid4()
        member = _make_minimal_signature(member_id)
        other = _make_minimal_signature(other_id)
        cluster = ArticleCluster(members=(member, other), matches=())

        # Act / Assert
        with pytest.raises(ValueError, match=str(member_id)):
            _best_match_for(member_id, cluster)


class TestNeedsUniqueValueJudgment:
    def test_returns_true_when_never_judged(self, db_session: Session):
        # Arrange
        article = make_article(db_session, body_hash="hash-a")

        # Act / Assert
        assert _needs_unique_value_judgment(article) is True

    def test_returns_false_when_the_judged_hash_matches_the_current_body_hash(
        self, db_session: Session
    ):
        # Arrange
        article = make_article(db_session, body_hash="hash-a")
        article.unique_value_judged_body_hash = "hash-a"
        article.has_unique_value = True

        # Act / Assert
        assert _needs_unique_value_judgment(article) is False

    def test_returns_true_when_the_body_hash_changed_since_the_last_judgment(
        self, db_session: Session
    ):
        # Arrange
        article = make_article(db_session, body_hash="hash-b")
        article.unique_value_judged_body_hash = "hash-a"

        # Act / Assert
        assert _needs_unique_value_judgment(article) is True

    def test_returns_true_when_the_body_hash_is_none_even_if_previously_judged(
        self, db_session: Session
    ):
        # Arrange — body_hash が None の記事は「一度も判定していない」のか
        # 「判定済みで変更なし」なのかを区別できないため、常に判定し直す
        article = make_article(db_session, body_hash=None)
        article.unique_value_judged_body_hash = None
        article.has_unique_value = True

        # Act / Assert
        assert _needs_unique_value_judgment(article) is True


class TestExcludesDeadArticles:
    def test_excludes_a_dead_article_from_deduplication_targets(self, db_session: Session):
        # Arrange — `_target_articles` の is_dead 絞り込みを検証する
        shared_body_hash = "identical-content-hash-dead"
        official = make_article(
            db_session, title="記事A", source_authority=0.9, body_hash=shared_body_hash
        )
        dead_mirror = make_article(
            db_session,
            title="記事B",
            source_authority=0.3,
            body_hash=shared_body_hash,
            is_dead=True,
        )
        provider = FakeLLMProvider(DUMMY_LLM_RESPONSE)

        # Act
        result = deduplicate_articles(db_session, provider, sleep=no_sleep)

        # Assert — is_dead の記事は対象から外れ、比較すらされない
        assert result.processed_articles == 1
        assert official.duplicate_of_article_id is None
        assert dead_mirror.duplicate_of_article_id is None


class TestLogClusterDecisionFailureDoesNotAbortDeduplication:
    def test_completes_deduplication_when_the_operation_log_write_fails(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange — 存在しない job_id を渡し、operation_logs.job_id の外部キー
        # 制約違反で `_log_cluster_decision` の書き込みを失敗させる
        # （`_log_cluster_decision` の except SQLAlchemyError 分岐を確認する）
        shared_body_hash = "identical-content-hash-log-failure"
        official = make_article(
            db_session, title="記事A", source_authority=0.9, body_hash=shared_body_hash
        )
        mirror = make_article(
            db_session, title="記事B", source_authority=0.3, body_hash=shared_body_hash
        )
        provider = FakeLLMProvider(DUMMY_LLM_RESPONSE)
        bogus_job_id = uuid.uuid4()
        messages = capture_warnings(monkeypatch)

        # Act
        result = deduplicate_articles(db_session, provider, job_id=bogus_job_id, sleep=no_sleep)

        # Assert — ログ書き込みが失敗しても重複判定自体は完走し DB へ反映される
        assert any("operation_logs" in message for message in messages)
        assert official.duplicate_of_article_id is None
        assert mirror.duplicate_of_article_id == official.id
        assert result.duplicate_count == 1


class TestUniqueValueJudgmentCaching:
    """項目1: LLM 判定結果のキャッシュ。"""

    def test_does_not_call_the_llm_again_on_an_unchanged_article(self, db_session: Session):
        # Arrange — 受入基準: 同じ入力で 2 回実行しても LLM 呼び出し回数が増えない
        make_article(
            db_session,
            title="新製品について解説する記事",
            canonical_url="https://official.example/cache-post",
            source_authority=0.9,
            body="公式発表の内容をそのまま紹介する記事。",
            body_hash="official-hash-cache-1",
        )
        analysis = make_article(
            db_session,
            title="新製品について解説する記事",
            canonical_url="https://blogger.example/cache-analysis",
            source_authority=0.65,
            content_type=ContentType.IMPLEMENTATION,
            technical_quality=0.85,
            body="実際に触って独自に計測したベンチマーク結果とコードを掲載する記事。",
            body_hash="analysis-hash-cache-1",
        )
        provider = FakeLLMProvider(
            [{"has_unique_value": True, "reason": "独自のベンチマーク結果とコードがある"}]
        )

        # Act
        first = deduplicate_articles(db_session, provider, sleep=no_sleep)
        second = deduplicate_articles(db_session, provider, sleep=no_sleep)

        # Assert
        assert first.llm_call_count == 1
        assert second.llm_call_count == 0
        assert second.llm_cache_hit_count == 1
        assert len(provider.calls) == 1
        assert analysis.duplicate_of_article_id is None

    def test_calls_the_llm_again_when_the_body_hash_changes_between_runs(self, db_session: Session):
        # Arrange — 本文が更新された記事は再判定される
        make_article(
            db_session,
            title="新製品について解説する記事",
            canonical_url="https://official.example/cache-post-2",
            source_authority=0.9,
            body="公式発表の内容をそのまま紹介する記事。",
            body_hash="official-hash-cache-2",
        )
        analysis = make_article(
            db_session,
            title="新製品について解説する記事",
            canonical_url="https://blogger.example/cache-analysis-2",
            source_authority=0.65,
            content_type=ContentType.IMPLEMENTATION,
            technical_quality=0.85,
            body="最初のバージョンの本文。",
            body_hash="analysis-hash-cache-2-v1",
        )
        provider = FakeLLMProvider([{"has_unique_value": True, "reason": "初回の判定理由"}])

        # Act — 1 回目実行後に本文が更新された想定
        first = deduplicate_articles(db_session, provider, sleep=no_sleep)
        analysis.body = "更新後の本文。新しい実測値を追加した。"
        analysis.body_hash = "analysis-hash-cache-2-v2"
        db_session.flush()
        second = deduplicate_articles(db_session, provider, sleep=no_sleep)

        # Assert
        assert first.llm_call_count == 1
        assert second.llm_call_count == 1
        assert second.llm_cache_hit_count == 0
        assert len(provider.calls) == 2

    def test_always_rejudges_when_the_body_hash_is_none(self, db_session: Session):
        # Arrange — body_hash が無い記事はキャッシュを信頼せず常に判定し直す
        make_article(
            db_session,
            title="新製品について解説する記事",
            canonical_url="https://official.example/cache-post-3",
            source_authority=0.9,
            body="公式発表の内容をそのまま紹介する記事。",
            body_hash="official-hash-cache-3",
        )
        make_article(
            db_session,
            title="新製品について解説する記事",
            canonical_url="https://blogger.example/cache-analysis-3",
            source_authority=0.65,
            content_type=ContentType.IMPLEMENTATION,
            technical_quality=0.85,
            body="本文ハッシュが取れなかった記事。",
            body_hash=None,
        )
        provider = FakeLLMProvider([{"has_unique_value": True, "reason": "判定理由"}])

        # Act
        first = deduplicate_articles(db_session, provider, sleep=no_sleep)
        second = deduplicate_articles(db_session, provider, sleep=no_sleep)

        # Assert — 本文は変わっていないが、body_hash が無いため毎回判定される
        assert first.llm_call_count == 1
        assert second.llm_call_count == 1
        assert second.llm_cache_hit_count == 0


class TestRunLimits:
    """項目4: 1 回の実行の処理量に掛ける安全弁。"""

    def test_truncates_the_target_articles_when_the_run_limit_is_exceeded(
        self,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ):
        # Arrange
        base_config = get_dedup_config()
        limited_config = base_config.model_copy(
            update={
                "limits": LimitsConfig(
                    max_articles_per_run=1,
                    max_llm_calls_per_run=base_config.limits.max_llm_calls_per_run,
                )
            }
        )
        monkeypatch.setattr("techradar.dedup.service.get_dedup_config", lambda: limited_config)
        make_article(db_session, title="記事A")
        make_article(db_session, title="記事B")
        provider = FakeLLMProvider(DUMMY_LLM_RESPONSE)
        messages = capture_warnings(monkeypatch)

        # Act
        result = deduplicate_articles(db_session, provider, sleep=no_sleep)

        # Assert
        assert result.processed_articles == 1
        assert any("truncated_count=1" in message for message in messages)

    def test_stops_calling_the_llm_once_the_run_limit_is_reached(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange
        base_config = get_dedup_config()
        limited_config = base_config.model_copy(
            update={
                "limits": LimitsConfig(
                    max_articles_per_run=base_config.limits.max_articles_per_run,
                    max_llm_calls_per_run=1,
                )
            }
        )
        monkeypatch.setattr("techradar.dedup.service.get_dedup_config", lambda: limited_config)
        shared_body_hash = "shared-hash-for-run-limit"
        official = make_article(
            db_session, title="公式発表記事", source_authority=0.9, body_hash=shared_body_hash
        )
        candidates = [
            make_article(
                db_session,
                title=f"解説記事{index}",
                source_authority=0.65,
                content_type=ContentType.IMPLEMENTATION,
                technical_quality=quality,
                body_hash=shared_body_hash,
            )
            for index, quality in enumerate((0.95, 0.85), start=1)
        ]
        provider = FakeLLMProvider(DUMMY_LLM_RESPONSE)

        # Act
        result = deduplicate_articles(db_session, provider, sleep=no_sleep)

        # Assert
        assert result.llm_call_count == 1
        assert result.llm_call_limit_reached is True
        assert len(provider.calls) == 1
        # 上限到達後に判定されなかった候補は安全側（重複）として扱われる
        assert candidates[-1].duplicate_of_article_id == official.id


class TestNewsEventId:
    """同一ニュースイベントのクラスタ ID（`PROJECT_SPEC.md` §17、Issue #20）。"""

    def test_groups_duplicate_articles_under_the_same_news_event_id(self, db_session: Session):
        # Arrange — 受入基準: 同一ニュースの複数記事が同じクラスタ ID でまとまる
        shared_body_hash = "identical-content-hash"
        official = make_article(
            db_session, title="記事A", source_authority=0.9, body_hash=shared_body_hash
        )
        mirror = make_article(
            db_session, title="記事B", source_authority=0.3, body_hash=shared_body_hash
        )
        provider = FakeLLMProvider(DUMMY_LLM_RESPONSE)

        # Act
        deduplicate_articles(db_session, provider, sleep=no_sleep)

        # Assert
        assert official.news_event_id is not None
        assert mirror.news_event_id == official.news_event_id

    def test_keeps_a_unique_value_article_in_the_same_news_event(self, db_session: Session):
        # Arrange — 独自価値ありと判定された記事は duplicate_of_article_id が
        # 付かない（別記事として残す）が、同一ニュースである事実は残す
        official = make_article(
            db_session,
            title="新製品について解説する記事",
            canonical_url="https://official.example/post",
            source_authority=0.9,
            body="公式発表の内容をそのまま紹介する記事。",
        )
        analysis = make_article(
            db_session,
            title="新製品について解説する記事",
            canonical_url="https://blogger.example/analysis",
            source_authority=0.65,
            content_type=ContentType.IMPLEMENTATION,
            technical_quality=0.85,
            body="実際に触って独自に計測したベンチマーク結果とコードを掲載する記事。",
        )
        provider = FakeLLMProvider(
            [{"has_unique_value": True, "reason": "独自のベンチマーク結果とコードがある"}]
        )

        # Act
        deduplicate_articles(db_session, provider, sleep=no_sleep)

        # Assert
        assert analysis.duplicate_of_article_id is None
        assert analysis.news_event_id is not None
        assert analysis.news_event_id == official.news_event_id

    def test_does_not_assign_an_id_to_a_standalone_article(self, db_session: Session):
        # Arrange — 単独記事はニュースイベントを構成しない
        article = make_article(db_session, title="単独記事")
        provider = FakeLLMProvider(DUMMY_LLM_RESPONSE)

        # Act
        deduplicate_articles(db_session, provider, sleep=no_sleep)

        # Assert
        assert article.news_event_id is None

    def test_keeps_the_same_id_across_reruns(self, db_session: Session):
        # Arrange — 再実行のたびに ID が振り直されると、外部から参照できない
        shared_body_hash = "identical-content-hash"
        official = make_article(
            db_session, title="記事A", source_authority=0.9, body_hash=shared_body_hash
        )
        mirror = make_article(
            db_session, title="記事B", source_authority=0.3, body_hash=shared_body_hash
        )
        provider = FakeLLMProvider(DUMMY_LLM_RESPONSE)
        deduplicate_articles(db_session, provider, sleep=no_sleep)
        first_id = official.news_event_id

        # Act
        deduplicate_articles(db_session, provider, sleep=no_sleep)

        # Assert
        assert official.news_event_id == first_id
        assert mirror.news_event_id == first_id

    def test_reuses_the_existing_id_when_a_new_article_joins_the_event(self, db_session: Session):
        # Arrange — 既にイベント ID を持つクラスタへ後から記事が加わっても、
        # 既存の ID を引き継ぐ（後続記事のために ID が変わらない）
        shared_body_hash = "identical-content-hash"
        official = make_article(
            db_session, title="記事A", source_authority=0.9, body_hash=shared_body_hash
        )
        make_article(db_session, title="記事B", source_authority=0.3, body_hash=shared_body_hash)
        provider = FakeLLMProvider(DUMMY_LLM_RESPONSE)
        deduplicate_articles(db_session, provider, sleep=no_sleep)
        existing_id = official.news_event_id

        # Act — 同じ本文ハッシュの記事を追加して再実行する
        latecomer = make_article(
            db_session, title="記事C", source_authority=0.2, body_hash=shared_body_hash
        )
        deduplicate_articles(db_session, provider, sleep=no_sleep)

        # Assert
        assert latecomer.news_event_id == existing_id
        assert official.news_event_id == existing_id

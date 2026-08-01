"""重複排除の DB 反映を検証する結合テスト（`PROJECT_SPEC.md` §17 受入基準）。

LLM は `FakeLLMProvider` に差し替えて呼ぶ。Embedding は使わない（対象の判定は
canonical URL・正規化 URL・タイトル・本文ハッシュだけで再現できるため）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from techradar.db import Article
from techradar.db.enums import ContentType, SourceType
from techradar.dedup.config import get_dedup_config
from techradar.dedup.service import _to_signature, deduplicate_articles
from techradar.llm import FakeLLMProvider

DUMMY_LLM_RESPONSE = [{"has_unique_value": False, "reason": "公式発表の要約に過ぎない"}]


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

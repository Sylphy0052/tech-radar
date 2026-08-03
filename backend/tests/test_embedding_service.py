"""Embedding の付与・再生成回避・近傍検索を検証する結合テスト。

実モデルは読み込まず `FakeEmbeddingProvider` を使う。抽象が差し替え可能で
あることの確認も兼ねる。実モデルの検証は `test_embedding_qwen.py`。

Fake は意味的な近さを持たない。ここで検証しているのは配線・フィルタ条件・
並び順であり、ランキングの品質ではない。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from techradar.db import EMBEDDING_DIMENSIONS, Article
from techradar.embedding import (
    EmbeddingProvider,
    FakeEmbeddingProvider,
    embed_articles,
    find_neighbours,
    find_similar_articles,
)
from techradar.embedding.service import (
    EMBED_CHUNK_SIZE,
    find_similar_by_vector,
    needs_embedding,
)


def make_article(
    session: Session,
    *,
    title: str,
    body: str = "",
    is_dead: bool = False,
    body_hash: str | None = "hash-1",
) -> Article:
    """テスト用の記事を保存する。"""
    article = Article(
        canonical_url=f"https://example.com/{uuid.uuid4().hex[:10]}",
        original_url="https://example.com/a",
        title=title,
        body=body,
        body_hash=body_hash,
        source_domain="example.com",
        is_dead=is_dead,
    )
    session.add(article)
    session.flush()
    return article


@pytest.fixture
def provider() -> FakeEmbeddingProvider:
    return FakeEmbeddingProvider()


class TestEmbedArticles:
    def test_assigns_vectors_of_the_configured_dimension(
        self, db_session: Session, provider: FakeEmbeddingProvider
    ):
        # Arrange
        article = make_article(db_session, title="MCP 入門", body="本文" * 50)

        # Act
        result = embed_articles(db_session, provider, [article])

        # Assert
        assert result.embedded == 1
        assert article.embedding is not None
        assert len(article.embedding) == EMBEDDING_DIMENSIONS

    def test_records_the_body_hash_used_for_the_embedding(
        self, db_session: Session, provider: FakeEmbeddingProvider
    ):
        # Arrange
        article = make_article(db_session, title="MCP 入門", body="本文" * 50)

        # Act
        embed_articles(db_session, provider, [article])

        # Assert — 本文更新の検知に使う
        assert article.embedding_body_hash == article.body_hash

    def test_does_not_regenerate_when_the_body_is_unchanged(
        self, db_session: Session, provider: FakeEmbeddingProvider
    ):
        # Arrange — §24 コスト管理「既存記事の Embedding を再生成しない」
        article = make_article(db_session, title="MCP 入門", body="本文" * 50)
        embed_articles(db_session, provider, [article])
        first_vector = list(article.embedding or [])

        # Act
        result = embed_articles(db_session, provider, [article])

        # Assert
        assert result.embedded == 0
        assert result.skipped == 1
        assert len(provider.embedded_documents) == 1
        assert list(article.embedding or []) == first_vector

    def test_regenerates_when_the_body_changed(
        self, db_session: Session, provider: FakeEmbeddingProvider
    ):
        # Arrange — 本文が更新されたらベクトルも作り直す。
        # `embedding` の有無だけで判定すると古いベクトルが残る
        article = make_article(db_session, title="MCP 入門", body="旧本文" * 50)
        embed_articles(db_session, provider, [article])
        first_vector = list(article.embedding or [])

        # Act
        article.body = "新本文" * 50
        article.body_hash = "hash-2"
        db_session.flush()
        result = embed_articles(db_session, provider, [article])

        # Assert
        assert result.embedded == 1
        assert list(article.embedding or []) != first_vector
        assert article.embedding_body_hash == "hash-2"

    def test_embeds_only_the_articles_that_need_it(
        self, db_session: Session, provider: FakeEmbeddingProvider
    ):
        # Arrange
        done = make_article(db_session, title="済", body="本文" * 50)
        embed_articles(db_session, provider, [done])
        pending = make_article(db_session, title="未", body="本文" * 50)

        # Act
        result = embed_articles(db_session, provider, [done, pending])

        # Assert
        assert result.embedded == 1
        assert result.skipped == 1

    def test_does_not_call_the_model_when_nothing_needs_embedding(
        self, db_session: Session, provider: FakeEmbeddingProvider
    ):
        # Arrange
        article = make_article(db_session, title="済", body="本文" * 50)
        embed_articles(db_session, provider, [article])
        provider.embedded_documents.clear()

        # Act
        embed_articles(db_session, provider, [article])

        # Assert — モデル呼び出しが 1 度も起きない
        assert provider.embedded_documents == []

    def test_processes_large_batches_in_chunks(
        self, db_session: Session, provider: FakeEmbeddingProvider
    ):
        # Arrange — 一度に大量の長文を渡すとメモリが跳ねる
        articles = [
            make_article(db_session, title=f"記事 {index}", body="本文" * 50)
            for index in range(EMBED_CHUNK_SIZE + 3)
        ]

        # Act
        result = embed_articles(db_session, provider, articles)

        # Assert
        assert result.embedded == len(articles)
        assert all(article.embedding is not None for article in articles)

    def test_includes_title_and_body_in_the_embedded_text(
        self, db_session: Session, provider: FakeEmbeddingProvider
    ):
        # Arrange
        article = make_article(db_session, title="固有タイトル", body="固有本文" * 30)

        # Act
        embed_articles(db_session, provider, [article])

        # Assert — タイトルだけだと短く、本文だけだと主題が薄まる
        embedded = provider.embedded_documents[0]
        assert "固有タイトル" in embedded
        assert "固有本文" in embedded

    def test_handles_an_empty_input(self, db_session: Session, provider: FakeEmbeddingProvider):
        # Arrange / Act
        result = embed_articles(db_session, provider, [])

        # Assert
        assert result.embedded == 0
        assert result.skipped == 0


class TestNeedsEmbedding:
    def test_requires_embedding_when_absent(self, db_session: Session):
        # Arrange / Act / Assert
        assert needs_embedding(make_article(db_session, title="未")) is True

    def test_requires_embedding_when_the_body_hash_differs(
        self, db_session: Session, provider: FakeEmbeddingProvider
    ):
        # Arrange
        article = make_article(db_session, title="記事", body="本文" * 50)
        embed_articles(db_session, provider, [article])

        # Act
        article.body_hash = "changed"

        # Assert
        assert needs_embedding(article) is True

    def test_does_not_require_embedding_when_unchanged(
        self, db_session: Session, provider: FakeEmbeddingProvider
    ):
        # Arrange
        article = make_article(db_session, title="記事", body="本文" * 50)
        embed_articles(db_session, provider, [article])

        # Act / Assert
        assert needs_embedding(article) is False


class TestSimilaritySearch:
    """並び順とフィルタ条件の検証。意味的なランキング品質は検証しない。"""

    @pytest.fixture(autouse=True)
    def _exact_search(self, db_session: Session) -> None:
        """このクラスの検索を厳密（総当たり）にする。

        `ix_articles_embedding_hnsw` は HNSW（近似最近傍）インデックスであり、
        プランナがこれを選ぶと「存在するのに返らない」行が出る。特にテスト実行中は
        ロールバックされた大量の記事行が dead tuple として HNSW のグラフに残り、
        `hnsw.ef_search`（既定 40）の探索打ち切りまでに live 行へ到達できないこと
        がある。実際、テーブルに live 行が 2 件しか無い状況でも 1 件しか返らない
        ケースを観測した（プランが Seq Scan のときは厳密なため常に 2 件返る）。

        どちらのプランが選ばれるかは autoanalyze の実行タイミング次第で、テストの
        追加によって記事行が増えるだけで結果が変わってしまう。ここで検証したいのは
        「距離順に並ぶこと」「フィルタ条件が効くこと」であって近似探索の再現率では
        ないため、インデックススキャンを止めて厳密な結果に固定する。
        """
        db_session.execute(text("SET LOCAL enable_indexscan = off"))

    def test_orders_by_distance(self, db_session: Session, provider: FakeEmbeddingProvider):
        # Arrange — 既知の距離関係を作る。near はクエリと同一方向、
        # far は直交に近い方向を向く
        query_vector = [0.0] * EMBEDDING_DIMENSIONS
        query_vector[0] = 1.0
        near_vector = [0.0] * EMBEDDING_DIMENSIONS
        near_vector[0] = 1.0
        far_vector = [0.0] * EMBEDDING_DIMENSIONS
        far_vector[1] = 1.0

        near = make_article(db_session, title="近い")
        far = make_article(db_session, title="遠い")
        near.embedding = near_vector
        far.embedding = far_vector
        db_session.flush()

        # Act
        found = find_similar_by_vector(db_session, query_vector, limit=2)

        # Assert
        assert [scored.article.id for scored in found] == [near.id, far.id]
        assert found[0].similarity == pytest.approx(1.0, abs=1e-6)
        assert found[1].similarity == pytest.approx(0.0, abs=1e-6)

    def test_returns_similarity_scores(self, db_session: Session, provider: FakeEmbeddingProvider):
        # Arrange — 後続の推薦スコア計算 (§14) が距離を必要とする
        article = make_article(db_session, title="記事")
        embed_articles(db_session, provider, [article])

        # Act
        found = find_similar_articles(db_session, provider, "記事", limit=1)

        # Assert
        assert 0.0 <= found[0].similarity <= 1.0

    def test_excludes_articles_without_embeddings(
        self, db_session: Session, provider: FakeEmbeddingProvider
    ):
        # Arrange
        embedded = make_article(db_session, title="埋め込み済み")
        make_article(db_session, title="未埋め込み")
        embed_articles(db_session, provider, [embedded])

        # Act
        found = find_similar_articles(db_session, provider, "何か", limit=10)

        # Assert
        assert [scored.article.id for scored in found] == [embedded.id]

    def test_excludes_dead_links(self, db_session: Session, provider: FakeEmbeddingProvider):
        # Arrange — リンク切れは推薦候補にしない
        alive = make_article(db_session, title="生きている")
        dead = make_article(db_session, title="リンク切れ", is_dead=True)
        embed_articles(db_session, provider, [alive, dead])

        # Act
        found = find_similar_articles(db_session, provider, "何か", limit=10)

        # Assert
        assert [scored.article.id for scored in found] == [alive.id]

    def test_respects_the_limit(self, db_session: Session, provider: FakeEmbeddingProvider):
        # Arrange
        articles = [make_article(db_session, title=f"記事 {i}") for i in range(5)]
        embed_articles(db_session, provider, articles)

        # Act
        found = find_similar_articles(db_session, provider, "何か", limit=2)

        # Assert
        assert len(found) == 2

    def test_rejects_a_non_positive_limit(
        self, db_session: Session, provider: FakeEmbeddingProvider
    ):
        # Arrange / Act / Assert
        with pytest.raises(ValueError, match="limit"):
            find_similar_by_vector(db_session, [0.0] * EMBEDDING_DIMENSIONS, limit=0)


class TestFindNeighbours:
    def test_excludes_the_source_article(
        self, db_session: Session, provider: FakeEmbeddingProvider
    ):
        # Arrange — 記事起点推薦で自分自身を返さない
        source = make_article(db_session, title="起点")
        other = make_article(db_session, title="別記事")
        embed_articles(db_session, provider, [source, other])

        # Act
        found = find_neighbours(db_session, source, limit=10)

        # Assert
        found_ids = [scored.article.id for scored in found]
        assert source.id not in found_ids
        assert other.id in found_ids

    def test_returns_nothing_for_an_article_without_an_embedding(self, db_session: Session):
        # Arrange
        source = make_article(db_session, title="未埋め込み")

        # Act / Assert
        assert find_neighbours(db_session, source) == []

    def test_uses_the_stored_embedding_without_calling_the_model(
        self, db_session: Session, provider: FakeEmbeddingProvider
    ):
        # Arrange
        source = make_article(db_session, title="起点")
        other = make_article(db_session, title="別記事")
        embed_articles(db_session, provider, [source, other])
        provider.embedded_queries.clear()

        # Act
        find_neighbours(db_session, source)

        # Assert — 保存済みベクトルを使い、埋め直さない
        assert provider.embedded_queries == []


class TestProviderSubstitution:
    def test_fake_provider_satisfies_the_protocol(self):
        # Arrange / Act / Assert — モデルを交換可能にする要件 (§25)
        assert isinstance(FakeEmbeddingProvider(), EmbeddingProvider)

    def test_qwen_provider_satisfies_the_protocol(self):
        # Arrange — 実モデルは読み込まずに型だけ確認する
        from techradar.config import Settings
        from techradar.embedding import QwenEmbeddingProvider

        # Act / Assert
        assert isinstance(QwenEmbeddingProvider(Settings(_env_file=None)), EmbeddingProvider)

    def test_fake_produces_well_separated_vectors(self):
        # Arrange — 無関係な文字列が似たベクトルになると、
        # 近傍検索のテストが「たまたま通る」だけになる
        fake = FakeEmbeddingProvider()
        vectors = fake.embed_documents(
            ["MCP入門", "パスタの茹で方", "PostgreSQL", "天気予報", "確定申告"]
        )

        # Act
        similarities = [
            sum(a * b for a, b in zip(vectors[i], vectors[j], strict=True))
            for i in range(len(vectors))
            for j in range(i + 1, len(vectors))
        ]

        # Assert — ほぼ直交する
        assert max(abs(value) for value in similarities) < 0.2


class TestHnswIndex:
    def test_planner_uses_the_hnsw_index(self, migrated_engine: Engine):
        # Arrange — 行数が少ないと planner は Seq Scan を選ぶため、
        # インデックスが使える形になっているかを enable_seqscan=off で確かめる
        vector = "[" + ",".join(["0.1"] * EMBEDDING_DIMENSIONS) + "]"

        # Act — ベクトルはバインドパラメータで渡す
        with migrated_engine.connect() as connection:
            connection.execute(text("SET enable_seqscan = off"))
            plan = "\n".join(
                row[0]
                for row in connection.execute(
                    text(
                        "EXPLAIN (COSTS OFF) SELECT id FROM articles "
                        "WHERE embedding IS NOT NULL "
                        "ORDER BY embedding <=> CAST(:vector AS vector) LIMIT 5"
                    ),
                    {"vector": vector},
                )
            )

        # Assert — コサイン距離の演算子で HNSW が選ばれる
        assert "ix_articles_embedding_hnsw" in plan
        assert "Index Scan" in plan

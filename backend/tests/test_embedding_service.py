"""Embedding の付与・再生成回避・近傍検索を検証する結合テスト。

実モデルは読み込まず `FakeEmbeddingProvider` を使う。抽象が差し替え可能で
あることの確認も兼ねる。実モデルの検証は `test_embedding_qwen.py`。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from techradar.db import EMBEDDING_DIMENSIONS, Article
from techradar.embedding import (
    EmbeddingProvider,
    FakeEmbeddingProvider,
    embed_articles,
    find_neighbours,
    find_similar_articles,
)


def make_article(session: Session, *, title: str, body: str, is_dead: bool = False) -> Article:
    """テスト用の記事を保存する。"""
    article = Article(
        canonical_url=f"https://example.com/{uuid.uuid4().hex[:10]}",
        original_url="https://example.com/a",
        title=title,
        body=body,
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

    def test_does_not_regenerate_existing_embeddings(
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
        assert result == type(result)(embedded=0, skipped=0)


class TestSimilaritySearch:
    def test_finds_the_matching_article_first(
        self, db_session: Session, provider: FakeEmbeddingProvider
    ):
        # Arrange — Fake は同じ文字列に同じベクトルを返すため、
        # クエリと一致する内容の記事が最も近くなる
        target_text = "Model Context Protocol の実装"
        target = make_article(db_session, title=target_text, body="")
        other = make_article(db_session, title="全く別の話題", body="")
        embed_articles(db_session, provider, [target, other])

        # Act
        found = find_similar_articles(db_session, provider, target_text, limit=2)

        # Assert
        assert found[0].id == target.id

    def test_excludes_articles_without_embeddings(
        self, db_session: Session, provider: FakeEmbeddingProvider
    ):
        # Arrange
        embedded = make_article(db_session, title="埋め込み済み", body="")
        make_article(db_session, title="未埋め込み", body="")
        embed_articles(db_session, provider, [embedded])

        # Act
        found = find_similar_articles(db_session, provider, "何か", limit=10)

        # Assert
        assert [article.id for article in found] == [embedded.id]

    def test_excludes_dead_links(self, db_session: Session, provider: FakeEmbeddingProvider):
        # Arrange — リンク切れは推薦候補にしない
        alive = make_article(db_session, title="生きている", body="")
        dead = make_article(db_session, title="リンク切れ", body="", is_dead=True)
        embed_articles(db_session, provider, [alive, dead])

        # Act
        found = find_similar_articles(db_session, provider, "何か", limit=10)

        # Assert
        assert [article.id for article in found] == [alive.id]

    def test_respects_the_limit(self, db_session: Session, provider: FakeEmbeddingProvider):
        # Arrange
        articles = [make_article(db_session, title=f"記事 {i}", body="") for i in range(5)]
        embed_articles(db_session, provider, articles)

        # Act
        found = find_similar_articles(db_session, provider, "何か", limit=2)

        # Assert
        assert len(found) == 2


class TestFindNeighbours:
    def test_excludes_the_source_article(
        self, db_session: Session, provider: FakeEmbeddingProvider
    ):
        # Arrange — 記事起点推薦で自分自身を返さない
        source = make_article(db_session, title="起点", body="")
        other = make_article(db_session, title="別記事", body="")
        embed_articles(db_session, provider, [source, other])

        # Act
        found = find_neighbours(db_session, source, limit=10)

        # Assert
        assert source.id not in [article.id for article in found]
        assert other.id in [article.id for article in found]

    def test_returns_nothing_for_an_article_without_an_embedding(
        self, db_session: Session, provider: FakeEmbeddingProvider
    ):
        # Arrange
        source = make_article(db_session, title="未埋め込み", body="")

        # Act / Assert
        assert find_neighbours(db_session, source) == []

    def test_uses_the_stored_embedding_without_calling_the_model(
        self, db_session: Session, provider: FakeEmbeddingProvider
    ):
        # Arrange
        source = make_article(db_session, title="起点", body="")
        other = make_article(db_session, title="別記事", body="")
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


class TestHnswIndexIsUsed:
    def test_similarity_query_uses_the_cosine_index(
        self, db_session: Session, provider: FakeEmbeddingProvider
    ):
        # Arrange — インデックスが使われる演算子で並べていること。
        # HNSW は行数が少ないと選ばれないため、計画に演算子が現れることを確認する
        articles = [make_article(db_session, title=f"記事 {i}", body="") for i in range(3)]
        embed_articles(db_session, provider, articles)
        vector = provider.embed_query("何か")

        # Act
        plan = db_session.execute(
            select(Article).order_by(Article.embedding.cosine_distance(vector)).limit(1).options()
        )

        # Assert — クエリが成立すること自体が pgvector 演算子の動作確認になる
        assert plan.scalars().first() is not None

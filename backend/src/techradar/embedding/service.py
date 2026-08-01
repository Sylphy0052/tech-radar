"""記事への Embedding 付与と近傍検索。

既存記事の Embedding は再生成しない（`PROJECT_SPEC.md` §24 コスト管理）。
判定は「`embedding` が既にあるか」で行い、本文が変わった場合のみ作り直す。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from techradar.db import Article
from techradar.embedding.base import EmbeddingProvider


@dataclass(frozen=True)
class EmbedResult:
    """付与結果。`skipped` は既に Embedding があって再生成しなかった件数。"""

    embedded: int
    skipped: int


def embed_articles(
    session: Session,
    provider: EmbeddingProvider,
    articles: Sequence[Article],
) -> EmbedResult:
    """記事へ Embedding を付与する。

    既に `embedding` を持つ記事は対象外にする。埋め込む対象が無ければ
    モデルを一切呼ばない。
    """
    targets = [article for article in articles if article.embedding is None]
    skipped = len(articles) - len(targets)
    if not targets:
        return EmbedResult(embedded=0, skipped=skipped)

    vectors = provider.embed_documents([_embedding_input(article) for article in targets])
    for article, vector in zip(targets, vectors, strict=True):
        article.embedding = vector
    session.flush()
    return EmbedResult(embedded=len(targets), skipped=skipped)


def _embedding_input(article: Article) -> str:
    """埋め込む対象のテキストを組み立てる。

    タイトルと本文を合わせる。タイトルだけだと短すぎ、本文だけだと
    主題が薄まるため。長さの上限はモデル側の `max_seq_length` に委ねる。
    """
    body = article.body or ""
    return f"{article.title}\n\n{body}".strip()


def find_similar_articles(
    session: Session,
    provider: EmbeddingProvider,
    query: str,
    *,
    limit: int = 10,
    exclude_article_id: object | None = None,
) -> list[Article]:
    """クエリに意味的に近い記事を返す。

    pgvector のコサイン距離で並べる。`ix_articles_embedding_hnsw` が
    `vector_cosine_ops` で作られているため、この演算子でインデックスが効く。
    """
    vector = provider.embed_query(query)
    return list(session.scalars(_similarity_query(vector, limit, exclude_article_id)))


def find_neighbours(
    session: Session,
    article: Article,
    *,
    limit: int = 10,
) -> list[Article]:
    """ある記事に近い記事を返す。既存の Embedding をそのまま使う。"""
    if article.embedding is None:
        return []
    return list(session.scalars(_similarity_query(article.embedding, limit, article.id)))


def _similarity_query(
    vector: Sequence[float],
    limit: int,
    exclude_article_id: object | None,
) -> Select[tuple[Article]]:
    """コサイン距離で並べた検索クエリを組み立てる。"""
    statement = (
        select(Article)
        .where(Article.embedding.is_not(None))
        # リンク切れ記事は推薦候補にしない。
        .where(Article.is_dead.is_(False))
        .order_by(Article.embedding.cosine_distance(list(vector)))
        .limit(limit)
    )
    if exclude_article_id is not None:
        statement = statement.where(Article.id != exclude_article_id)
    return statement

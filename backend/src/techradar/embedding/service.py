"""記事への Embedding 付与と近傍検索。

同じ本文を二度埋め込まない（`PROJECT_SPEC.md` §24 コスト管理）。判定は
`articles.embedding_body_hash` と現在の `body_hash` の比較で行う。
「`embedding` があるか」だけで判定すると、本文が更新された記事の
ベクトルが古いまま残る。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from techradar.db import Article
from techradar.embedding.base import EmbeddingProvider

DEFAULT_SIMILARITY_LIMIT = 10

# 1 度に埋め込む件数の上限。長い本文をまとめて渡すとメモリが跳ねる。
EMBED_CHUNK_SIZE = 16


@dataclass(frozen=True)
class EmbedResult:
    """付与結果。`skipped` は本文が変わっておらず再生成しなかった件数。"""

    embedded: int
    skipped: int


@dataclass(frozen=True)
class ScoredArticle:
    """近傍検索の結果。`similarity` はコサイン類似度（1.0 が最も近い）。

    後続の推薦スコア計算（`PROJECT_SPEC.md` §14 `interest_similarity`）と
    重複判定（§17）が距離そのものを必要とするため、記事と一緒に返す。
    """

    article: Article
    similarity: float


def needs_embedding(article: Article) -> bool:
    """Embedding を作り直す必要があるかを判定する。

    未生成、または生成時と本文が変わっている場合に True を返す。
    """
    if article.embedding is None:
        return True
    return article.embedding_body_hash != article.body_hash


def embed_articles(
    session: Session,
    provider: EmbeddingProvider,
    articles: Sequence[Article],
) -> EmbedResult:
    """記事へ Embedding を付与する。

    本文が変わっていない記事は対象外にする。対象が無ければモデルを一切呼ばない。
    """
    targets = [article for article in articles if needs_embedding(article)]
    skipped = len(articles) - len(targets)
    if not targets:
        return EmbedResult(embedded=0, skipped=skipped)

    for start in range(0, len(targets), EMBED_CHUNK_SIZE):
        chunk = targets[start : start + EMBED_CHUNK_SIZE]
        vectors = provider.embed_documents([_embedding_input(article) for article in chunk])
        for article, vector in zip(chunk, vectors, strict=True):
            article.embedding = vector
            article.embedding_body_hash = article.body_hash

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
    limit: int = DEFAULT_SIMILARITY_LIMIT,
    exclude_article_id: uuid.UUID | None = None,
) -> list[ScoredArticle]:
    """クエリに意味的に近い記事を、類似度付きで返す。"""
    vector = provider.embed_query(query)
    return find_similar_by_vector(
        session, vector, limit=limit, exclude_article_id=exclude_article_id
    )


def find_neighbours(
    session: Session,
    article: Article,
    *,
    limit: int = DEFAULT_SIMILARITY_LIMIT,
) -> list[ScoredArticle]:
    """ある記事に近い記事を返す。既存の Embedding をそのまま使う。"""
    if article.embedding is None:
        return []
    return find_similar_by_vector(
        session, article.embedding, limit=limit, exclude_article_id=article.id
    )


def find_similar_by_vector(
    session: Session,
    vector: Sequence[float],
    *,
    limit: int = DEFAULT_SIMILARITY_LIMIT,
    exclude_article_id: uuid.UUID | None = None,
) -> list[ScoredArticle]:
    """任意のベクトルに近い記事を返す。

    関心クラスタの centroid（`PROJECT_SPEC.md` §8）のように、テキストを
    経由しないベクトルで検索できるようにする。
    """
    if limit <= 0:
        message = f"limit は 1 以上にしてください: {limit}"
        raise ValueError(message)

    rows = session.execute(_similarity_query(vector, limit, exclude_article_id)).all()
    # pgvector のコサイン距離は 1 - 類似度。呼び出し側が扱いやすい類似度へ戻す。
    return [ScoredArticle(article=article, similarity=1.0 - distance) for article, distance in rows]


def _similarity_query(
    vector: Sequence[float],
    limit: int,
    exclude_article_id: uuid.UUID | None,
) -> Select[tuple[Article, float]]:
    """コサイン距離で並べた検索クエリを組み立てる。

    `ix_articles_embedding_hnsw` が `vector_cosine_ops` で作られているため、
    この演算子でインデックスが効く。
    """
    distance = Article.embedding.cosine_distance(list(vector))
    statement = (
        select(Article, distance)
        .where(Article.embedding.is_not(None))
        # リンク切れ記事は推薦候補にしない。
        .where(Article.is_dead.is_(False))
        .order_by(distance)
        .limit(limit)
    )
    if exclude_article_id is not None:
        statement = statement.where(Article.id != exclude_article_id)
    return statement

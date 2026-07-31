"""initial schema (初期スキーマ)

Revision ID: 28118191000a
Revises:
Create Date: 2026-08-01 03:18:48.145844

"""

from typing import Sequence, Union

from alembic import op
import pgvector.sqlalchemy
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "28118191000a"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 空の DB に対しても単体で適用できるよう、拡張の作成をマイグレーションに含める。
    # Docker の初期化スクリプトはコンテナ初回起動時にしか走らないため、
    # テスト用に CREATE DATABASE した DB では拡張が存在しない。
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "articles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("original_url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("translated_title", sa.Text(), nullable=True),
        sa.Column("summary_ja", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("source_domain", sa.Text(), nullable=False),
        sa.Column("author", sa.Text(), nullable=True),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("body_hash", sa.String(length=64), nullable=True),
        sa.Column("domain", sa.Text(), nullable=True),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column(
            "topics", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.Column(
            "technologies",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("content_type", sa.Text(), nullable=True),
        sa.Column("difficulty", sa.Text(), nullable=True),
        sa.Column("source_type", sa.Text(), nullable=True),
        sa.Column("source_authority", sa.Float(), server_default="0", nullable=False),
        sa.Column("technical_quality", sa.Float(), server_default="0", nullable=False),
        sa.Column("is_primary_source", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("is_dead", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.vector.VECTOR(dim=1024), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_articles")),
        sa.UniqueConstraint("canonical_url", name=op.f("uq_articles_canonical_url")),
    )
    op.create_index("ix_articles_body_hash", "articles", ["body_hash"], unique=False)
    op.create_index(
        "ix_articles_embedding_hnsw",
        "articles",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index("ix_articles_published_at", "articles", ["published_at"], unique=False)
    op.create_index("ix_articles_source_domain", "articles", ["source_domain"], unique=False)
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    op.create_index("ix_jobs_status_created_at", "jobs", ["status", "created_at"], unique=False)
    op.create_table(
        "source_registry",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("entity_name", sa.Text(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("path_pattern", sa.Text(), nullable=True),
        sa.Column("github_org", sa.Text(), nullable=True),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("authority_score", sa.Float(), nullable=False),
        sa.Column("verified", sa.Boolean(), server_default="false", nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_registry")),
        sa.UniqueConstraint("domain", "path_pattern", name="uq_source_registry_domain"),
    )
    op.create_index("ix_source_registry_domain", "source_registry", ["domain"], unique=False)
    op.create_table(
        "user_interest_clusters",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column(
            "topics", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.Column("centroid_embedding", pgvector.sqlalchemy.vector.VECTOR(dim=1024), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_interest_clusters")),
    )
    op.create_index(
        "ix_user_interest_clusters_user_id", "user_interest_clusters", ["user_id"], unique=False
    )
    op.create_table(
        "user_topic_preferences",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("positive_weight", sa.Float(), server_default="0", nullable=False),
        sa.Column("negative_weight", sa.Float(), server_default="0", nullable=False),
        sa.Column("effective_weight", sa.Float(), server_default="0", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", "topic", name="pk_user_topic_preferences"),
    )
    op.create_table(
        "article_feedback",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("article_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["articles.id"],
            name=op.f("fk_article_feedback_article_id_articles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "article_id", name="pk_article_feedback"),
    )
    op.create_index(
        "ix_article_feedback_article_id", "article_feedback", ["article_id"], unique=False
    )
    op.create_table(
        "operation_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("article_id", sa.Uuid(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_reason", sa.Text(), nullable=True),
        sa.Column(
            "details", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["articles.id"],
            name=op.f("fk_operation_logs_article_id_articles"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_operation_logs_job_id_jobs"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_operation_logs")),
    )
    op.create_index("ix_operation_logs_created_at", "operation_logs", ["created_at"], unique=False)
    op.create_index(
        "ix_operation_logs_operation_status",
        "operation_logs",
        ["operation", "status"],
        unique=False,
    )
    op.create_table(
        "recommendation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("source_article_id", sa.Uuid(), nullable=True),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["source_article_id"],
            ["articles.id"],
            name=op.f("fk_recommendation_runs_source_article_id_articles"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recommendation_runs")),
    )
    op.create_index(
        "ix_recommendation_runs_user_id", "recommendation_runs", ["user_id"], unique=False
    )
    op.create_table(
        "user_articles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("article_id", sa.Uuid(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("interest_weight", sa.Float(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["articles.id"],
            name=op.f("fk_user_articles_article_id_articles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_articles")),
        sa.UniqueConstraint("user_id", "article_id", name="uq_user_articles_user_id_article_id"),
    )
    op.create_index("ix_user_articles_user_id", "user_articles", ["user_id"], unique=False)
    op.create_table(
        "recommendations",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("article_id", sa.Uuid(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column(
            "reasons", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["articles.id"],
            name=op.f("fk_recommendations_article_id_articles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["recommendation_runs.id"],
            name=op.f("fk_recommendations_run_id_recommendation_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "article_id", name="pk_recommendations"),
    )
    op.create_index(
        "ix_recommendations_run_id_rank", "recommendations", ["run_id", "rank"], unique=False
    )
    # ### end Alembic commands ###


def downgrade() -> None:
    """Downgrade schema."""
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index("ix_recommendations_run_id_rank", table_name="recommendations")
    op.drop_table("recommendations")
    op.drop_index("ix_user_articles_user_id", table_name="user_articles")
    op.drop_table("user_articles")
    op.drop_index("ix_recommendation_runs_user_id", table_name="recommendation_runs")
    op.drop_table("recommendation_runs")
    op.drop_index("ix_operation_logs_operation_status", table_name="operation_logs")
    op.drop_index("ix_operation_logs_created_at", table_name="operation_logs")
    op.drop_table("operation_logs")
    op.drop_index("ix_article_feedback_article_id", table_name="article_feedback")
    op.drop_table("article_feedback")
    op.drop_table("user_topic_preferences")
    op.drop_index("ix_user_interest_clusters_user_id", table_name="user_interest_clusters")
    op.drop_table("user_interest_clusters")
    op.drop_index("ix_source_registry_domain", table_name="source_registry")
    op.drop_table("source_registry")
    op.drop_index("ix_jobs_status_created_at", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index("ix_articles_source_domain", table_name="articles")
    op.drop_index("ix_articles_published_at", table_name="articles")
    op.drop_index(
        "ix_articles_embedding_hnsw",
        table_name="articles",
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.drop_index("ix_articles_body_hash", table_name="articles")
    op.drop_table("articles")
    # ### end Alembic commands ###

"""URL 登録 API を検証する（`PROJECT_SPEC.md` §6.2, §20, Issue #12）。"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from techradar.api.deps import get_session
from techradar.config import Settings
from techradar.db.enums import JobType
from techradar.db.models import ArticleRegistration, Job
from techradar.main import create_app


class _ForeignKeyViolation(Exception):
    """外部キー違反を模した DBAPI 例外（`is_unique_violation` の判定用）。"""

    sqlstate = "23503"


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    """テスト用 DB セッションを使う API クライアント。"""
    app = create_app(Settings(_env_file=None))
    app.dependency_overrides[get_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def independent_sessions(migrated_engine: Engine) -> Iterator[Callable[[], Session]]:
    """互いに独立した DB 接続をテスト内で必要な数だけ払い出す。

    `db_session` は1本の接続を共有し外側トランザクションでロールバックする作りのため、
    「コミット済みの行が別接続から見えるか」は再現できない。ここでは接続ごとに
    独立したトランザクションを張り、テストがコミットした行の後始末まで責任を持つ。
    """
    sessions: list[Session] = []

    def open_session() -> Session:
        session = Session(bind=migrated_engine, expire_on_commit=False)
        sessions.append(session)
        return session

    try:
        yield open_session
    finally:
        for session in sessions:
            session.close()
        with Session(bind=migrated_engine) as cleanup_session:
            cleanup_session.execute(delete(ArticleRegistration))
            cleanup_session.execute(delete(Job).where(Job.type == JobType.FETCH_ARTICLE.value))
            cleanup_session.commit()


class TestCreateArticleRegistration:
    def test_registers_a_url_as_pending_and_enqueues_a_fetch_job(
        self, client: TestClient, db_session: Session
    ) -> None:
        """受入基準: 登録すると 201 で pending が返り、fetch_article ジョブが1件積まれる。"""
        # Act
        response = client.post("/api/articles", json={"url": "https://example.com/article"})

        # Assert
        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "pending"
        assert body["url"] == "https://example.com/article"
        assert body["article_id"] is None
        assert body["error_reason"] is None
        uuid.UUID(body["id"])  # 有効な UUID 形式であること

        jobs = db_session.scalars(select(Job).where(Job.type == JobType.FETCH_ARTICLE.value)).all()
        assert len(jobs) == 1

    def test_commits_the_registration_before_responding(
        self, independent_sessions: Callable[[], Session]
    ) -> None:
        """受入基準: 応答を受け取った時点で、別接続からも登録を追跡できること。

        リクエスト単位のセッションは FastAPI の依存の後処理でコミットされるが、
        後処理が走るのはレスポンス送信より後になる。UI は応答直後に
        `GET /api/articles/registrations/{id}` を叩くため、コミットを応答前に
        済ませておかないと登録直後の状態取得が 404 になる。
        """
        # Arrange
        api_session = independent_sessions()
        observer = independent_sessions()
        app = create_app(Settings(_env_file=None))
        app.dependency_overrides[get_session] = lambda: api_session

        # Act
        with TestClient(app) as test_client:
            response = test_client.post("/api/articles", json={"url": "https://example.com/a"})

        # Assert — 応答時点で別接続から見えること（コミット済みであること）
        assert response.status_code == 201
        registration_id = uuid.UUID(response.json()["id"])
        assert observer.get(ArticleRegistration, registration_id) is not None

    def test_does_not_swallow_an_integrity_error_that_is_not_a_unique_violation(
        self, client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """一意制約違反以外の整合性エラーを「重複登録」として握り潰さない。

        握り潰すと、原因の異なる失敗が「既存の登録を返した」という正常応答に
        化けてしまい、実際には登録されていないことに気付けない。
        """
        # Arrange — 外部キー違反（SQLSTATE 23503）を模した整合性エラー
        original_flush = db_session.flush

        def _raise_foreign_key_violation(*args: object, **kwargs: object) -> None:
            del args, kwargs
            monkeypatch.setattr(db_session, "flush", original_flush)
            raise IntegrityError("INSERT ...", {}, orig=_ForeignKeyViolation())

        monkeypatch.setattr(db_session, "flush", _raise_foreign_key_violation)

        # Act / Assert — 200/201 ではなく例外として表に出る
        with pytest.raises(IntegrityError):
            client.post("/api/articles", json={"url": "https://example.com/article"})

    def test_does_not_expose_normalized_url_or_user_id(self, client: TestClient) -> None:
        """内部情報（正規化 URL・ユーザー ID）を無用に露出させない。"""
        # Act
        response = client.post("/api/articles", json={"url": "https://example.com/article"})

        # Assert
        body = response.json()
        assert "normalized_url" not in body
        assert "user_id" not in body

    def test_does_not_enqueue_a_second_job_when_the_same_url_is_registered_again(
        self, client: TestClient, db_session: Session
    ) -> None:
        """受入基準: 同じ URL を再登録しても 200 が返りジョブが増えない。"""
        # Arrange
        first_response = client.post("/api/articles", json={"url": "https://example.com/article"})
        first_id = first_response.json()["id"]

        # Act
        second_response = client.post("/api/articles", json={"url": "https://example.com/article"})

        # Assert
        assert second_response.status_code == 200
        assert second_response.json()["id"] == first_id
        jobs = db_session.scalars(select(Job).where(Job.type == JobType.FETCH_ARTICLE.value)).all()
        assert len(jobs) == 1

    def test_treats_urls_that_normalize_to_the_same_value_as_a_duplicate(
        self, client: TestClient, db_session: Session
    ) -> None:
        """正規化後に一致する別表記の URL でも重複登録として扱う。"""
        # Arrange
        first_response = client.post(
            "/api/articles", json={"url": "https://Example.com/article?utm_source=x"}
        )
        first_id = first_response.json()["id"]

        # Act — 末尾スラッシュ・大文字ホスト違いだけの別表記
        second_response = client.post("/api/articles", json={"url": "https://example.com/article/"})

        # Assert
        assert second_response.status_code == 200
        assert second_response.json()["id"] == first_id
        jobs = db_session.scalars(select(Job).where(Job.type == JobType.FETCH_ARTICLE.value)).all()
        assert len(jobs) == 1

    def test_stores_the_registration_id_and_url_in_the_job_payload(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Act
        response = client.post("/api/articles", json={"url": "https://example.com/article"})

        # Assert
        registration_id = response.json()["id"]
        job = db_session.scalars(select(Job).where(Job.type == JobType.FETCH_ARTICLE.value)).one()
        assert job.payload == {
            "registration_id": registration_id,
            "url": "https://example.com/article",
        }

    def test_links_the_created_job_to_the_registration(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Act
        response = client.post("/api/articles", json={"url": "https://example.com/article"})

        # Assert
        registration_id = uuid.UUID(response.json()["id"])
        registration = db_session.get(ArticleRegistration, registration_id)
        assert registration is not None
        job = db_session.scalars(select(Job).where(Job.type == JobType.FETCH_ARTICLE.value)).one()
        assert registration.job_id == job.id

    def test_rejects_an_unknown_field(self, client: TestClient) -> None:
        # Act
        response = client.post(
            "/api/articles", json={"url": "https://example.com/article", "unexpected": "x"}
        )

        # Assert
        assert response.status_code == 422

    @pytest.mark.parametrize(
        "invalid_url",
        [
            "ftp://example.com/article",
            "javascript:alert(1)",
            "not-a-url",
            "",
            "example.com/article",
        ],
    )
    def test_rejects_a_url_with_a_disallowed_scheme_or_invalid_format(
        self, client: TestClient, invalid_url: str
    ) -> None:
        """受入基準: http/https 以外のスキームや不正形式が 422 で弾かれる。"""
        # Act
        response = client.post("/api/articles", json={"url": invalid_url})

        # Assert
        assert response.status_code == 422

    def test_rejects_a_url_longer_than_the_configured_limit(self, client: TestClient) -> None:
        # Arrange
        too_long_url = "https://example.com/" + "a" * 2048

        # Act
        response = client.post("/api/articles", json={"url": too_long_url})

        # Assert
        assert response.status_code == 422


class TestGetArticleRegistration:
    def test_returns_the_registration_status(self, client: TestClient) -> None:
        # Arrange
        create_response = client.post("/api/articles", json={"url": "https://example.com/article"})
        registration_id = create_response.json()["id"]

        # Act
        response = client.get(f"/api/articles/registrations/{registration_id}")

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == registration_id
        assert body["status"] == "pending"

    def test_returns_404_for_an_unknown_registration_id(self, client: TestClient) -> None:
        # Act
        response = client.get(f"/api/articles/registrations/{uuid.uuid4()}")

        # Assert
        assert response.status_code == 404

    def test_returns_404_for_a_registration_owned_by_another_user(
        self, client: TestClient, db_session: Session
    ) -> None:
        """他ユーザーの登録は参照させない。

        MVP は単一ユーザーのため現状は到達しない経路だが、作成側 (`POST`) が
        `user_id` で絞っているのに参照側が絞らないままだと、認証を導入した
        ときにこのエンドポイントだけ他人の登録を返してしまう。
        """
        # Arrange
        other_registration = ArticleRegistration(
            user_id=uuid.uuid4(),
            url="https://example.com/other",
            normalized_url="https://example.com/other",
        )
        db_session.add(other_registration)
        db_session.flush()

        # Act
        response = client.get(f"/api/articles/registrations/{other_registration.id}")

        # Assert
        assert response.status_code == 404

    def test_does_not_expose_normalized_url_or_user_id(self, client: TestClient) -> None:
        # Arrange
        create_response = client.post("/api/articles", json={"url": "https://example.com/article"})
        registration_id = create_response.json()["id"]

        # Act
        response = client.get(f"/api/articles/registrations/{registration_id}")

        # Assert
        body = response.json()
        assert "normalized_url" not in body
        assert "user_id" not in body


def _upload_bulk_file(
    client: TestClient, content: str | bytes, *, filename: str = "urls.md"
) -> httpx.Response:
    """一括登録 API へファイルをアップロードする（テスト用ヘルパー）。"""
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return client.post("/api/articles/bulk", files={"file": (filename, raw, "text/markdown")})


@contextmanager
def _recorded_sql(session: Session) -> Iterator[list[str]]:
    """セッションが使う接続で実行された SQL 文を記録する。

    「何回 DB へ往復したか」を数えるため、ORM の発行結果ではなく実際に
    カーソルへ渡された文を見る。
    """
    statements: list[str] = []

    def _record(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    connection = session.connection()
    event.listen(connection, "before_cursor_execute", _record)
    try:
        yield statements
    finally:
        event.remove(connection, "before_cursor_execute", _record)


class TestBulkImportArticleRegistrations:
    """`POST /api/articles/bulk`（Issue #39）。"""

    def test_registers_urls_from_markdown_link_and_bare_url_lines(
        self, client: TestClient, db_session: Session
    ) -> None:
        """受入基準: Markdownリンク・素URL行から抽出したURLが登録される。

        見出し・空行・URLを含まない行は無視され、エラー件数に数えられない。
        """
        # Arrange
        content = (
            "## 7月下旬\n"
            "\n"
            "- [記事A](https://example.com/a)\n"
            "メモ行（URLなし）\n"
            "https://example.com/b\n"
        )

        # Act
        response = _upload_bulk_file(client, content)

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert body["created_count"] == 2
        assert body["duplicate_count"] == 0
        assert body["error_count"] == 0
        assert body["errors"] == []
        assert {item["url"] for item in body["created"]} == {
            "https://example.com/a",
            "https://example.com/b",
        }
        jobs = db_session.scalars(select(Job).where(Job.type == JobType.FETCH_ARTICLE.value)).all()
        assert len(jobs) == 2

    def test_registers_only_the_first_url_when_a_line_has_multiple(
        self, client: TestClient, db_session: Session
    ) -> None:
        """受入基準: 1行に複数URLがあっても最初の1つだけ抽出・登録される。"""
        # Act
        response = _upload_bulk_file(client, "https://example.com/a https://example.com/b\n")

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert body["created_count"] == 1
        assert body["created"][0]["url"] == "https://example.com/a"
        jobs = db_session.scalars(select(Job).where(Job.type == JobType.FETCH_ARTICLE.value)).all()
        assert len(jobs) == 1

    def test_reports_invalid_lines_with_line_number_and_reason_without_blocking_other_lines(
        self, client: TestClient, db_session: Session
    ) -> None:
        """受入基準: 不正スキーム/2048文字超の行が行番号・理由付きでエラーとして返り、
        他の行の登録は成功する。
        """
        # Arrange
        too_long_url = "https://example.com/" + "a" * 2048
        content = f"https://example.com/good\nftp://example.com/bad\n{too_long_url}\n"

        # Act
        response = _upload_bulk_file(client, content)

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert body["created_count"] == 1
        assert body["created"][0]["url"] == "https://example.com/good"
        assert body["error_count"] == 2
        assert {error["line_number"] for error in body["errors"]} == {2, 3}
        for error in body["errors"]:
            assert error["line"]
            assert error["reason"]

    def test_counts_a_line_as_duplicate_when_a_matching_registration_already_exists(
        self, client: TestClient, db_session: Session
    ) -> None:
        """受入基準: 既存登録と同じ正規化URLの行は重複として数えられ、
        fetchジョブが積み増されない。
        """
        # Arrange
        setup_response = client.post("/api/articles", json={"url": "https://example.com/existing"})
        assert setup_response.status_code == 201

        # Act
        response = _upload_bulk_file(client, "https://example.com/existing\n")

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert body["created_count"] == 0
        assert body["duplicate_count"] == 1
        assert body["created"] == []
        jobs = db_session.scalars(select(Job).where(Job.type == JobType.FETCH_ARTICLE.value)).all()
        assert len(jobs) == 1

    def test_registers_only_the_first_occurrence_of_a_duplicate_url_within_the_file(
        self, client: TestClient, db_session: Session
    ) -> None:
        """受入基準: 同一ファイル内の重複URL（正規化後に一致）は初出のみ登録される。"""
        # Arrange — 末尾スラッシュ・大文字ホスト違いだけの別表記
        content = "https://example.com/a\nhttps://Example.com/a/\n"

        # Act
        response = _upload_bulk_file(client, content)

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert body["created_count"] == 1
        assert body["duplicate_count"] == 1
        assert len(body["created"]) == 1
        jobs = db_session.scalars(select(Job).where(Job.type == JobType.FETCH_ARTICLE.value)).all()
        assert len(jobs) == 1

    def test_returns_413_and_leaves_the_db_unchanged_when_url_count_exceeds_the_limit(
        self, client: TestClient, db_session: Session
    ) -> None:
        """受入基準: 抽出後URL件数が501件のファイルは413になりDBが無変更のまま。"""
        # Arrange
        content = "\n".join(f"https://example.com/{i}" for i in range(501)) + "\n"

        # Act
        response = _upload_bulk_file(client, content)

        # Assert
        assert response.status_code == 413
        assert db_session.scalar(select(func.count()).select_from(ArticleRegistration)) == 0
        assert db_session.scalar(select(func.count()).select_from(Job)) == 0

    def test_does_not_count_headings_and_blank_lines_toward_the_url_count_limit(
        self, client: TestClient
    ) -> None:
        """受入基準: 抽出後のURL件数ではなく行数で判定すると誤って413になるケースが
        無いことを確認する（見出し・空行を大量に含んでもURLが500件以下なら通る）。
        """
        # Arrange — 見出し・空行が600行、URLは500件ぴったり
        heading_lines = "\n".join(f"## 見出し{i}" for i in range(600))
        url_lines = "\n".join(f"https://example.com/{i}" for i in range(500))
        content = f"{heading_lines}\n{url_lines}\n"

        # Act
        response = _upload_bulk_file(client, content)

        # Assert
        assert response.status_code == 200
        assert response.json()["created_count"] == 500

    def test_returns_413_and_leaves_the_db_unchanged_when_file_size_exceeds_the_limit(
        self, client: TestClient, db_session: Session
    ) -> None:
        """受入基準: 1MB超のファイルは413になりDBが無変更のまま。"""
        # Arrange
        oversized_content = "a" * (1024 * 1024 + 1)

        # Act
        response = _upload_bulk_file(client, oversized_content, filename="urls.txt")

        # Assert
        assert response.status_code == 413
        assert db_session.scalar(select(func.count()).select_from(ArticleRegistration)) == 0

    def test_does_not_roll_back_other_lines_when_one_line_hits_a_unique_violation(
        self, client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """受入基準: ある行で一意制約違反が起きても、他の行の登録が巻き戻らない
        （SAVEPOINTの担保）。

        既存登録を作った上で、既存チェック（`_find_existing_normalized_urls`）が
        常に空集合を返すよう差し替えることで、事前チェックと挿入の間に同時挿入が
        起きた場合（TOCTOU）と同じ状況を再現する。1行目は正常に登録された後、
        2行目で実際のDB一意制約違反が起きるが、SAVEPOINTで隔離されていれば
        1行目の登録は巻き戻らない。
        """
        # Arrange
        setup_response = client.post("/api/articles", json={"url": "https://example.com/existing"})
        assert setup_response.status_code == 201
        monkeypatch.setattr(
            "techradar.api.articles._find_existing_normalized_urls",
            lambda *args, **kwargs: set(),
        )
        content = "https://example.com/success\nhttps://example.com/existing\n"

        # Act
        response = _upload_bulk_file(client, content)

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert body["created_count"] == 1
        assert body["created"][0]["url"] == "https://example.com/success"
        assert body["duplicate_count"] == 1

        success_registration = db_session.scalar(
            select(ArticleRegistration).where(
                ArticleRegistration.url == "https://example.com/success"
            )
        )
        assert success_registration is not None
        jobs = db_session.scalars(select(Job).where(Job.type == JobType.FETCH_ARTICLE.value)).all()
        # 事前の単発登録分 + 1行目成功分の2件。2行目（重複）は積み増されない。
        assert len(jobs) == 2

    def test_returns_422_when_the_file_cannot_be_decoded_as_utf8(
        self, client: TestClient, db_session: Session
    ) -> None:
        """受入基準: UTF-8デコード不能なファイルは422になる。"""
        # Arrange
        invalid_utf8_bytes = b"https://example.com/a\n\xff\xfe invalid bytes"

        # Act
        response = _upload_bulk_file(client, invalid_utf8_bytes, filename="urls.txt")

        # Assert
        assert response.status_code == 422
        assert db_session.scalar(select(func.count()).select_from(ArticleRegistration)) == 0

    def test_returns_422_for_an_unsupported_file_extension(self, client: TestClient) -> None:
        """`.md` / `.txt` 以外の拡張子は受け付けない。"""
        # Act
        response = _upload_bulk_file(client, "https://example.com/a\n", filename="urls.csv")

        # Assert
        assert response.status_code == 422

    def test_does_not_touch_the_registrations_table_per_row_at_the_url_count_limit(
        self, client: TestClient, db_session: Session
    ) -> None:
        """受入基準: 上限の500件を一括登録しても、既存登録の突き合わせは1クエリで済む。

        行ごとに既存チェックの SELECT を出す実装では、ここが件数ぶんの往復になる。
        併せて、登録行への UPDATE が行ごとに走らないことも確かめる。UPDATE は
        `updated_at` をサーバー側で書き直すため、応答を組み立てる際に行ごとの
        再読込 SELECT を誘発する。
        """
        # Arrange
        content = "\n".join(f"https://example.com/{i}" for i in range(500)) + "\n"

        # Act
        with _recorded_sql(db_session) as statements:
            response = _upload_bulk_file(client, content)

        # Assert
        assert response.status_code == 200
        assert response.json()["created_count"] == 500
        lookups = [
            statement
            for statement in statements
            if statement.lstrip().upper().startswith("SELECT")
            and "article_registrations" in statement
        ]
        assert len(lookups) == 1
        updates = [
            statement
            for statement in statements
            if statement.lstrip().upper().startswith("UPDATE")
            and "article_registrations" in statement
        ]
        assert updates == []

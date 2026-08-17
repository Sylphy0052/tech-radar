import { afterEach, describe, expect, it, vi } from "vitest";

import {
  buildSearchParamsFromFilters,
  deleteInterestArticle,
  EMPTY_ARTICLE_FILTERS,
  isoToJstDateInputValue,
  jstDateToRegisteredFromIso,
  jstDateToRegisteredToIso,
  listInterestArticles,
  originLabel,
  parseArticleFiltersFromSearchParams,
  technologyDisplayStatus,
} from "@/lib/interest-articles";
import type { ArticleFilters } from "@/lib/interest-articles";
import { TEST_TIMEOUT_MS } from "@/test-utils/timeouts";

afterEach(() => {
  vi.unstubAllGlobals();
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

/** 空の関心記事一覧レスポンス（番号付きページングの形、Issue #91）。 */
function emptyListResponse() {
  return { items: [], total_count: 0, page: 1, page_size: 20, total_pages: 0 };
}

describe("parseArticleFiltersFromSearchParams", () => {
  it("returns empty filters when the search params are empty", () => {
    // Arrange
    const searchParams = new URLSearchParams();

    // Act
    const filters = parseArticleFiltersFromSearchParams(searchParams);

    // Assert
    expect(filters).toEqual(EMPTY_ARTICLE_FILTERS);
  }, TEST_TIMEOUT_MS);

  it("reads multiple origin values", () => {
    // Arrange
    const searchParams = new URLSearchParams("origin=good&origin=saved");

    // Act
    const filters = parseArticleFiltersFromSearchParams(searchParams);

    // Assert
    expect(filters.origin).toEqual(["good", "saved"]);
  }, TEST_TIMEOUT_MS);

  it("drops origin values that are not part of the interest article list", () => {
    // Arrange — read_full/clicked はこの一覧に出ない経路なので、URL に混ざっていても捨てる
    const searchParams = new URLSearchParams("origin=good&origin=read_full");

    // Act
    const filters = parseArticleFiltersFromSearchParams(searchParams);

    // Assert
    expect(filters.origin).toEqual(["good"]);
  }, TEST_TIMEOUT_MS);

  it("parses is_primary_source as a boolean", () => {
    expect(parseArticleFiltersFromSearchParams(new URLSearchParams("is_primary_source=true")).isPrimarySource).toBe(
      true,
    );
    expect(parseArticleFiltersFromSearchParams(new URLSearchParams("is_primary_source=false")).isPrimarySource).toBe(
      false,
    );
    expect(parseArticleFiltersFromSearchParams(new URLSearchParams("")).isPrimarySource).toBeNull();
  }, TEST_TIMEOUT_MS);

  it("falls back to null when registered_from is not a valid date", () => {
    // Arrange — 共有リンク/手動編集/ブラウザ履歴経由で容易に到達しうる不正値
    const searchParams = new URLSearchParams("registered_from=not-a-date");

    // Act
    const filters = parseArticleFiltersFromSearchParams(searchParams);

    // Assert — バックエンドへ不正値を送らないよう、ここで落とす
    expect(filters.registeredFrom).toBeNull();
  }, TEST_TIMEOUT_MS);

  it("falls back to null when registered_to is not a valid date", () => {
    // Arrange
    const searchParams = new URLSearchParams("registered_to=also-not-a-date");

    // Act
    const filters = parseArticleFiltersFromSearchParams(searchParams);

    // Assert
    expect(filters.registeredTo).toBeNull();
  }, TEST_TIMEOUT_MS);

  it("reads the search query and the tag filters", () => {
    // Arrange — トピック・技術タグは複数指定できる（backend では AND、Issue #91）
    const searchParams = new URLSearchParams(
      "q=Rust&topics=LLM&topics=RAG&technologies=Python",
    );

    // Act
    const filters = parseArticleFiltersFromSearchParams(searchParams);

    // Assert
    expect(filters.q).toBe("Rust");
    expect(filters.topics).toEqual(["LLM", "RAG"]);
    expect(filters.technologies).toEqual(["Python"]);
  }, TEST_TIMEOUT_MS);

  it("reads the remaining text and date filters", () => {
    // Arrange
    const searchParams = new URLSearchParams(
      "domain=ai&category=llm&source_domain=blog.example.com&language=ja&registered_from=2026-08-01T00%3A00%3A00.000Z&registered_to=2026-08-01T23%3A59%3A59.999Z",
    );

    // Act
    const filters = parseArticleFiltersFromSearchParams(searchParams);

    // Assert
    expect(filters).toEqual({
      origin: [],
      q: null,
      topics: [],
      technologies: [],
      domain: "ai",
      category: "llm",
      sourceDomain: "blog.example.com",
      language: "ja",
      registeredFrom: "2026-08-01T00:00:00.000Z",
      registeredTo: "2026-08-01T23:59:59.999Z",
      isPrimarySource: null,
    });
  }, TEST_TIMEOUT_MS);
});

describe("buildSearchParamsFromFilters", () => {
  it("produces an empty query string for empty filters", () => {
    // Act
    const params = buildSearchParamsFromFilters(EMPTY_ARTICLE_FILTERS);

    // Assert
    expect(params.toString()).toBe("");
  }, TEST_TIMEOUT_MS);

  it("appends one origin entry per selected value", () => {
    // Arrange
    const filters: ArticleFilters = { ...EMPTY_ARTICLE_FILTERS, origin: ["good", "saved"] };

    // Act
    const params = buildSearchParamsFromFilters(filters);

    // Assert
    expect(params.getAll("origin")).toEqual(["good", "saved"]);
  }, TEST_TIMEOUT_MS);

  it("includes every filter kind when combined", () => {
    // Arrange
    const filters: ArticleFilters = {
      origin: ["manual"],
      q: "Rust",
      topics: ["LLM", "RAG"],
      technologies: ["Python"],
      domain: "ai",
      category: "llm",
      sourceDomain: "blog.example.com",
      language: "ja",
      registeredFrom: "2026-08-01T00:00:00.000Z",
      registeredTo: "2026-08-01T23:59:59.999Z",
      isPrimarySource: true,
    };

    // Act
    const params = buildSearchParamsFromFilters(filters);

    // Assert
    expect(Object.fromEntries(params.entries())).toMatchObject({
      q: "Rust",
      domain: "ai",
      category: "llm",
      source_domain: "blog.example.com",
      language: "ja",
      registered_from: "2026-08-01T00:00:00.000Z",
      registered_to: "2026-08-01T23:59:59.999Z",
      is_primary_source: "true",
    });
    expect(params.getAll("origin")).toEqual(["manual"]);
    expect(params.getAll("topics")).toEqual(["LLM", "RAG"]);
    expect(params.getAll("technologies")).toEqual(["Python"]);
  }, TEST_TIMEOUT_MS);

  it("round-trips through parseArticleFiltersFromSearchParams", () => {
    // Arrange
    const filters: ArticleFilters = {
      origin: ["good", "saved"],
      q: "Rust",
      topics: ["LLM"],
      technologies: [],
      domain: "ai",
      category: null,
      sourceDomain: null,
      language: "ja",
      registeredFrom: null,
      registeredTo: null,
      isPrimarySource: false,
    };

    // Act
    const restored = parseArticleFiltersFromSearchParams(buildSearchParamsFromFilters(filters));

    // Assert
    expect(restored).toEqual(filters);
  }, TEST_TIMEOUT_MS);
});

describe("JST date conversion helpers", () => {
  it("converts a date-only value to the start of that JST day in UTC", () => {
    expect(jstDateToRegisteredFromIso("2026-08-01")).toBe("2026-07-31T15:00:00.000Z");
  }, TEST_TIMEOUT_MS);

  it("converts a date-only value to the end of that JST day in UTC", () => {
    expect(jstDateToRegisteredToIso("2026-08-01")).toBe("2026-08-01T14:59:59.999Z");
  }, TEST_TIMEOUT_MS);

  it("converts a UTC instant back to the JST calendar date", () => {
    expect(isoToJstDateInputValue("2026-07-31T15:00:00.000Z")).toBe("2026-08-01");
  }, TEST_TIMEOUT_MS);

  it("returns an empty string instead of throwing for an unparseable ISO string", () => {
    // Arrange — `?registered_from=not-a-date` のような URL から到達しうる値
    // Act & Assert — RangeError を送出せず、安全側（空欄）にフォールバックする
    expect(() => isoToJstDateInputValue("not-a-date")).not.toThrow();
    expect(isoToJstDateInputValue("not-a-date")).toBe("");
  }, TEST_TIMEOUT_MS);
});

describe("listInterestArticles", () => {
  it("requests /api/articles without a query string when filters are empty", async () => {
    // Arrange
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(emptyListResponse()));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    await listInterestArticles(EMPTY_ARTICLE_FILTERS);

    // Assert
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/\/api\/articles$/),
      expect.anything(),
    );
  }, TEST_TIMEOUT_MS);

  it("forwards the search query and the tag filters as query parameters", async () => {
    // Arrange
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(emptyListResponse()));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    await listInterestArticles({
      ...EMPTY_ARTICLE_FILTERS,
      q: "Rust",
      topics: ["LLM", "RAG"],
      technologies: ["Python"],
    });

    // Assert
    const [url] = fetchMock.mock.calls[0] as [string];
    const searchParams = new URL(url).searchParams;
    expect(searchParams.get("q")).toBe("Rust");
    expect(searchParams.getAll("topics")).toEqual(["LLM", "RAG"]);
    expect(searchParams.getAll("technologies")).toEqual(["Python"]);
  }, TEST_TIMEOUT_MS);

  it("forwards a single filter as a query parameter", async () => {
    // Arrange
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(emptyListResponse()));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    await listInterestArticles({ ...EMPTY_ARTICLE_FILTERS, domain: "ai" });

    // Assert
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(new URL(url).searchParams.get("domain")).toBe("ai");
  }, TEST_TIMEOUT_MS);

  it("forwards combined filters and page/limit as query parameters", async () => {
    // Arrange
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(emptyListResponse()));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    await listInterestArticles(
      { ...EMPTY_ARTICLE_FILTERS, origin: ["good", "saved"], language: "ja", isPrimarySource: true },
      { page: 2, limit: 50 },
    );

    // Assert
    const [url] = fetchMock.mock.calls[0] as [string];
    const searchParams = new URL(url).searchParams;
    expect(searchParams.getAll("origin")).toEqual(["good", "saved"]);
    expect(searchParams.get("language")).toBe("ja");
    expect(searchParams.get("is_primary_source")).toBe("true");
    expect(searchParams.get("page")).toBe("2");
    expect(searchParams.get("limit")).toBe("50");
  }, TEST_TIMEOUT_MS);
});

describe("deleteInterestArticle", () => {
  it("sends a DELETE request to the interest endpoint", async () => {
    // Arrange
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    await deleteInterestArticle("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa");

    // Assert
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/articles/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/interest"),
      expect.objectContaining({ method: "DELETE" }),
    );
  }, TEST_TIMEOUT_MS);
});

describe("originLabel", () => {
  it("returns the Japanese label for each of the five origins", () => {
    // Arrange / Act / Assert
    expect(originLabel("manual")).toBe("手動登録");
    expect(originLabel("good")).toBe("Good");
    expect(originLabel("saved")).toBe("保存");
    expect(originLabel("read_full")).toBe("全文閲覧");
    expect(originLabel("clicked")).toBe("クリック");
  }, TEST_TIMEOUT_MS);
});

describe("technologyDisplayStatus", () => {
  // 受入基準（Issue #92）: analysis_status と technologies から、技術タグ欄に
  // 何を出すかが状態別に決まる。「未解析だから空」と「解析済みだが実際に0件」を
  // 区別できることが目的のため、pending/analyzing/null は同じ扱いにまとめる。
  it("returns pending when analysis_status is null", () => {
    expect(technologyDisplayStatus(null, [])).toBe("pending");
  }, TEST_TIMEOUT_MS);

  it("returns pending when analysis_status is pending", () => {
    expect(technologyDisplayStatus("pending", [])).toBe("pending");
  }, TEST_TIMEOUT_MS);

  it("returns pending when analysis_status is analyzing", () => {
    expect(technologyDisplayStatus("analyzing", [])).toBe("pending");
  }, TEST_TIMEOUT_MS);

  it("returns failed when analysis_status is failed", () => {
    expect(technologyDisplayStatus("failed", [])).toBe("failed");
  }, TEST_TIMEOUT_MS);

  it("returns empty when completed but technologies is empty", () => {
    expect(technologyDisplayStatus("completed", [])).toBe("empty");
  }, TEST_TIMEOUT_MS);

  it("returns tags when completed and technologies has entries", () => {
    expect(technologyDisplayStatus("completed", ["Python"])).toBe("tags");
  }, TEST_TIMEOUT_MS);
});

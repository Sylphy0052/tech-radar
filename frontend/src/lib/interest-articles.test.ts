import { afterEach, describe, expect, it, vi } from "vitest";

import {
  buildSearchParamsFromFilters,
  deleteInterestArticle,
  EMPTY_ARTICLE_FILTERS,
  isoToJstDateInputValue,
  jstDateToRegisteredFromIso,
  jstDateToRegisteredToIso,
  listInterestArticles,
  parseArticleFiltersFromSearchParams,
} from "@/lib/interest-articles";
import type { ArticleFilters } from "@/lib/interest-articles";

afterEach(() => {
  vi.unstubAllGlobals();
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

describe("parseArticleFiltersFromSearchParams", () => {
  it("returns empty filters when the search params are empty", () => {
    // Arrange
    const searchParams = new URLSearchParams();

    // Act
    const filters = parseArticleFiltersFromSearchParams(searchParams);

    // Assert
    expect(filters).toEqual(EMPTY_ARTICLE_FILTERS);
  });

  it("reads multiple origin values", () => {
    // Arrange
    const searchParams = new URLSearchParams("origin=good&origin=saved");

    // Act
    const filters = parseArticleFiltersFromSearchParams(searchParams);

    // Assert
    expect(filters.origin).toEqual(["good", "saved"]);
  });

  it("drops origin values that are not part of the interest article list", () => {
    // Arrange — read_full/clicked はこの一覧に出ない経路なので、URL に混ざっていても捨てる
    const searchParams = new URLSearchParams("origin=good&origin=read_full");

    // Act
    const filters = parseArticleFiltersFromSearchParams(searchParams);

    // Assert
    expect(filters.origin).toEqual(["good"]);
  });

  it("parses is_primary_source as a boolean", () => {
    expect(parseArticleFiltersFromSearchParams(new URLSearchParams("is_primary_source=true")).isPrimarySource).toBe(
      true,
    );
    expect(parseArticleFiltersFromSearchParams(new URLSearchParams("is_primary_source=false")).isPrimarySource).toBe(
      false,
    );
    expect(parseArticleFiltersFromSearchParams(new URLSearchParams("")).isPrimarySource).toBeNull();
  });

  it("falls back to null when registered_from is not a valid date", () => {
    // Arrange — 共有リンク/手動編集/ブラウザ履歴経由で容易に到達しうる不正値
    const searchParams = new URLSearchParams("registered_from=not-a-date");

    // Act
    const filters = parseArticleFiltersFromSearchParams(searchParams);

    // Assert — バックエンドへ不正値を送らないよう、ここで落とす
    expect(filters.registeredFrom).toBeNull();
  });

  it("falls back to null when registered_to is not a valid date", () => {
    // Arrange
    const searchParams = new URLSearchParams("registered_to=also-not-a-date");

    // Act
    const filters = parseArticleFiltersFromSearchParams(searchParams);

    // Assert
    expect(filters.registeredTo).toBeNull();
  });

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
      domain: "ai",
      category: "llm",
      sourceDomain: "blog.example.com",
      language: "ja",
      registeredFrom: "2026-08-01T00:00:00.000Z",
      registeredTo: "2026-08-01T23:59:59.999Z",
      isPrimarySource: null,
    });
  });
});

describe("buildSearchParamsFromFilters", () => {
  it("produces an empty query string for empty filters", () => {
    // Act
    const params = buildSearchParamsFromFilters(EMPTY_ARTICLE_FILTERS);

    // Assert
    expect(params.toString()).toBe("");
  });

  it("appends one origin entry per selected value", () => {
    // Arrange
    const filters: ArticleFilters = { ...EMPTY_ARTICLE_FILTERS, origin: ["good", "saved"] };

    // Act
    const params = buildSearchParamsFromFilters(filters);

    // Assert
    expect(params.getAll("origin")).toEqual(["good", "saved"]);
  });

  it("includes every filter kind when combined", () => {
    // Arrange
    const filters: ArticleFilters = {
      origin: ["manual"],
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
      domain: "ai",
      category: "llm",
      source_domain: "blog.example.com",
      language: "ja",
      registered_from: "2026-08-01T00:00:00.000Z",
      registered_to: "2026-08-01T23:59:59.999Z",
      is_primary_source: "true",
    });
    expect(params.getAll("origin")).toEqual(["manual"]);
  });

  it("round-trips through parseArticleFiltersFromSearchParams", () => {
    // Arrange
    const filters: ArticleFilters = {
      origin: ["good", "saved"],
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
  });
});

describe("JST date conversion helpers", () => {
  it("converts a date-only value to the start of that JST day in UTC", () => {
    expect(jstDateToRegisteredFromIso("2026-08-01")).toBe("2026-07-31T15:00:00.000Z");
  });

  it("converts a date-only value to the end of that JST day in UTC", () => {
    expect(jstDateToRegisteredToIso("2026-08-01")).toBe("2026-08-01T14:59:59.999Z");
  });

  it("converts a UTC instant back to the JST calendar date", () => {
    expect(isoToJstDateInputValue("2026-07-31T15:00:00.000Z")).toBe("2026-08-01");
  });

  it("returns an empty string instead of throwing for an unparseable ISO string", () => {
    // Arrange — `?registered_from=not-a-date` のような URL から到達しうる値
    // Act & Assert — RangeError を送出せず、安全側（空欄）にフォールバックする
    expect(() => isoToJstDateInputValue("not-a-date")).not.toThrow();
    expect(isoToJstDateInputValue("not-a-date")).toBe("");
  });
});

describe("listInterestArticles", () => {
  it("requests /api/articles without a query string when filters are empty", async () => {
    // Arrange
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [], next_cursor: null }));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    await listInterestArticles(EMPTY_ARTICLE_FILTERS);

    // Assert
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/\/api\/articles$/),
      expect.anything(),
    );
  });

  it("forwards a single filter as a query parameter", async () => {
    // Arrange
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [], next_cursor: null }));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    await listInterestArticles({ ...EMPTY_ARTICLE_FILTERS, domain: "ai" });

    // Assert
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(new URL(url).searchParams.get("domain")).toBe("ai");
  });

  it("forwards combined filters and cursor/limit as query parameters", async () => {
    // Arrange
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [], next_cursor: null }));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    await listInterestArticles(
      { ...EMPTY_ARTICLE_FILTERS, origin: ["good", "saved"], language: "ja", isPrimarySource: true },
      { cursor: "page-2", limit: 50 },
    );

    // Assert
    const [url] = fetchMock.mock.calls[0] as [string];
    const searchParams = new URL(url).searchParams;
    expect(searchParams.getAll("origin")).toEqual(["good", "saved"]);
    expect(searchParams.get("language")).toBe("ja");
    expect(searchParams.get("is_primary_source")).toBe("true");
    expect(searchParams.get("cursor")).toBe("page-2");
    expect(searchParams.get("limit")).toBe("50");
  });
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
  });
});

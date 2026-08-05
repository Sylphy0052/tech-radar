import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LoadingIndicator } from "@/components/ui/LoadingIndicator";
import { TEST_TIMEOUT_MS } from "@/test-utils/timeouts";

describe("LoadingIndicator", () => {
  it("renders the given label as a status", () => {
    // Arrange / Act
    render(<LoadingIndicator label="読み込み中です。" />);

    // Assert
    expect(screen.getByRole("status")).toHaveTextContent("読み込み中です。");
  }, TEST_TIMEOUT_MS);
});

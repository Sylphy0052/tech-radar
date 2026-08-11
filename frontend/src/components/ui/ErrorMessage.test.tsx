import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ErrorMessage } from "@/components/ui/ErrorMessage";
import { TEST_TIMEOUT_MS } from "@/test-utils/timeouts";

describe("ErrorMessage", () => {
  it("renders the given message as an alert", () => {
    // Arrange / Act
    render(<ErrorMessage message="通信に失敗しました。" />);

    // Assert
    expect(screen.getByRole("alert")).toHaveTextContent("通信に失敗しました。");
  }, TEST_TIMEOUT_MS);
});

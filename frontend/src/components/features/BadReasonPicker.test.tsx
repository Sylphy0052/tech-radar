import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { BadReasonPicker } from "@/components/features/BadReasonPicker";
import { BAD_REASON_LABELS } from "@/lib/feedback";
import { TEST_TIMEOUT_MS } from "@/test-utils/timeouts";

describe("BadReasonPicker", () => {
  it("shows every bad reason as a selectable option", () => {
    // Arrange / Act
    render(<BadReasonPicker onSubmit={vi.fn()} onCancel={vi.fn()} />);

    // Assert
    for (const label of Object.values(BAD_REASON_LABELS)) {
      expect(screen.getByLabelText(label)).toBeInTheDocument();
    }
  }, TEST_TIMEOUT_MS);

  it("submits without a reason when nothing is selected", () => {
    // Arrange
    const onSubmit = vi.fn();
    render(<BadReasonPicker onSubmit={onSubmit} onCancel={vi.fn()} />);

    // Act
    fireEvent.click(screen.getByRole("button", { name: "理由なしで送信" }));

    // Assert
    expect(onSubmit).toHaveBeenCalledWith(undefined);
  }, TEST_TIMEOUT_MS);

  it("submits the selected reason", () => {
    // Arrange
    const onSubmit = vi.fn();
    render(<BadReasonPicker onSubmit={onSubmit} onCancel={vi.fn()} />);

    // Act
    fireEvent.click(screen.getByLabelText(BAD_REASON_LABELS.too_shallow));
    fireEvent.click(screen.getByRole("button", { name: "この理由で送信" }));

    // Assert
    expect(onSubmit).toHaveBeenCalledWith("too_shallow");
  }, TEST_TIMEOUT_MS);

  it("calls onCancel when the close operation is used", () => {
    // Arrange
    const onCancel = vi.fn();
    render(<BadReasonPicker onSubmit={vi.fn()} onCancel={onCancel} />);

    // Act
    fireEvent.click(screen.getByRole("button", { name: "閉じる" }));

    // Assert
    expect(onCancel).toHaveBeenCalled();
  }, TEST_TIMEOUT_MS);
});

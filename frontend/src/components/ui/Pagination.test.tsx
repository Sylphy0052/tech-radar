import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Pagination } from "@/components/ui/Pagination";
import { TEST_TIMEOUT_MS } from "@/test-utils/timeouts";

describe("Pagination", () => {
  it("does not render page number buttons when there is only one page", () => {
    // Arrange / Act
    render(<Pagination currentPage={1} totalPages={1} totalCount={3} onPageChange={vi.fn()} />);

    // Assert
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  }, TEST_TIMEOUT_MS);

  it("does not render page number buttons when there are zero pages", () => {
    // Arrange / Act
    render(<Pagination currentPage={1} totalPages={0} totalCount={0} onPageChange={vi.fn()} />);

    // Assert
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("shows the total count even when the pager itself is hidden", () => {
    // Arrange / Act
    render(<Pagination currentPage={1} totalPages={1} totalCount={42} onPageChange={vi.fn()} />);

    // Assert
    expect(screen.getByText("全42件")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("shows the total count alongside the page buttons", () => {
    // Arrange / Act
    render(<Pagination currentPage={1} totalPages={5} totalCount={97} onPageChange={vi.fn()} />);

    // Assert
    expect(screen.getByText("全97件")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("disables the previous button on the first page", () => {
    // Arrange / Act
    render(<Pagination currentPage={1} totalPages={5} totalCount={50} onPageChange={vi.fn()} />);

    // Assert
    expect(screen.getByRole("button", { name: "前のページへ" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "次のページへ" })).toBeEnabled();
  }, TEST_TIMEOUT_MS);

  it("disables the next button on the last page", () => {
    // Arrange / Act
    render(<Pagination currentPage={5} totalPages={5} totalCount={50} onPageChange={vi.fn()} />);

    // Assert
    expect(screen.getByRole("button", { name: "次のページへ" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "前のページへ" })).toBeEnabled();
  }, TEST_TIMEOUT_MS);

  it("enables both previous and next on a middle page", () => {
    // Arrange / Act
    render(<Pagination currentPage={3} totalPages={5} totalCount={50} onPageChange={vi.fn()} />);

    // Assert
    expect(screen.getByRole("button", { name: "前のページへ" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "次のページへ" })).toBeEnabled();
  }, TEST_TIMEOUT_MS);

  it("calls onPageChange with the clicked page number", () => {
    // Arrange — currentPage=3, totalPages=5 なら先頭・末尾・前後の間引きが
    // 発生せず 1〜5 全ページのボタンが揃う。
    const onPageChange = vi.fn();
    render(
      <Pagination currentPage={3} totalPages={5} totalCount={50} onPageChange={onPageChange} />,
    );

    // Act
    fireEvent.click(screen.getByRole("button", { name: "4ページ目へ" }));

    // Assert
    expect(onPageChange).toHaveBeenCalledWith(4);
  }, TEST_TIMEOUT_MS);

  it("calls onPageChange with currentPage + 1 when the next button is clicked", () => {
    // Arrange
    const onPageChange = vi.fn();
    render(
      <Pagination currentPage={2} totalPages={5} totalCount={50} onPageChange={onPageChange} />,
    );

    // Act
    fireEvent.click(screen.getByRole("button", { name: "次のページへ" }));

    // Assert
    expect(onPageChange).toHaveBeenCalledWith(3);
  }, TEST_TIMEOUT_MS);

  it("calls onPageChange with currentPage - 1 when the previous button is clicked", () => {
    // Arrange
    const onPageChange = vi.fn();
    render(
      <Pagination currentPage={2} totalPages={5} totalCount={50} onPageChange={onPageChange} />,
    );

    // Act
    fireEvent.click(screen.getByRole("button", { name: "前のページへ" }));

    // Assert
    expect(onPageChange).toHaveBeenCalledWith(1);
  }, TEST_TIMEOUT_MS);

  it("marks the current page button with aria-current", () => {
    // Arrange / Act
    render(<Pagination currentPage={3} totalPages={5} totalCount={50} onPageChange={vi.fn()} />);

    // Assert
    expect(screen.getByRole("button", { name: "3ページ目へ" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("button", { name: "2ページ目へ" })).not.toHaveAttribute(
      "aria-current",
    );
  }, TEST_TIMEOUT_MS);

  it("shows an ellipsis and keeps the first, last, and neighboring pages when there are many pages", () => {
    // Arrange / Act — 総ページ数20、現在10ページ目。先頭(1)・末尾(20)・
    // 現在の前後(9,10,11)だけが残り、間は省略記号になるはず。
    render(
      <Pagination currentPage={10} totalPages={20} totalCount={200} onPageChange={vi.fn()} />,
    );

    // Assert
    expect(screen.getByRole("button", { name: "1ページ目へ" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "9ページ目へ" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "10ページ目へ" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "11ページ目へ" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "20ページ目へ" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "5ページ目へ" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "15ページ目へ" })).not.toBeInTheDocument();
    expect(screen.getAllByText("…")).toHaveLength(2);
  }, TEST_TIMEOUT_MS);

  it("does not show an ellipsis when the near end leaves no gap to bridge", () => {
    // Arrange / Act — 総ページ数6、現在1ページ目。先頭(1)から前後(1,2)と
    // 末尾(6)の間は4,5が抜けるため、末尾側にだけ省略記号が1つ出るはず。
    render(<Pagination currentPage={1} totalPages={6} totalCount={60} onPageChange={vi.fn()} />);

    // Assert
    expect(screen.getByRole("button", { name: "1ページ目へ" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "2ページ目へ" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "6ページ目へ" })).toBeInTheDocument();
    expect(screen.getAllByText("…")).toHaveLength(1);
  }, TEST_TIMEOUT_MS);
});

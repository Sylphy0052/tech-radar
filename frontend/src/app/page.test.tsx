import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import Home from "@/app/page";

describe("Home", () => {
  it("renders the service name as the heading", () => {
    // Arrange / Act
    render(<Home />);

    // Assert
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("TechRadar");
  });
});

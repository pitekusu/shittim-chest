import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vite-plus/test";

import { Avatar } from "./Avatar";

afterEach(cleanup);

describe("Avatar", () => {
  it("uses the geometric placeholder when an avatar image cannot load", () => {
    render(
      <Avatar
        avatar={{
          kind: "image",
          url: "https://media.example.invalid/avatar.webp",
          alt: "依頼者のアバター",
          fallbackVariant: "cyan",
        }}
      />,
    );

    fireEvent.error(screen.getByRole("img", { name: "依頼者のアバター" }));

    expect(screen.queryByRole("img", { name: "依頼者のアバター" })).not.toBeInTheDocument();
    expect(screen.getByText("依頼者のアバター")).toBeInTheDocument();
  });
});

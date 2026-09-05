import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";

import { recordDetail } from "./test/recordsTestUtils";
import { VoteGraph } from "./components/VoteGraph";

const record = recordDetail();

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("VoteGraph", () => {
  it("renders accessible vote relations and highlights related routes", () => {
    render(<VoteGraph record={record} />);

    const outgoing = screen.getByLabelText("アロナがプラナに投票");
    const incoming = screen.getByLabelText("プラナがアロナに投票");
    const other = screen.getByLabelText("安倍晋三AIがアロナに投票");
    const node = screen.getByRole("button", { name: "アロナに関係する投票を強調" });

    fireEvent.focus(node);
    expect(outgoing.parentElement).toHaveAttribute("data-relation", "outgoing");
    expect(incoming.parentElement).toHaveAttribute("data-relation", "incoming");
    expect(other.parentElement).toHaveAttribute("data-relation", "incoming");

    fireEvent.focus(outgoing);
    expect(screen.getByRole("tooltip")).toHaveTextContent("アロナがプラナに投票");
    expect(outgoing.parentElement).toHaveAttribute("data-relation", "active");
    expect(incoming.parentElement).toHaveAttribute("data-relation", "unrelated");

    fireEvent.blur(outgoing);
    fireEvent.pointerEnter(outgoing, { pointerType: "touch" });
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    fireEvent.click(outgoing);
    expect(screen.getByRole("tooltip")).toHaveTextContent("アロナがプラナに投票");
  });

  it("uses click activation without treating a cancelled touch gesture as a selection", () => {
    render(<VoteGraph record={record} />);

    const node = screen.getByRole("button", { name: "アロナに関係する投票を強調" });
    const route = screen.getByLabelText("アロナがプラナに投票");

    fireEvent.touchStart(node);
    fireEvent.touchMove(node);
    fireEvent.touchEnd(node);
    expect(node).toHaveAttribute("aria-pressed", "false");
    expect(route.parentElement).toHaveAttribute("data-relation", "default");

    fireEvent.focus(node);
    fireEvent.click(node, { detail: 1 });
    expect(node).toHaveAttribute("aria-pressed", "true");
    expect(route.parentElement).toHaveAttribute("data-relation", "outgoing");

    fireEvent.click(node, { detail: 1 });
    expect(node).toHaveAttribute("aria-pressed", "false");
    expect(route.parentElement).toHaveAttribute("data-relation", "default");

    fireEvent.touchStart(route);
    fireEvent.touchMove(route);
    fireEvent.touchEnd(route);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();

    fireEvent.focus(route);
    fireEvent.click(route, { detail: 1 });
    expect(route).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("tooltip")).toHaveTextContent("アロナがプラナに投票");
    expect(route.parentElement).toHaveAttribute("data-relation", "active");

    fireEvent.click(route, { detail: 1 });
    expect(route).toHaveAttribute("aria-pressed", "false");
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    expect(route.parentElement).toHaveAttribute("data-relation", "default");
  });

  it("reveals only once after entering the viewport", async () => {
    let notify: IntersectionObserverCallback | undefined;
    const observe = vi.fn<(target: Element) => void>();
    const disconnect = vi.fn<() => void>();
    class MockIntersectionObserver implements IntersectionObserver {
      public readonly root = null;
      public readonly rootMargin = "";
      public readonly scrollMargin = "";
      public readonly thresholds = [0, 0.25];

      public constructor(callback: IntersectionObserverCallback) {
        notify = callback;
      }

      public observe = observe;
      public disconnect = disconnect;
      public unobserve = vi.fn<(target: Element) => void>();
      public takeRecords = () => [];
    }
    vi.stubGlobal("IntersectionObserver", MockIntersectionObserver);

    const { rerender } = render(<VoteGraph record={record} />);
    const graph = screen.getByTestId("vote-graph");
    expect(graph).toHaveAttribute("data-revealed", "false");

    notify?.(
      [{ isIntersecting: true, intersectionRatio: 0.25 } as IntersectionObserverEntry],
      {} as IntersectionObserver,
    );
    await waitFor(() => expect(graph).toHaveAttribute("data-revealed", "true"));

    rerender(<VoteGraph record={{ ...record }} />);
    expect(graph).toHaveAttribute("data-revealed", "true");
    expect(observe).toHaveBeenCalledOnce();
    expect(disconnect).not.toHaveBeenCalled();
  });

  it("creates collision-free definition identifiers for multiple graphs", () => {
    const { container } = render(
      <>
        <VoteGraph record={record} />
        <VoteGraph record={record} />
      </>,
    );
    const identifiers = [...container.querySelectorAll("[id]")].map((element) => element.id);
    expect(new Set(identifiers).size).toBe(identifiers.length);
  });
});

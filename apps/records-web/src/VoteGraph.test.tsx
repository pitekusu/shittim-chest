import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";

import type { RecordDetailResponse } from "./api";
import { VoteGraph } from "./VoteGraph";

const participant = (slot: "participant-a" | "participant-b" | "participant-c", name: string) => ({
  slot,
  displayName: name,
  avatar: {
    kind: "placeholder" as const,
    alt: `${name}のアバター`,
    fallbackVariant: "cyan" as const,
  },
});

const record: RecordDetailResponse = {
  schemaVersion: 1,
  recordId: "r".repeat(43),
  completedAt: "2026-08-15T06:00:00Z",
  question: "question",
  requester: {
    displayName: "requester",
    avatar: { kind: "placeholder", alt: "依頼者", fallbackVariant: "cyan" },
  },
  participants: [
    participant("participant-a", "アロナ"),
    participant("participant-b", "プラナ"),
    participant("participant-c", "安倍晋三AI"),
  ],
  initialOpinions: [
    { participant: "participant-a", summary: "a", proposal: "a" },
    { participant: "participant-b", summary: "b", proposal: "b" },
    { participant: "participant-c", summary: "c", proposal: "c" },
  ],
  finalProposals: [
    { participant: "participant-a", title: "a", proposal: "a" },
    { participant: "participant-b", title: "b", proposal: "b" },
    { participant: "participant-c", title: "c", proposal: "c" },
  ],
  votes: [
    { voter: "participant-a", candidate: "participant-b", reason: "a" },
    { voter: "participant-b", candidate: "participant-a", reason: "b" },
    { voter: "participant-c", candidate: "participant-a", reason: "c" },
  ],
  result: {
    winner: "participant-a",
    voteCounts: [
      { participant: "participant-a", count: 2 },
      { participant: "participant-b", count: 1 },
      { participant: "participant-c", count: 0 },
    ],
    tieBreakApplied: false,
  },
  finalDecision: {
    winner: "participant-a",
    victoryMessage: "victory",
    decision: "decision",
    actions: ["action"],
    caveats: ["caveat"],
  },
};

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

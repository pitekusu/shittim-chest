import { afterEach, describe, expect, it, vi } from "vite-plus/test";

import {
  getMemorialMemory,
  getMemorialState,
  MEMORIAL_QUERY_KEY,
  prepareMemorialUpload,
  queueMemorialGeneration,
  resetMemorial,
  uploadMemorialSource,
} from "./memorial";
import type { MemorialStateResponse, UploadResponse } from "./types";

const DIGEST = Uint8Array.from({ length: 32 }, (_, index) => index);
const DIGEST_HEX = Array.from(DIGEST, (value) => value.toString(16).padStart(2, "0")).join("");
const DIGEST_BASE64 = Buffer.from(DIGEST).toString("base64");

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function parseJsonBody(body: BodyInit | null | undefined): unknown {
  if (typeof body !== "string") throw new TypeError("expected a JSON string request body");
  return JSON.parse(body);
}

function lockedState(cycle = 1): MemorialStateResponse {
  return {
    schemaVersion: 1,
    state: "locked",
    cycle,
    resetCount: cycle - 1,
    unlockedParticipant: null,
    unlockedAt: null,
    uploadReady: false,
    latestReadyCycle: null,
    memories: [],
  };
}

function unlockedState(): MemorialStateResponse {
  return {
    schemaVersion: 1,
    state: "unlocked",
    cycle: 1,
    resetCount: 0,
    unlockedParticipant: "participant-a",
    unlockedAt: "2026-09-03T01:00:00Z",
    uploadReady: true,
    latestReadyCycle: null,
    memories: [],
  };
}

function readyState(): MemorialStateResponse {
  return {
    ...unlockedState(),
    state: "ready",
    uploadReady: false,
    latestReadyCycle: 1,
    memories: [
      {
        cycle: 1,
        participant: "participant-a",
        unlockedAt: "2026-09-03T01:00:00Z",
        generatedAt: "2026-09-03T01:05:00Z",
      },
    ],
  };
}

function memoryResponse(cycle = 1) {
  return {
    schemaVersion: 1,
    cycle,
    participant: "participant-a",
    unlockedAt: "2026-09-03T01:00:00Z",
    generatedAt: "2026-09-03T01:05:00Z",
    image: {
      url: "https://records.example.invalid/memorial.webp",
      width: 1920,
      height: 1080,
      alt: "アロナとのメモリアル",
    },
    narrative: "ふたりの思い出です。",
  } as const;
}

function uploadResponse(): UploadResponse {
  return {
    schemaVersion: 1,
    cycle: 1,
    method: "POST",
    uploadUrl: "https://upload.example.invalid/",
    expiresAt: "2026-09-03T01:10:00Z",
    fields: {
      key: "uploads/opaque-source",
      "Content-Type": "image/png",
      "x-amz-checksum-sha256": DIGEST_BASE64,
      "x-amz-algorithm": "AWS4-HMAC-SHA256",
      "x-amz-credential": "credential/scope",
      "x-amz-date": "20260903T010000Z",
      "x-amz-security-token": "session-token",
      policy: "cG9saWN5",
      "x-amz-signature": "d".repeat(64),
    },
  };
}

function imageFile(): File {
  const file = new File([Uint8Array.of(1, 2, 3)], "private-owner-name.png", {
    type: "image/png",
  });
  Object.defineProperty(file, "arrayBuffer", {
    value: vi.fn<Blob["arrayBuffer"]>(() => Promise.resolve(Uint8Array.of(1, 2, 3).buffer)),
  });
  return file;
}

type Digest = (algorithm: AlgorithmIdentifier, data: BufferSource) => Promise<ArrayBuffer>;

function installDigest() {
  const digest = vi.fn<Digest>(() => Promise.resolve(DIGEST.buffer));
  vi.stubGlobal("crypto", { subtle: { digest } });
  return digest;
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("Memorial API", () => {
  it("uses the Memorial query key and validates owner-scoped state", async () => {
    const state = readyState();
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(jsonResponse(state))),
    );

    expect(MEMORIAL_QUERY_KEY).toEqual(["memorial"]);
    await expect(getMemorialState()).resolves.toEqual(state);
    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/memorial",
      expect.objectContaining({ credentials: "same-origin" }),
    );
  });

  it("rejects unknown fields and inconsistent Memorial state", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({ ...lockedState(), ownerKey: "must-not-pass" }))
      .mockResolvedValueOnce(jsonResponse({ ...lockedState(), cycle: 2 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getMemorialState()).rejects.toMatchObject({ code: "INVALID_API_RESPONSE" });
    await expect(getMemorialState()).rejects.toMatchObject({ code: "INVALID_API_RESPONSE" });
  });

  it("hashes the image in the browser and sends CSRF and idempotency headers", async () => {
    const digest = installDigest();
    const ticket = uploadResponse();
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(ticket));
    vi.stubGlobal("fetch", fetchMock);
    const file = imageFile();

    await expect(
      prepareMemorialUpload(file, 1, "csrf-token", "upload-idempotency-key"),
    ).resolves.toEqual(ticket);

    expect(digest).toHaveBeenCalledWith("SHA-256", await file.arrayBuffer());
    const [, init] = fetchMock.mock.calls[0] ?? [];
    expect(parseJsonBody(init?.body)).toEqual({
      schemaVersion: 1,
      expectedCycle: 1,
      contentType: "image/png",
      sizeBytes: 3,
      sha256: DIGEST_HEX,
    });
    const headers = new Headers(init?.headers);
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(headers.get("X-CSRF-Token")).toBe("csrf-token");
    expect(headers.get("X-Idempotency-Key")).toBe("upload-idempotency-key");
    expect(init?.credentials).toBe("same-origin");
  });

  it("rejects invalid local files and an upload ticket for a different checksum", async () => {
    const fetchMock = vi.fn<typeof fetch>();
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      prepareMemorialUpload(
        new File([Uint8Array.of(1)], "source.gif", { type: "image/gif" }),
        1,
        "csrf-token",
        "idempotency-key",
      ),
    ).rejects.toMatchObject({ status: 400, code: "REQUEST_INVALID" });
    expect(fetchMock).not.toHaveBeenCalled();

    installDigest();
    fetchMock.mockResolvedValue(
      jsonResponse({
        ...uploadResponse(),
        fields: {
          ...uploadResponse().fields,
          "x-amz-checksum-sha256": Buffer.alloc(32, 255).toString("base64"),
        },
      }),
    );
    await expect(
      prepareMemorialUpload(imageFile(), 1, "csrf-token", "idempotency-key"),
    ).rejects.toMatchObject({ code: "INVALID_API_RESPONSE" });
  });

  it("posts all presigned fields before a privacy-safe file field", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(uploadMemorialSource(uploadResponse(), imageFile())).resolves.toBeUndefined();

    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(url).toBe("https://upload.example.invalid/");
    expect(init).toMatchObject({
      method: "POST",
      credentials: "omit",
      referrerPolicy: "no-referrer",
    });
    const body = init?.body;
    expect(body).toBeInstanceOf(FormData);
    const entries = Array.from((body as FormData).entries());
    expect(entries.map(([name]) => name)).toEqual([
      ...Object.keys(uploadResponse().fields),
      "file",
    ]);
    const uploadedFile = entries.at(-1)?.[1];
    expect(uploadedFile).toBeInstanceOf(File);
    expect((uploadedFile as File).name).toBe("memorial-source");
    expect((uploadedFile as File).name).not.toContain("private-owner-name");
  });

  it("does not retry a failed direct S3 upload", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 403 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(uploadMemorialSource(uploadResponse(), imageFile())).rejects.toMatchObject({
      status: 403,
      code: "MEMORIAL_UPLOAD_UNAVAILABLE",
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("queues generation and resets with exact confirmation bodies", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        jsonResponse({ ...unlockedState(), state: "queued", uploadReady: false }, 202),
      )
      .mockResolvedValueOnce(jsonResponse(lockedState(2)));
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      queueMemorialGeneration(1, "GENERATE MEMORIAL", "csrf-token", "generate-idempotency-key"),
    ).resolves.toMatchObject({ state: "queued" });
    await expect(
      resetMemorial(1, "RESET AFFECTION", "csrf-token", "reset-idempotency-key"),
    ).resolves.toEqual(lockedState(2));

    expect(fetchMock.mock.calls.map(([path]) => path)).toEqual([
      "/api/v1/memorial/generate",
      "/api/v1/memorial/reset",
    ]);
    expect(parseJsonBody(fetchMock.mock.calls[0]?.[1]?.body)).toEqual({
      schemaVersion: 1,
      expectedCycle: 1,
      confirmation: "GENERATE MEMORIAL",
    });
    expect(parseJsonBody(fetchMock.mock.calls[1]?.[1]?.body)).toEqual({
      schemaVersion: 1,
      expectedCycle: 1,
      confirmation: "RESET AFFECTION",
    });
    const generateHeaders = new Headers(fetchMock.mock.calls[0]?.[1]?.headers);
    const resetHeaders = new Headers(fetchMock.mock.calls[1]?.[1]?.headers);
    expect(generateHeaders.get("X-CSRF-Token")).toBe("csrf-token");
    expect(generateHeaders.get("X-Idempotency-Key")).toBe("generate-idempotency-key");
    expect(resetHeaders.get("X-Idempotency-Key")).toBe("reset-idempotency-key");
  });

  it("rejects generation and reset responses for unexpected cycles", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(lockedState(2), 202))
      .mockResolvedValueOnce(jsonResponse(lockedState(1)))
      .mockResolvedValueOnce(jsonResponse(lockedState(3)));
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      queueMemorialGeneration(1, "GENERATE MEMORIAL", "csrf-token", "generate-idempotency-key"),
    ).rejects.toMatchObject({ code: "INVALID_API_RESPONSE" });
    await expect(
      resetMemorial(1, "RESET AFFECTION", "csrf-token", "reset-idempotency-key"),
    ).rejects.toMatchObject({ code: "INVALID_API_RESPONSE" });
    await expect(
      resetMemorial(1, "RESET AFFECTION", "csrf-token", "reset-idempotency-key"),
    ).rejects.toMatchObject({ code: "INVALID_API_RESPONSE" });
  });

  it.each(["queued", "generating", "ready", "failed"] as const)(
    "accepts a %s response after generation is accepted",
    async (state) => {
      const response =
        state === "ready" ? readyState() : { ...unlockedState(), state, uploadReady: false };
      vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(response, 202)));

      await expect(
        queueMemorialGeneration(1, "GENERATE MEMORIAL", "csrf-token", "generate-idempotency-key"),
      ).resolves.toMatchObject({ state });
    },
  );

  it.each(["locked", "unlocked"] as const)(
    "rejects a %s response that does not prove generation was accepted",
    async (state) => {
      const response = state === "locked" ? lockedState() : unlockedState();
      vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(response, 202)));

      await expect(
        queueMemorialGeneration(1, "GENERATE MEMORIAL", "csrf-token", "generate-idempotency-key"),
      ).rejects.toMatchObject({ code: "INVALID_API_RESPONSE" });
    },
  );

  it.each(["unlocked", "queued", "generating", "failed"] as const)(
    "rejects a %s response after reset advances the cycle",
    async (state) => {
      const response: MemorialStateResponse = {
        ...unlockedState(),
        state,
        cycle: 2,
        resetCount: 1,
        uploadReady: state === "unlocked",
      };
      vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(response)));

      await expect(
        resetMemorial(1, "RESET AFFECTION", "csrf-token", "reset-idempotency-key"),
      ).rejects.toMatchObject({ code: "INVALID_API_RESPONSE" });
    },
  );

  it("rejects a ready response after reset advances the cycle", async () => {
    const response: MemorialStateResponse = {
      ...readyState(),
      cycle: 2,
      resetCount: 1,
      latestReadyCycle: 2,
      memories: [
        {
          ...readyState().memories[0]!,
          cycle: 2,
        },
      ],
    };
    vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(response)));

    await expect(
      resetMemorial(1, "RESET AFFECTION", "csrf-token", "reset-idempotency-key"),
    ).rejects.toMatchObject({ code: "INVALID_API_RESPONSE" });
  });

  it("binds a memory detail to the selected immutable summary", async () => {
    const summary = readyState().memories[0]!;
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(memoryResponse()))
      .mockResolvedValueOnce(jsonResponse({ ...memoryResponse(), cycle: 2 }))
      .mockResolvedValueOnce(jsonResponse({ ...memoryResponse(), participant: "participant-b" }))
      .mockResolvedValueOnce(
        jsonResponse({
          ...memoryResponse(),
          unlockedAt: "2026-09-03T10:00:00+09:00",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          ...memoryResponse(),
          generatedAt: "2026-09-03T10:05:00+09:00",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          ...memoryResponse(),
          generatedAt: "2026-09-03T00:59:00Z",
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    await expect(getMemorialMemory(summary)).resolves.toEqual(memoryResponse());
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/v1/memorial/memories/1");
    await expect(getMemorialMemory(summary)).rejects.toMatchObject({
      code: "INVALID_API_RESPONSE",
    });
    await expect(getMemorialMemory(summary)).rejects.toMatchObject({
      code: "INVALID_API_RESPONSE",
    });
    await expect(getMemorialMemory(summary)).rejects.toMatchObject({
      code: "INVALID_API_RESPONSE",
    });
    await expect(getMemorialMemory(summary)).rejects.toMatchObject({
      code: "INVALID_API_RESPONSE",
    });
    await expect(
      getMemorialMemory({ ...summary, generatedAt: "2026-09-03T00:59:00Z" }),
    ).rejects.toMatchObject({ code: "INVALID_API_RESPONSE" });
  });
});

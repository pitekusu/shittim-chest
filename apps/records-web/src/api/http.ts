import errorResponseValidator from "../generated/error-response-validator.mjs";

interface ErrorResponse {
  readonly error: {
    readonly code: string;
    readonly message: string;
    readonly requestId: string;
  };
}

export type ResponseValidator<T> = (value: unknown) => value is T;

export class RecordsApiError extends Error {
  public constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    public readonly requestId: string,
  ) {
    super(message);
  }
}

function isErrorResponse(value: unknown): value is ErrorResponse {
  return errorResponseValidator(value);
}

export async function requestJson<T>(
  path: string,
  validate: ResponseValidator<T>,
  init?: RequestInit,
): Promise<T> {
  const headers = new Headers(init?.headers);
  headers.set("Accept", "application/json");
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers,
  });
  const value: unknown = await response.json();
  if (!response.ok) {
    if (!isErrorResponse(value)) {
      throw new RecordsApiError(
        response.status,
        "INVALID_ERROR_RESPONSE",
        "エラー応答を確認できませんでした。",
        "local-validation",
      );
    }
    throw new RecordsApiError(
      response.status,
      value.error.code,
      value.error.message,
      value.error.requestId,
    );
  }
  if (!validate(value)) {
    throw new RecordsApiError(
      response.status,
      "INVALID_API_RESPONSE",
      "サーバーから不正な応答を受信しました。",
      "local-validation",
    );
  }
  return value;
}

export function parseErrorResponse(value: unknown): ErrorResponse | null {
  return isErrorResponse(value) ? value : null;
}

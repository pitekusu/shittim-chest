import { RecordsApiError, requestJson, type ResponseValidator } from "./http";

const RETRYABLE_STATUSES = new Set([429, 503]);

function waitForRetry(signal?: AbortSignal): Promise<void> {
  signal?.throwIfAborted();
  return new Promise((resolve, reject) => {
    const onAbort = () => {
      clearTimeout(timer);
      reject(signal?.reason);
    };
    const timer = setTimeout(
      () => {
        signal?.removeEventListener("abort", onAbort);
        resolve();
      },
      250 + Math.floor(Math.random() * 251),
    );
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

// Only GET requests retry, once. A cancelled route must not send the retry.
export async function requestAdminGet<T>(
  path: string,
  validate: ResponseValidator<T>,
  signal?: AbortSignal,
): Promise<T> {
  signal?.throwIfAborted();
  try {
    return await requestJson(path, validate, { signal });
  } catch (error) {
    if (!(error instanceof RecordsApiError) || !RETRYABLE_STATUSES.has(error.status)) throw error;
  }
  await waitForRetry(signal);
  signal?.throwIfAborted();
  return requestJson(path, validate, { signal });
}

import { useEffect, useRef, useState } from "react";

import { getRecord } from "../api/recordDetail";
import type { RecordAttachment } from "../api/types";
import { useAuthenticationRecovery } from "../hooks/useAuthenticationRecovery";
import styles from "../styles/detail.module.css";

function ImageViewer({
  image,
  index,
  failed,
  refreshing,
  revision,
  onFailure,
  onRefresh,
  onClose,
}: {
  readonly image: RecordAttachment;
  readonly index: number;
  readonly failed: boolean;
  readonly refreshing: boolean;
  readonly revision: number;
  readonly onFailure: () => void;
  readonly onRefresh: () => void;
  readonly onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [originalSize, setOriginalSize] = useState(false);
  useEffect(() => {
    const element = dialog.current;
    const previousFocus = document.activeElement;
    element?.showModal();
    return () => {
      element?.close();
      if (previousFocus instanceof HTMLElement) previousFocus.focus();
    };
  }, []);
  return (
    <dialog
      ref={dialog}
      className={styles.imageDialog}
      aria-label={`添付画像 ${index + 1}`}
      onCancel={onClose}
    >
      <div className={styles.imageActions}>
        <span>添付画像 {index + 1}</span>
        <button
          type="button"
          aria-pressed={originalSize}
          onClick={() => setOriginalSize(!originalSize)}
        >
          {originalSize ? "画面に合わせる" : "原寸で見る"}
        </button>
        <button type="button" onClick={onClose} autoFocus>
          閉じる
        </button>
      </div>
      {failed ? (
        <div className={styles.imageError}>
          <output>画像を読み込めませんでした。</output>
          <button type="button" onClick={onRefresh} disabled={refreshing}>
            {refreshing ? "画像を更新しています" : "画像を再読込"}
          </button>
        </div>
      ) : (
        <div className={styles.imageViewport} data-original-size={originalSize}>
          <img
            key={`${image.attachmentId}:${revision}`}
            src={image.url}
            alt={`議論に添付された画像 ${index + 1}`}
            width={image.width}
            height={image.height}
            referrerPolicy="no-referrer"
            onError={onFailure}
          />
        </div>
      )}
    </dialog>
  );
}

export function RecordImages({
  recordId,
  attachments,
}: {
  readonly recordId: string;
  readonly attachments: readonly RecordAttachment[];
}) {
  const [selected, setSelected] = useState<string | null>(null);
  const [updated, setUpdated] = useState<ReadonlyMap<string, RecordAttachment>>(new Map());
  const [failed, setFailed] = useState<ReadonlySet<string>>(new Set());
  const [refreshing, setRefreshing] = useState<string | null>(null);
  const [refreshError, setRefreshError] = useState<unknown>(null);
  const [revision, setRevision] = useState(0);
  const controller = useRef<AbortController | null>(null);
  useAuthenticationRecovery(refreshError);
  useEffect(() => () => controller.current?.abort(), []);

  function fail(id: string) {
    setFailed((current) => new Set([...current, id]));
  }

  async function refresh(id: string) {
    controller.current?.abort();
    const request = new AbortController();
    controller.current = request;
    setRefreshing(id);
    setRefreshError(null);
    try {
      const result = await getRecord(recordId, request.signal);
      const original = attachments.find((image) => image.attachmentId === id);
      const image = result.attachments?.find((candidate) => candidate.attachmentId === id);
      if (result.recordId !== recordId || !image || image.sha256 !== original?.sha256) {
        throw new Error("image reference changed");
      }
      if (request.signal.aborted) return;
      setUpdated((current) => new Map([...current, [id, image]]));
      setFailed((current) => new Set([...current].filter((candidate) => candidate !== id)));
      setRevision((current) => current + 1);
    } catch (error) {
      if (!request.signal.aborted) setRefreshError(error);
    } finally {
      if (!request.signal.aborted) setRefreshing(null);
    }
  }

  if (attachments.length === 0) return null;
  const index = attachments.findIndex((image) => image.attachmentId === selected);
  const selectedImage = index >= 0 ? (updated.get(selected!) ?? attachments[index]) : undefined;
  return (
    <section className={styles.attachments} aria-labelledby="attachments-title">
      <h2 id="attachments-title" className={styles.attachmentHeading}>
        添付画像 <span>{attachments.length}枚</span>
      </h2>
      <div className={styles.attachmentGrid}>
        {attachments.map((original, index) => {
          const image = updated.get(original.attachmentId) ?? original;
          return (
            <div key={image.attachmentId} className={styles.attachmentItem}>
              <button
                type="button"
                className={styles.attachmentButton}
                aria-label={`添付画像 ${index + 1}を拡大`}
                aria-haspopup="dialog"
                onClick={() => setSelected(image.attachmentId)}
              >
                {failed.has(image.attachmentId) ? (
                  <span className={styles.attachmentFallback}>画像を読み込めませんでした</span>
                ) : (
                  <img
                    key={`${image.attachmentId}:${revision}`}
                    src={image.thumbnailUrl ?? image.url}
                    alt={`議論に添付された画像 ${index + 1}`}
                    loading="lazy"
                    decoding="async"
                    referrerPolicy="no-referrer"
                    onError={() => fail(image.attachmentId)}
                  />
                )}
                <span className={styles.attachmentNumber} aria-hidden="true">
                  {index + 1}
                </span>
              </button>
              {failed.has(image.attachmentId) && (
                <button
                  type="button"
                  onClick={() => void refresh(image.attachmentId)}
                  disabled={refreshing !== null}
                >
                  {refreshing === image.attachmentId ? "画像を更新しています" : "画像を再読込"}
                </button>
              )}
            </div>
          );
        })}
      </div>
      {refreshError !== null && <output>画像を更新できませんでした。再度お試しください。</output>}
      {selectedImage && (
        <ImageViewer
          key={selectedImage.attachmentId}
          image={selectedImage}
          index={index}
          failed={failed.has(selectedImage.attachmentId)}
          refreshing={refreshing !== null}
          revision={revision}
          onFailure={() => fail(selectedImage.attachmentId)}
          onRefresh={() => void refresh(selectedImage.attachmentId)}
          onClose={() => setSelected(null)}
        />
      )}
    </section>
  );
}

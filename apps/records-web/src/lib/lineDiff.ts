export type LineDiffKind = "context" | "removed" | "added";

export interface LineDiffEntry {
  readonly kind: LineDiffKind;
  readonly beforeLine: number | null;
  readonly afterLine: number | null;
  readonly text: string;
}

const MAX_LCS_CELLS = 1_000_000;

function lines(value: string): readonly string[] {
  return value.replaceAll("\r\n", "\n").replaceAll("\r", "\n").split("\n");
}

function coarseDiff(before: readonly string[], after: readonly string[]): readonly LineDiffEntry[] {
  let prefix = 0;
  while (prefix < before.length && prefix < after.length && before[prefix] === after[prefix]) {
    prefix += 1;
  }
  let suffix = 0;
  while (
    suffix < before.length - prefix &&
    suffix < after.length - prefix &&
    before[before.length - suffix - 1] === after[after.length - suffix - 1]
  ) {
    suffix += 1;
  }

  const entries: LineDiffEntry[] = [];
  for (let index = 0; index < prefix; index += 1) {
    entries.push({
      kind: "context",
      beforeLine: index + 1,
      afterLine: index + 1,
      text: before[index] ?? "",
    });
  }
  for (let index = prefix; index < before.length - suffix; index += 1) {
    entries.push({
      kind: "removed",
      beforeLine: index + 1,
      afterLine: null,
      text: before[index] ?? "",
    });
  }
  for (let index = prefix; index < after.length - suffix; index += 1) {
    entries.push({
      kind: "added",
      beforeLine: null,
      afterLine: index + 1,
      text: after[index] ?? "",
    });
  }
  for (let offset = suffix; offset > 0; offset -= 1) {
    const beforeIndex = before.length - offset;
    const afterIndex = after.length - offset;
    entries.push({
      kind: "context",
      beforeLine: beforeIndex + 1,
      afterLine: afterIndex + 1,
      text: before[beforeIndex] ?? "",
    });
  }
  return entries;
}

export function lineDiff(beforeValue: string, afterValue: string): readonly LineDiffEntry[] {
  const before = lines(beforeValue);
  const after = lines(afterValue);
  if ((before.length + 1) * (after.length + 1) > MAX_LCS_CELLS) {
    return coarseDiff(before, after);
  }

  const columns = after.length + 1;
  const lcs = new Uint16Array((before.length + 1) * columns);
  for (let beforeIndex = before.length - 1; beforeIndex >= 0; beforeIndex -= 1) {
    for (let afterIndex = after.length - 1; afterIndex >= 0; afterIndex -= 1) {
      const cell = beforeIndex * columns + afterIndex;
      lcs[cell] =
        before[beforeIndex] === after[afterIndex]
          ? (lcs[(beforeIndex + 1) * columns + afterIndex + 1] ?? 0) + 1
          : Math.max(
              lcs[(beforeIndex + 1) * columns + afterIndex] ?? 0,
              lcs[beforeIndex * columns + afterIndex + 1] ?? 0,
            );
    }
  }

  const entries: LineDiffEntry[] = [];
  let beforeIndex = 0;
  let afterIndex = 0;
  while (beforeIndex < before.length || afterIndex < after.length) {
    if (
      beforeIndex < before.length &&
      afterIndex < after.length &&
      before[beforeIndex] === after[afterIndex]
    ) {
      entries.push({
        kind: "context",
        beforeLine: beforeIndex + 1,
        afterLine: afterIndex + 1,
        text: before[beforeIndex] ?? "",
      });
      beforeIndex += 1;
      afterIndex += 1;
      continue;
    }
    const removeScore = lcs[(beforeIndex + 1) * columns + afterIndex] ?? -1;
    const addScore = lcs[beforeIndex * columns + afterIndex + 1] ?? -1;
    if (beforeIndex < before.length && (afterIndex >= after.length || removeScore >= addScore)) {
      entries.push({
        kind: "removed",
        beforeLine: beforeIndex + 1,
        afterLine: null,
        text: before[beforeIndex] ?? "",
      });
      beforeIndex += 1;
    } else {
      entries.push({
        kind: "added",
        beforeLine: null,
        afterLine: afterIndex + 1,
        text: after[afterIndex] ?? "",
      });
      afterIndex += 1;
    }
  }
  return entries;
}

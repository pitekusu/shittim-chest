const COMPLETED_DATE_TIME_FORMAT = new Intl.DateTimeFormat("ja-JP", {
  timeZone: "Asia/Tokyo",
  year: "numeric",
  month: "long",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

export function formatCompletedDateTime(value: string): string {
  return COMPLETED_DATE_TIME_FORMAT.format(new Date(value));
}

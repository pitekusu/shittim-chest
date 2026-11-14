import { Combobox } from "@base-ui/react/combobox";
import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import {
  lazy,
  Suspense,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type MouseEvent,
} from "react";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";

import type { GalleryCard } from "../api/gallery";
import { RecordsApiError } from "../api/http";
import { AvatarSelect, type AvatarSelectOption } from "../components/AvatarSelect";
import { GalleryGrid, galleryCollectorName } from "../components/GalleryGrid";
import { useAuthenticationRecovery } from "../hooks/useAuthenticationRecovery";
import { filterGallery, galleryListQuery, galleryPerformers, gallerySorts } from "../lib/gallery";
import styles from "../styles/gallery.module.css";

const GalleryViewer = lazy(() => import("../components/GalleryViewer"));

const sorts = Object.entries(gallerySorts);
const performers = Object.entries(galleryPerformers);
const rarities: readonly (readonly [string, string])[] = [
  ["0", "★0"],
  ["1", "★1"],
  ["2", "★2"],
  ["3", "★3"],
  ["4", "★4"],
  ["unrecorded", "未記録"],
];

function choices(entries: Iterable<readonly [string, string]>): AvatarSelectOption<string>[] {
  return Array.from(entries, ([value, label]) => ({ value, label, avatar: null }));
}

function ordered(values: Map<string, string>) {
  return [...values].sort((left, right) => left[1].localeCompare(right[1], "ja"));
}

function choiceValue(options: readonly AvatarSelectOption<string>[], value: string | null) {
  return options.find((option) => option.value === value)?.value ?? options[0]?.value ?? "";
}

function SearchableFilter({
  label,
  options,
  value,
  onChange,
}: {
  readonly label: string;
  readonly options: readonly AvatarSelectOption<string>[];
  readonly value: string;
  readonly onChange: (value: string) => void;
}) {
  const id = useId();
  const selected = options.find((option) => option.value === value) ?? null;
  return (
    <div className={styles.searchFilter}>
      <label htmlFor={id}>{label}</label>
      <Combobox.Root
        items={options}
        value={selected}
        isItemEqualToValue={(option, current) => option.value === current.value}
        onValueChange={(next) => onChange(next?.value ?? "")}
        openOnInputClick
      >
        <Combobox.InputGroup className={styles.comboboxGroup}>
          <Combobox.Input id={id} className={styles.comboboxInput} placeholder="すべて" />
          <Combobox.Trigger
            className={styles.comboboxTrigger}
            aria-label={`${label}の選択肢を表示`}
          >
            <Combobox.Icon aria-hidden="true">⌄</Combobox.Icon>
          </Combobox.Trigger>
        </Combobox.InputGroup>
        <Combobox.Portal>
          <Combobox.Positioner className={styles.comboboxPositioner} sideOffset={8}>
            <Combobox.Popup className={styles.comboboxPopup}>
              <Combobox.Empty className={styles.comboboxEmpty}>
                該当する候補がありません。
              </Combobox.Empty>
              <Combobox.List>
                {(option: AvatarSelectOption<string>) => (
                  <Combobox.Item key={option.value} value={option} className={styles.comboboxItem}>
                    <span>{option.label}</span>
                    <Combobox.ItemIndicator aria-hidden="true">✓</Combobox.ItemIndicator>
                  </Combobox.Item>
                )}
              </Combobox.List>
            </Combobox.Popup>
          </Combobox.Positioner>
        </Combobox.Portal>
      </Combobox.Root>
    </div>
  );
}

function GalleryFilters({
  cards,
  filters,
  onChange,
}: {
  readonly cards: readonly GalleryCard[];
  readonly filters: URLSearchParams;
  readonly onChange: (key: string, value: string) => void;
}) {
  const schools = new Map<string, string>();
  const collectors = new Map<string, string>();
  for (const card of cards) {
    schools.set(card.schoolName, card.schoolName);
    if (card.collector) collectors.set(card.collector.id, galleryCollectorName(card));
  }
  const sections = [
    {
      key: "collector",
      label: "獲得者",
      options: choices([
        ["", "すべて"],
        ["mine", "自分"],
        ...ordered(collectors),
        ["unknown", "不明"],
      ]),
    },
    { key: "school", label: "学校", options: choices([["", "すべて"], ...ordered(schools)]) },
    { key: "rarity", label: "レア度", options: choices([["", "すべて"], ...rarities]) },
    { key: "performer", label: "着用者", options: choices([["", "すべて"], ...performers]) },
    { key: "sort", label: "並べ替え", options: choices(sorts) },
  ];
  return (
    <fieldset className={styles.filters} aria-label="ギャラリーの表示条件">
      {sections.map(({ key, label, options }) => {
        const Filter = key === "collector" || key === "school" ? SearchableFilter : AvatarSelect;
        return (
          <Filter
            key={key}
            label={label}
            options={options}
            value={choiceValue(options, filters.get(key))}
            onChange={(value) => onChange(key, value)}
          />
        );
      })}
    </fieldset>
  );
}

export default function GalleryPage({ scope }: { readonly scope: number }) {
  const { cardId } = useParams<{ cardId?: string }>();
  const [viewerRequested, setViewerRequested] = useState(Boolean(cardId));
  if (cardId && !viewerRequested) setViewerRequested(true);
  const navigate = useNavigate();
  const location = useLocation();
  const [filters, setFilters] = useSearchParams();
  const trigger = useRef<HTMLAnchorElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const queryClient = useQueryClient();
  const listOptions = galleryListQuery(scope);
  const query = useInfiniteQuery(listOptions);
  useAuthenticationRecovery(query.error);
  const { fetchNextPage, hasNextPage, isFetching, isFetchNextPageError } = query;
  useEffect(() => {
    if (hasNextPage && !isFetching && !isFetchNextPageError) void fetchNextPage();
  }, [fetchNextPage, hasNextPage, isFetching, isFetchNextPageError]);
  const cards = useMemo(() => {
    const unique = new Map<string, GalleryCard>();
    for (const page of query.data?.pages ?? []) {
      for (const card of page.items) if (!unique.has(card.cardId)) unique.set(card.cardId, card);
    }
    return [...unique.values()];
  }, [query.data]);
  const results = useMemo(() => filterGallery(cards, filters), [cards, filters]);
  const fullyLoaded = query.isSuccess && !hasNextPage;
  const hasFilters = ["collector", "school", "rarity", "performer"].some((key) => filters.has(key));
  const search = location.search;
  const openedFromGallery = location.state?.galleryOpened === true;
  const setFilter = (key: string, value: string) => {
    setFilters((current) => {
      const next = new URLSearchParams(current);
      if (!value || (key === "sort" && value === "newest")) next.delete(key);
      else next.set(key, value);
      return next;
    });
  };
  const onOpen = (event: MouseEvent<HTMLAnchorElement>, selected: GalleryCard) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey)
      return;
    event.preventDefault();
    trigger.current = event.currentTarget;
    void navigate(
      { pathname: `/gallery/${selected.cardId}`, search },
      { state: { galleryOpened: true } },
    );
  };
  return (
    <section className={styles.page} data-route-motion-ready="">
      <header className={styles.header}>
        <h1 ref={heading} tabIndex={-1}>
          生徒ガチャ
        </h1>
      </header>
      <GalleryFilters cards={cards} filters={filters} onChange={setFilter} />
      <div className={styles.resultsBar}>
        <output>{`${results.length.toLocaleString("ja-JP")}枚${fullyLoaded ? "" : query.isError ? " · 読み込み中断" : " · 読み込み中…"}`}</output>
        {hasFilters && (
          <button
            className={styles.secondary}
            type="button"
            onClick={() => {
              const sort = filters.get("sort");
              setFilters(sort && sort !== "newest" ? { sort } : {});
            }}
          >
            絞り込みを解除
          </button>
        )}
      </div>
      {query.isError && (
        <div className={styles.empty} role="alert">
          <p>カードの一覧を読み込めませんでした。</p>
          <button
            className={styles.secondary}
            type="button"
            onClick={() => {
              if (query.error instanceof RecordsApiError && query.error.code === "CURSOR_INVALID") {
                void queryClient.resetQueries({ queryKey: listOptions.queryKey, exact: true });
              } else if (isFetchNextPageError) void fetchNextPage();
              else void query.refetch();
            }}
          >
            もう一度試す
          </button>
        </div>
      )}
      {fullyLoaded && results.length === 0 && (
        <p className={styles.empty}>
          {hasFilters
            ? "条件に一致するカードはありません。"
            : "獲得した生徒カードはまだありません。"}
        </p>
      )}
      {results.length > 0 && (
        <GalleryGrid cards={results} scope={scope} search={search} onOpen={onOpen} />
      )}
      {viewerRequested && (
        <Suspense fallback={<output className={styles.empty}>拡大表示を準備中…</output>}>
          <GalleryViewer
            scope={scope}
            cardId={cardId}
            cards={results}
            fallbackFocus={heading}
            trigger={trigger}
            onSelect={(next) =>
              void navigate(
                { pathname: `/gallery/${next}`, search },
                { replace: true, state: location.state },
              )
            }
            onClose={() => {
              if (openedFromGallery) void navigate(-1);
              else void navigate({ pathname: "/gallery", search }, { replace: true });
            }}
          />
        </Suspense>
      )}
    </section>
  );
}

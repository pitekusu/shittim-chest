import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense, useEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { getGallerySchoolLogos, type GalleryCard } from "../api/gallery";
import { RecordsApiError } from "../api/http";
import { AvatarSelect, type AvatarSelectOption } from "../components/AvatarSelect";
import { GalleryGrid } from "../components/GalleryGrid";
import { GalleryCollectorIcon, GallerySchoolIcon } from "../components/GalleryMetadata";
import { useAuthenticationRecovery } from "../hooks/useAuthenticationRecovery";
import {
  filterGallery,
  galleryCollectorName,
  galleryListQuery,
  galleryPerformers,
  galleryRarityAriaLabel,
  galleryRarityLabel,
  gallerySorts,
} from "../lib/gallery";
import { personaAvatars } from "../lib/personaAvatars";
import styles from "../styles/gallery.module.css";

const GalleryViewer = lazy(() => import("../components/GalleryViewer"));
const emptySchoolLogos: Readonly<Record<string, string>> = {};

const allOption = { value: "all", label: "すべて", avatar: null } as const;
const sorts = Object.entries(gallerySorts).map(([value, label]) => ({
  value,
  label,
  avatar: null,
}));
const performers = (Object.keys(galleryPerformers) as GalleryCard["performer"][]).map((value) => ({
  value,
  label: galleryPerformers[value],
  avatar: { ...personaAvatars[value], alt: "" },
}));
const rarities = ([0, 1, 2, 3, 4, null] as const).map((rarity) => ({
  value: String(rarity ?? "unrecorded"),
  label: galleryRarityLabel(rarity),
  valueAriaLabel: galleryRarityAriaLabel(rarity),
  avatar: null,
}));

const byLabel = (left: AvatarSelectOption<string>, right: AvatarSelectOption<string>) =>
  left.label.localeCompare(right.label, "ja");

function GalleryFilters({
  cards,
  schoolLogos,
  filters,
  onChange,
}: {
  readonly cards: readonly GalleryCard[];
  readonly schoolLogos: Readonly<Record<string, string>>;
  readonly filters: URLSearchParams;
  readonly onChange: (key: string, value: string) => void;
}) {
  const schools = new Map<string, AvatarSelectOption<string>>();
  const collectors = new Map<string, AvatarSelectOption<string>>();
  for (const card of cards) {
    if (!schools.has(card.schoolName)) {
      schools.set(card.schoolName, {
        value: card.schoolName,
        label: card.schoolName,
        avatar: null,
        icon: <GallerySchoolIcon logo={schoolLogos[card.schoolName]} />,
      });
    }
    if (card.collector && !collectors.has(card.collector.id)) {
      collectors.set(card.collector.id, {
        value: card.collector.id,
        label: galleryCollectorName(card),
        avatar: null,
        icon: <GalleryCollectorIcon avatar={card.collector.avatar ?? null} />,
      });
    }
  }
  const sections = [
    {
      key: "collector",
      label: "獲得者",
      options: [
        ...[...collectors.values()].sort(byLabel),
        { value: "mine", label: "自分", avatar: null },
        { value: "unknown", label: "不明", avatar: null },
        allOption,
      ],
    },
    { key: "school", label: "学校", options: [...[...schools.values()].sort(byLabel), allOption] },
    { key: "rarity", label: "レア度", options: [allOption, ...rarities] },
    { key: "performer", label: "アロプラ安倍", options: [allOption, ...performers] },
    { key: "sort", label: "並べ替え", options: sorts },
  ];
  return (
    <fieldset className={styles.filters} aria-label="ギャラリーの表示条件">
      {sections.map(({ key, label, options }) => {
        const selected = filters.get(key);
        const value = options.find((option) => option.value === selected)?.value;
        return (
          <AvatarSelect
            key={key}
            label={label}
            options={options}
            value={value ?? (key === "sort" ? "newest" : allOption.value)}
            onChange={(next) => onChange(key, next === allOption.value ? "" : next)}
            animatedChevron
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
  const logoQuery = useQuery({
    queryKey: ["gallery", scope, "school-logos"],
    queryFn: ({ signal }) => getGallerySchoolLogos(signal),
    staleTime: 5 * 60_000,
    enabled: cards.length > 0 || Boolean(cardId),
  });
  useAuthenticationRecovery(logoQuery.error);
  const schoolLogos = logoQuery.data?.logos ?? emptySchoolLogos;
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
      <GalleryFilters
        cards={cards}
        schoolLogos={schoolLogos}
        filters={filters}
        onChange={setFilter}
      />
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
        <GalleryGrid
          cards={results}
          schoolLogos={schoolLogos}
          scope={scope}
          search={search}
          onOpen={onOpen}
        />
      )}
      {viewerRequested && (
        <Suspense fallback={<output className={styles.empty}>拡大表示を準備中…</output>}>
          <GalleryViewer
            scope={scope}
            cardId={cardId}
            cards={results}
            schoolLogos={schoolLogos}
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

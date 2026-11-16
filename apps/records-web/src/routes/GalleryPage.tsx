import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  lazy,
  Suspense,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent,
} from "react";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";

import {
  getGalleryRoster,
  getGallerySchoolLogos,
  type GalleryCollectionCard,
} from "../api/gallery";
import { RecordsApiError } from "../api/http";
import { AvatarSelect, type AvatarSelectOption } from "../components/AvatarSelect";
import { GalleryGrid, type GalleryGridReturn } from "../components/GalleryGrid";
import { GallerySchoolIcon } from "../components/GalleryMetadata";
import { useAuthenticationRecovery } from "../hooks/useAuthenticationRecovery";
import {
  filterGallery,
  galleryCollection,
  galleryCollectionName,
  galleryCollectionSchool,
  galleryListQuery,
  galleryRarityAriaLabel,
  galleryRarityLabel,
  gallerySorts,
  type GalleryCollectionEntry,
} from "../lib/gallery";
import styles from "../styles/gallery.module.css";

const GalleryViewer = lazy(() => import("../components/GalleryViewer"));
const emptySchoolLogos: Readonly<Record<string, string>> = {};
const emptyEntries: readonly GalleryCollectionEntry[] = [];
const filterKeys = ["q", "school", "rarity", "acquired"] as const;

function isPlainClick(event: MouseEvent<HTMLAnchorElement>) {
  return event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey;
}

const allOption = { value: "all", label: "すべて", avatar: null } as const;
const sorts = Object.entries(gallerySorts).map(([value, label]) => ({
  value,
  label,
  avatar: null,
}));
const rarities = ([1, 2, 3, 4, 0] as const).map((rarity) => ({
  value: String(rarity),
  label: galleryRarityLabel(rarity),
  valueAriaLabel: galleryRarityAriaLabel(rarity),
  avatar: null,
}));

function GalleryFilters({
  entries,
  schoolLogos,
  filters,
  onChange,
}: {
  readonly entries: readonly GalleryCollectionEntry[];
  readonly schoolLogos: Readonly<Record<string, string>>;
  readonly filters: URLSearchParams;
  readonly onChange: (key: string, value: string) => void;
}) {
  const nameQuery = filters.get("q") ?? "";
  const [nameInput, setNameInput] = useState({ query: nameQuery, value: nameQuery });
  const composingName = useRef(false);
  if (nameInput.query !== nameQuery) {
    setNameInput({ query: nameQuery, value: nameQuery });
  }
  const changeName = (value: string) => {
    setNameInput({ query: nameQuery, value });
    // Replacing the URL during composition interrupts the IME's input session.
    if (!composingName.current && value !== nameQuery) onChange("q", value);
  };
  const acquiredQuery = filters.get("acquired") === "1";
  const [acquiredInput, setAcquiredInput] = useState({
    query: acquiredQuery,
    checked: acquiredQuery,
  });
  if (acquiredInput.query !== acquiredQuery) {
    setAcquiredInput({ query: acquiredQuery, checked: acquiredQuery });
  }

  const schools = new Set<string>();
  for (const entry of entries) {
    const name = galleryCollectionSchool(entry);
    if (name) schools.add(name);
  }
  const schoolOptions: AvatarSelectOption<string>[] = [...schools]
    .sort((left, right) => left.localeCompare(right, "ja"))
    .map((school) => ({
      value: school,
      label: school,
      avatar: null,
      icon: <GallerySchoolIcon logo={schoolLogos[school]} />,
    }));
  const sections = [
    { key: "school", label: "学校", options: [allOption, ...schoolOptions] },
    { key: "rarity", label: "レア度", options: [allOption, ...rarities] },
    { key: "sort", label: "並べ替え", options: sorts },
  ];
  return (
    <fieldset className={styles.filters} aria-label="生徒図鑑の表示条件">
      <label className={styles.searchField}>
        生徒名
        <input
          type="search"
          value={nameInput.value}
          onChange={(event) => changeName(event.currentTarget.value)}
          onCompositionStart={() => {
            composingName.current = true;
          }}
          onCompositionEnd={(event) => {
            composingName.current = false;
            changeName(event.currentTarget.value);
          }}
          placeholder="生徒名を入力"
        />
      </label>
      {sections.map(({ key, label, options }) => {
        const selected = filters.get(key);
        const value = options.find((option) => option.value === selected)?.value;
        return (
          <div key={key} className={key === "sort" ? styles.sortField : undefined}>
            <AvatarSelect
              label={label}
              options={options}
              value={value ?? (key === "sort" ? "commonest" : allOption.value)}
              onChange={(next) => onChange(key, next === allOption.value ? "" : next)}
              animatedChevron
            />
          </div>
        );
      })}
      <label className={styles.acquiredFilter}>
        <input
          type="checkbox"
          checked={acquiredInput.checked}
          onChange={(event) => {
            const { checked } = event.currentTarget;
            setAcquiredInput({ query: acquiredQuery, checked });
            onChange("acquired", checked ? "1" : "");
          }}
        />
        獲得済みのみ表示
      </label>
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
  const groupHeading = useRef<HTMLHeadingElement>(null);
  const [collectionReturn, setCollectionReturn] = useState<GalleryGridReturn>();
  const studentId = filters.get("student");
  const hasFilters = filterKeys.some((key) => filters.has(key));
  const collectionSearch = new URLSearchParams();
  for (const key of [...filterKeys, "sort"]) {
    const value = filters.get(key);
    if (value) collectionSearch.set(key, value);
  }
  const gridParams = new URLSearchParams(collectionSearch);
  if (studentId !== null) gridParams.set("student", studentId);
  const gridSearch = gridParams.toString();
  const openedFromGallery = location.state?.galleryOpened === true;
  const openedFromCollection = location.state?.galleryGroupOpened === true;
  const queryClient = useQueryClient();
  const listOptions = galleryListQuery(scope);
  const query = useInfiniteQuery(listOptions);
  useAuthenticationRecovery(query.error);
  const { fetchNextPage, hasNextPage, isFetching, isFetchNextPageError } = query;
  useEffect(() => {
    if (hasNextPage && !isFetching && !isFetchNextPageError) void fetchNextPage();
  }, [fetchNextPage, hasNextPage, isFetching, isFetchNextPageError]);
  const cards = useMemo(() => {
    const unique = new Map<string, GalleryCollectionCard>();
    for (const page of query.data?.pages ?? []) {
      for (const card of page.items) if (!unique.has(card.cardId)) unique.set(card.cardId, card);
    }
    return [...unique.values()];
  }, [query.data]);
  const roster = useQuery({
    queryKey: ["gallery", scope, "roster"],
    queryFn: ({ signal }) => getGalleryRoster(signal),
  });
  useAuthenticationRecovery(roster.error);
  const logoQuery = useQuery({
    queryKey: ["gallery", scope, "school-logos"],
    queryFn: ({ signal }) => getGallerySchoolLogos(signal),
    staleTime: 5 * 60_000,
    enabled: Boolean(roster.data || cardId),
  });
  useAuthenticationRecovery(logoQuery.error);
  const schoolLogos = logoQuery.data?.logos ?? emptySchoolLogos;
  const fullyLoaded = query.data?.pages.at(-1)?.nextCursor === null;
  const collection = useMemo(
    () => (fullyLoaded && roster.data ? galleryCollection(roster.data.items, cards) : undefined),
    [fullyLoaded, roster.data, cards],
  );
  const selected = collection?.find(
    (entry) => entry.studentId === studentId && entry.cards.length > 0,
  );
  const groupAvailable = selected !== undefined;
  useLayoutEffect(() => {
    if (!groupAvailable) return;
    window.scrollTo(0, 0);
    groupHeading.current?.focus({ preventScroll: true });
  }, [groupAvailable, studentId]);

  const entries = useMemo(
    () => (collection ? filterGallery(collection, filters) : emptyEntries),
    [collection, filters],
  );
  const viewerCards = useMemo(
    () => selected?.cards ?? entries.flatMap((entry) => entry.cards.slice(0, 1)),
    [entries, selected],
  );
  const retryCards = () => {
    if (query.error instanceof RecordsApiError && query.error.code === "CURSOR_INVALID") {
      void queryClient.resetQueries({ queryKey: listOptions.queryKey, exact: true });
    } else if (isFetchNextPageError) void fetchNextPage();
    else void query.refetch();
  };
  const setFilter = (key: string, value: string) => {
    setFilters(
      () => {
        const next = new URLSearchParams(collectionSearch);
        if (!value || (key === "sort" && value === "commonest")) next.delete(key);
        else next.set(key, value);
        return next;
      },
      { replace: key === "q" },
    );
  };
  const onOpen = (event: MouseEvent<HTMLAnchorElement>) => {
    if (isPlainClick(event)) trigger.current = event.currentTarget;
  };
  const onOpenGroup = (event: MouseEvent<HTMLAnchorElement>, position: GalleryGridReturn) => {
    if (isPlainClick(event)) setCollectionReturn(position);
  };
  return (
    <section className={styles.page} data-route-motion-ready="">
      <header className={styles.header}>
        <h1 ref={heading} tabIndex={-1}>
          生徒図鑑
        </h1>
      </header>
      {studentId !== null ? (
        <button
          className={styles.backButton}
          type="button"
          onClick={() => {
            if (openedFromCollection) void navigate(-1);
            else
              void navigate(
                { pathname: "/gallery", search: collectionSearch.toString() },
                { replace: true },
              );
          }}
        >
          図鑑に戻る
        </button>
      ) : (
        <GalleryFilters
          entries={collection ?? emptyEntries}
          schoolLogos={schoolLogos}
          filters={collectionSearch}
          onChange={setFilter}
        />
      )}
      {(studentId === null || !collection) && (
        <div className={styles.resultsBar}>
          <output>
            {collection
              ? `獲得済み ${collection.filter((entry) => entry.cards.length > 0).length.toLocaleString("ja-JP")} / ${collection.length.toLocaleString("ja-JP")}体`
              : query.isError || roster.isError
                ? "図鑑の読み込み中断"
                : "図鑑を読み込み中…"}
          </output>
          {studentId === null && hasFilters && (
            <button
              className={styles.secondary}
              type="button"
              onClick={() => {
                const sort = collectionSearch.get("sort");
                setFilters(sort ? { sort } : {});
              }}
            >
              絞り込みを解除
            </button>
          )}
        </div>
      )}
      {roster.isError && (
        <div className={styles.empty} role="alert">
          <p>生徒の名簿を読み込めませんでした。</p>
          <button className={styles.secondary} type="button" onClick={() => void roster.refetch()}>
            もう一度試す
          </button>
        </div>
      )}
      {studentId !== null && collection && !selected && (
        <p className={styles.empty}>この生徒の獲得カードは見つかりませんでした。</p>
      )}
      {selected && (
        <>
          <h2 className={styles.groupTitle} ref={groupHeading} tabIndex={-1}>
            {galleryCollectionName(selected)}
          </h2>
          <GalleryGrid
            key={selected.studentId}
            entries={selected.cards}
            label={`${galleryCollectionName(selected)}の獲得カード`}
            firstCardId={selected.cards[0]?.cardId}
            schoolLogos={schoolLogos}
            scope={scope}
            search={gridSearch}
            onOpen={onOpen}
          />
        </>
      )}
      {studentId === null &&
        collection &&
        (entries.length ? (
          <GalleryGrid
            key="collection"
            entries={entries}
            label="生徒図鑑"
            schoolLogos={schoolLogos}
            scope={scope}
            search={gridSearch}
            restoreFocus={collectionReturn}
            onRestoreFocus={() => setCollectionReturn(undefined)}
            onOpen={onOpen}
            onOpenGroup={onOpenGroup}
          />
        ) : (
          <p className={styles.empty}>条件に一致する生徒はいません。</p>
        ))}
      {query.isError && (
        <div className={styles.empty} role="alert">
          <p>カードの一覧を読み込めませんでした。</p>
          <button className={styles.secondary} type="button" onClick={retryCards}>
            もう一度試す
          </button>
        </div>
      )}
      {viewerRequested && (
        <Suspense fallback={<output className={styles.empty}>拡大表示を準備中…</output>}>
          <GalleryViewer
            scope={scope}
            cardId={cardId}
            cards={viewerCards}
            schoolLogos={schoolLogos}
            fallbackFocus={groupAvailable ? groupHeading : heading}
            trigger={trigger}
            onSelect={(next) =>
              void navigate(
                { pathname: `/gallery/${next}`, search: gridSearch },
                { replace: true, state: location.state },
              )
            }
            onClose={() => {
              if (openedFromGallery) void navigate(-1);
              else void navigate({ pathname: "/gallery", search: gridSearch }, { replace: true });
            }}
          />
        </Suspense>
      )}
    </section>
  );
}

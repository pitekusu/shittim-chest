import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type PropsWithChildren,
  type RefObject,
} from "react";
import { useLocation } from "react-router-dom";

import { useOptionalRecordsArchive } from "./hooks/useRecordsArchive";
import { routeScenePath } from "./lib/routePresentation";
import styles from "./styles/routeMotion.module.css";

export type RouteMotionKind = "archive" | "detail" | "insights" | "admin" | "memorial" | "other";

export function routeMotionKind(pathname: string): RouteMotionKind {
  if (pathname === "/") return "archive";
  if (pathname === "/insights") return "insights";
  if (pathname === "/admin") return "admin";
  if (pathname === "/memorial") return "memorial";
  if (pathname.startsWith("/records/")) return "detail";
  return "other";
}

function RouteScene({
  pathname,
  animate,
  sceneRef,
  children,
}: PropsWithChildren<{
  readonly pathname: string;
  readonly animate: boolean;
  readonly sceneRef: RefObject<HTMLDivElement | null>;
}>) {
  const [motion, setMotion] = useState<"idle" | "waiting" | "active" | "settled">(
    animate ? "waiting" : "idle",
  );
  const motionStartedRef = useRef(!animate);
  const sceneFinishedRef = useRef(!animate);
  const contentReadyRef = useRef(!animate);
  const contentFinishedRef = useRef(!animate);

  useLayoutEffect(() => {
    if (!animate) return;
    const scene = sceneRef.current;
    if (!scene) return;
    const preference = window.matchMedia("(prefers-reduced-motion: reduce)");
    let observer: MutationObserver | null = null;
    const detach = () => {
      observer?.disconnect();
      scene.removeEventListener("animationend", handleRouteAnimation);
      scene.removeEventListener("animationcancel", handleRouteAnimation);
      preference.removeEventListener("change", handlePreference);
    };
    const settle = () => {
      setMotion("settled");
      detach();
    };
    const settleWhenComplete = () => {
      if (scene.querySelector("[data-route-motion-ready]")) contentReadyRef.current = true;
      if (sceneFinishedRef.current && contentReadyRef.current && contentFinishedRef.current)
        settle();
    };
    const observeReadiness = () => {
      const ready = scene.querySelector("[data-route-motion-ready]");
      contentReadyRef.current = ready !== null;
      if (ready && !motionStartedRef.current) {
        motionStartedRef.current = true;
        setMotion("active");
      }
      const terminal = scene.querySelector<HTMLElement>("[data-route-motion-terminal]");
      if (ready && (!terminal || !terminal.classList.contains(styles.routeMotionItem)))
        contentFinishedRef.current = true;
      settleWhenComplete();
    };
    function handleRouteAnimation(event: Event) {
      const target = event.target as HTMLElement;
      if (target === scene) sceneFinishedRef.current = true;
      if (target.hasAttribute("data-route-motion-terminal")) contentFinishedRef.current = true;
      settleWhenComplete();
    }
    function handlePreference() {
      if (preference.matches) settle();
    }
    if (preference.matches) {
      settle();
      return;
    }
    observer = new MutationObserver(observeReadiness);
    observer.observe(scene, { attributes: true, childList: true, subtree: true });
    scene.addEventListener("animationend", handleRouteAnimation);
    scene.addEventListener("animationcancel", handleRouteAnimation);
    preference.addEventListener("change", handlePreference);
    observeReadiness();
    return detach;
  }, [animate, sceneRef]);

  return (
    <div
      className={styles.routeScene}
      data-route-motion={motion}
      data-route-scene={pathname}
      ref={sceneRef}
    >
      <div className={styles.routeContent}>{children}</div>
    </div>
  );
}

export function BrandedRouteStage({ children }: PropsWithChildren) {
  const location = useLocation();
  const scenePath = routeScenePath(location.pathname);
  const archive = useOptionalRecordsArchive();
  const archiveReturnTarget = archive?.returnTarget;
  const clearArchiveReturnTarget = archive?.clearReturnTarget;
  const sceneRef = useRef<HTMLDivElement>(null);
  const previousPathnameRef = useRef(scenePath);
  const hasNavigatedRef = useRef(false);
  const mountedRef = useRef(false);
  const focusedPathnameRef = useRef(scenePath);

  if (previousPathnameRef.current !== scenePath) {
    previousPathnameRef.current = scenePath;
    hasNavigatedRef.current = true;
  }

  useEffect(() => {
    if (!mountedRef.current) {
      mountedRef.current = true;
      return;
    }
    const previousPathname = focusedPathnameRef.current;
    focusedPathnameRef.current = scenePath;
    if (previousPathname === scenePath) return;

    const scene = sceneRef.current;
    if (!scene) return;

    const returnTarget =
      scenePath === "/" &&
      archiveReturnTarget != null &&
      previousPathname === `/records/${archiveReturnTarget.recordId}`
        ? archiveReturnTarget
        : null;
    const focusContent = () => {
      if (returnTarget) {
        // Cached cards can move or disappear during a refetch. Restore only after
        // the archive's current retrieval has settled, even when a card is present.
        if (!scene.querySelector("[data-archive-ready]")) return false;
        const card = scene.querySelector<HTMLAnchorElement>(
          `[data-record-id="${returnTarget.recordId}"]`,
        );
        if (card) {
          window.scrollTo({ top: returnTarget.scrollY, behavior: "instant" });
          card.focus({ preventScroll: true });
          clearArchiveReturnTarget?.();
          return true;
        }
        clearArchiveReturnTarget?.();
      }
      const heading = scene.querySelector<HTMLElement>('h1[tabindex="-1"]');
      if (!heading) return false;
      heading.focus();
      return true;
    };

    if (focusContent()) return;

    const observer = new MutationObserver(() => {
      if (focusContent()) observer.disconnect();
    });
    observer.observe(scene, { attributes: true, childList: true, subtree: true });
    return () => observer.disconnect();
  }, [scenePath, archiveReturnTarget, clearArchiveReturnTarget]);

  return (
    <div
      className={styles.routeStage}
      data-route-kind={routeMotionKind(scenePath)}
      data-route-stage=""
    >
      <RouteScene
        pathname={scenePath}
        animate={hasNavigatedRef.current}
        key={scenePath}
        sceneRef={sceneRef}
      >
        {children}
      </RouteScene>
    </div>
  );
}

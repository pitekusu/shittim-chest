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
    let detached = false;
    const detach = () => {
      if (detached) return;
      detached = true;
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
  const archive = useOptionalRecordsArchive();
  const archiveReturnTarget = archive?.returnTarget;
  const clearArchiveReturnTarget = archive?.clearReturnTarget;
  const sceneRef = useRef<HTMLDivElement>(null);
  const previousPathnameRef = useRef(location.pathname);
  const hasNavigatedRef = useRef(false);
  const mountedRef = useRef(false);
  const focusedPathnameRef = useRef(location.pathname);

  if (previousPathnameRef.current !== location.pathname) {
    previousPathnameRef.current = location.pathname;
    hasNavigatedRef.current = true;
  }

  useEffect(() => {
    if (!mountedRef.current) {
      mountedRef.current = true;
      return;
    }
    const previousPathname = focusedPathnameRef.current;
    focusedPathnameRef.current = location.pathname;
    if (previousPathname === location.pathname) return;

    const scene = sceneRef.current;
    if (!scene) return;

    const returnTarget =
      location.pathname === "/" &&
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
  }, [location.pathname, archiveReturnTarget, clearArchiveReturnTarget]);

  return (
    <div
      className={styles.routeStage}
      data-route-kind={routeMotionKind(location.pathname)}
      data-route-stage=""
    >
      <RouteScene
        pathname={location.pathname}
        animate={hasNavigatedRef.current}
        key={location.pathname}
        sceneRef={sceneRef}
      >
        {children}
      </RouteScene>
    </div>
  );
}

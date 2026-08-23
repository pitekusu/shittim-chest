import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type PropsWithChildren,
  type RefObject,
} from "react";
import { useLocation } from "react-router-dom";

import styles from "./App.module.css";

export type RouteMotionKind = "archive" | "detail" | "insights" | "other";

export function routeMotionKind(pathname: string): RouteMotionKind {
  if (pathname === "/") return "archive";
  if (pathname === "/insights") return "insights";
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
  const [motion, setMotion] = useState<"idle" | "active" | "settled">(animate ? "active" : "idle");
  const sceneFinishedRef = useRef(!animate);
  const contentReadyRef = useRef(!animate);
  const contentFinishedRef = useRef(!animate);

  const settleWhenComplete = useCallback(() => {
    if (sceneRef.current?.querySelector("[data-route-motion-ready]")) {
      contentReadyRef.current = true;
    }
    if (sceneFinishedRef.current && contentReadyRef.current && contentFinishedRef.current) {
      setMotion("settled");
    }
  }, [sceneRef]);

  useEffect(() => {
    if (!animate) return;
    const scene = sceneRef.current;
    if (!scene) return;

    if (
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
    ) {
      sceneFinishedRef.current = true;
      contentReadyRef.current = true;
      contentFinishedRef.current = true;
      setMotion("settled");
      return;
    }

    const observeReadiness = () => {
      const ready = scene.querySelector("[data-route-motion-ready]");
      contentReadyRef.current = ready !== null;
      const terminal = scene.querySelector<HTMLElement>("[data-route-motion-terminal]");
      if (ready && (!terminal || !terminal.classList.contains(styles.routeMotionItem))) {
        contentFinishedRef.current = true;
      }
      settleWhenComplete();
    };
    observeReadiness();
    const observer = new MutationObserver(observeReadiness);
    observer.observe(scene, { attributes: true, childList: true, subtree: true });
    const handleRouteAnimation = (event: Event) => {
      const target = event.target as HTMLElement;
      if (target === scene) sceneFinishedRef.current = true;
      if (target.hasAttribute("data-route-motion-terminal")) {
        contentFinishedRef.current = true;
      }
      settleWhenComplete();
    };
    scene.addEventListener("animationend", handleRouteAnimation);
    return () => {
      observer.disconnect();
      scene.removeEventListener("animationend", handleRouteAnimation);
    };
  }, [animate, sceneRef, settleWhenComplete]);

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
  const sceneRef = useRef<HTMLDivElement>(null);
  const previousPathnameRef = useRef(location.pathname);
  const hasNavigatedRef = useRef(false);
  const mountedRef = useRef(false);

  if (previousPathnameRef.current !== location.pathname) {
    previousPathnameRef.current = location.pathname;
    hasNavigatedRef.current = true;
  }

  useEffect(() => {
    if (!mountedRef.current) {
      mountedRef.current = true;
      return;
    }

    const scene = sceneRef.current;
    if (!scene) return;

    const focusHeading = () => {
      const heading = scene.querySelector<HTMLElement>('h1[tabindex="-1"]');
      if (!heading) return false;
      heading.focus();
      return true;
    };

    if (focusHeading()) return;

    const observer = new MutationObserver(() => {
      if (focusHeading()) observer.disconnect();
    });
    observer.observe(scene, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [location.pathname]);

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

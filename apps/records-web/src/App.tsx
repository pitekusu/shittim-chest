import {
  QueryClient,
  QueryClientProvider,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { lazy, Suspense, useCallback, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";

import { RecordsApiError } from "./api/http";
import { getSession, logout } from "./api/session";
import type { SessionResponse } from "./api/types";
import { BrandMark } from "./components/Brand";
import { BrandTransition } from "./components/BrandTransition";
import { ErrorPanel } from "./components/ErrorPanel";
import { Layout } from "./components/Layout";
import { RouteChunkBoundary, RouteLoadingFallback } from "./components/RouteChunkBoundary";
import { SESSION_QUERY_KEY } from "./hooks/useAuthenticationRecovery";
import { LOGIN_TRANSITION_KEY } from "./lib/authTransition";
import { BrandedRouteStage } from "./RouteMotion";
import { LoginPage } from "./routes/LoginPage";
import { NotFoundPage } from "./routes/NotFoundPage";
import authStyles from "./styles/auth.module.css";
import { useRecordsTheme, type Theme } from "./theme";

const RecordsHome = lazy(() => import("./routes/RecordsHome"));
const RecordDetail = lazy(() => import("./routes/RecordDetail"));
const RankingsPage = lazy(() => import("./routes/RankingsPage"));
const AdminPage = lazy(() => import("./routes/AdminPage"));

function LoadingScreen(): React.JSX.Element {
  return (
    <main className={authStyles.loadingScreen} aria-busy="true">
      <BrandMark />
      <p>記録庫を開いています</p>
    </main>
  );
}

function AuthenticatedRoutes({
  session,
  onLogout,
  theme,
  onThemeToggle,
}: {
  readonly session: SessionResponse & { authenticated: true };
  readonly onLogout: () => void;
  readonly theme: Theme;
  readonly onThemeToggle: () => void;
}): React.JSX.Element {
  const location = useLocation();
  const [showTransition, setShowTransition] = useState(
    () => sessionStorage.getItem(LOGIN_TRANSITION_KEY) === "pending",
  );
  const finishLoginTransition = useCallback(() => {
    sessionStorage.removeItem(LOGIN_TRANSITION_KEY);
    setShowTransition(false);
  }, []);

  if (showTransition) {
    return (
      <BrandTransition
        accessibleName="ログインしました"
        message="WELCOME, SENSEI."
        onComplete={finishLoginTransition}
      />
    );
  }

  return (
    <Layout
      displayName={session.user.displayName}
      avatar={session.user.avatar}
      onLogout={onLogout}
      theme={theme}
      onThemeToggle={onThemeToggle}
    >
      <BrandedRouteStage>
        <RouteChunkBoundary key={location.pathname}>
          <Suspense fallback={<RouteLoadingFallback />}>
            <Routes>
              <Route path="/" element={<RecordsHome />} />
              <Route path="/records/:recordId" element={<RecordDetail />} />
              <Route path="/insights" element={<RankingsPage />} />
              <Route
                path="/admin"
                element={
                  <AdminPage isAdmin={session.isAdmin === true} csrfToken={session.csrfToken} />
                }
              />
              <Route
                path="/admin/prompts"
                element={
                  <AdminPage
                    isAdmin={session.isAdmin === true}
                    csrfToken={session.csrfToken}
                    view="prompts"
                  />
                }
              />
              <Route path="/login" element={<Navigate to="/" replace />} />
              <Route path="*" element={<NotFoundPage />} />
            </Routes>
          </Suspense>
        </RouteChunkBoundary>
      </BrandedRouteStage>
    </Layout>
  );
}

function ApplicationRoutes({
  theme,
  onThemeToggle,
}: {
  readonly theme: Theme;
  readonly onThemeToggle: () => void;
}): React.JSX.Element {
  const session = useQuery({ queryKey: SESSION_QUERY_KEY, queryFn: getSession });
  const location = useLocation();
  const navigate = useNavigate();
  const client = useQueryClient();
  const [showLogoutTransition, setShowLogoutTransition] = useState(false);
  const logoutMutation = useMutation({
    mutationFn: (csrfToken: string) => logout(csrfToken),
    onSuccess: () => {
      sessionStorage.removeItem(LOGIN_TRANSITION_KEY);
      setShowLogoutTransition(true);
    },
  });
  const finishLogoutTransition = useCallback(() => {
    client.clear();
    client.setQueryData<SessionResponse>(SESSION_QUERY_KEY, {
      schemaVersion: 1,
      authenticated: false,
      isAdmin: false,
      user: null,
      csrfToken: null,
    });
    setShowLogoutTransition(false);
    void navigate("/login", { replace: true });
  }, [client, navigate]);

  if (showLogoutTransition) {
    return (
      <BrandTransition
        accessibleName="ログオフしました"
        message="GOODBYE, SENSEI."
        onComplete={finishLogoutTransition}
      />
    );
  }
  if (session.isPending) return <LoadingScreen />;
  if (session.isError) {
    const error = session.error instanceof RecordsApiError ? session.error : undefined;
    return (
      <main className={authStyles.errorShell}>
        <ErrorPanel
          title="記録庫へ接続できません"
          message={error?.message ?? "しばらくしてから、もう一度お試しください。"}
          requestId={error?.requestId}
          onRetry={() => void session.refetch()}
        />
      </main>
    );
  }
  if (!session.data.authenticated) {
    if (location.pathname !== "/login") {
      return <Navigate to="/login" state={{ from: location.pathname }} replace />;
    }
    return (
      <Routes>
        <Route path="/login" element={<LoginPage session={session.data} />} />
      </Routes>
    );
  }

  const authenticatedSession = session.data;
  return (
    <AuthenticatedRoutes
      session={authenticatedSession}
      onLogout={() => logoutMutation.mutate(authenticatedSession.csrfToken)}
      theme={theme}
      onThemeToggle={onThemeToggle}
    />
  );
}

export function App(): React.JSX.Element {
  const { theme, toggleTheme } = useRecordsTheme();
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: { queries: { retry: false, staleTime: 30_000 } },
      }),
  );

  return (
    <QueryClientProvider client={client}>
      <BrowserRouter>
        <ApplicationRoutes theme={theme} onThemeToggle={toggleTheme} />
      </BrowserRouter>
    </QueryClientProvider>
  );
}

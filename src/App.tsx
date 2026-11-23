import { ErrorBoundary } from "./components/ErrorBoundary";
import { Layout } from "./components/Layout";
import { useAsync } from "./hooks/useAsync";
import { api } from "./lib/api";
import { useRoute } from "./lib/router";
import { DashboardPage } from "./pages/DashboardPage";
import { ProjectDetailPage } from "./pages/ProjectDetailPage";
import { ProjectsPage } from "./pages/ProjectsPage";

export default function App() {
  const route = useRoute();
  const info = useAsync(() => api.info(), []);
  const health = useAsync(() => api.health(), []);

  return (
    <ErrorBoundary>
      <Layout info={info.data} databaseStatus={health.data?.database ?? null}>
        {route.name === "dashboard" && <DashboardPage />}
        {route.name === "projects" && <ProjectsPage />}
        {route.name === "project" && (
          <ProjectDetailPage projectId={route.id} maxUploadMb={info.data?.max_upload_mb ?? 2048} />
        )}
      </Layout>
    </ErrorBoundary>
  );
}

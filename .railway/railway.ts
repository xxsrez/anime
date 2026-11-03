import { defineRailway, github, project, service, volume } from "railway/iac";

// Keep the independently deployed daily-sync service and all variables outside
// this partial's ownership.
export const partial = "web";

export default defineRailway(() => {
  const data = volume("web-volume", { region: "sfo", sizeMB: 500 });
  const web = service("web", {
    source: github("xxsrez/anime", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "RAILPACK" },
    start: "sh scripts/railway_start.sh",
    healthcheck: "/api/health",
    healthcheckTimeout: 60,
    // Railway defaults to ON_FAILURE with 10 retries; see README for why the
    // default is omitted from IaC until the planner handles it correctly.
    replicas: { sfo: 1 },
    volumeMounts: { "/data": data },
  });

  // Railway CLI supports this policy, although SDK 3.16.0 omits it from the
  // ProjectDefinition type. Verify that plans say "variables: unmanaged".
  const projectConfig = {
    variables: { managed: false },
    resources: [web, data],
  };
  return project("anime-local", projectConfig);
});

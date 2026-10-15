// Secret names only. Values are configured through the host's secret store.
interface Env {
  GITHUB_CLIENT_ID: string;
  GITHUB_CLIENT_SECRET: string;
  SESSION_SECRET: string;
  ENTRA_TENANT_ID?: string;
  ENTRA_AUTHORITY?: string;
  ENTRA_CUSTOM_DOMAIN?: string;
  ENTRA_CLIENT_ID?: string;
  ENTRA_CLIENT_SECRET?: string;
  ENTRA_REQUIRED_ROLE?: string;
  DIRECTORY_HOOK_CONFIG?: string;
  DIRECTORY_GRAPH_CLIENT_ID?: string;
  DIRECTORY_GRAPH_CLIENT_SECRET?: string;
}

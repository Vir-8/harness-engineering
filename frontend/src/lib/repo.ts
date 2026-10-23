// The source, linked from the footer and from each part on the Architecture page.
export const REPO_URL = "https://github.com/Vir-8/harness-engineering";

export function sourceUrl(path: string): string {
  return `${REPO_URL}/${path.endsWith("/") ? "tree" : "blob"}/main/${path.replace(/\/$/, "")}`;
}

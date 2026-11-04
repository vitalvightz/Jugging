// Visible, authenticated page observations only. Paths never leave this module.
const featureForPage = (path: string) => {
  const section = path.split("/")[1] ?? "";
  const features: Record<string, string> = {
    plan: "plans", plans: "plans", "new-plan": "plans", create: "plans", generate: "plans",
    today: "recovery", timer: "timer", history: "history", nutrition: "nutrition",
    strength: "strength", sparring: "sparring",
  };
  return features[section] ?? "onboarding";
};

type VisibilitySource = Pick<Document, "visibilityState" | "addEventListener" | "removeEventListener">;
export function observeUsage(
  source: VisibilitySource,
  getPath: () => string,
  send: (event: string, feature: string) => void,
) {
  let viewedPath: string | null = null;
  const pageChanged = () => {
    const path = getPath();
    if (source.visibilityState !== "visible" || viewedPath === path) return;
    viewedPath = path;
    send("page_viewed", featureForPage(path));
  };
  const visible = () => {
    if (source.visibilityState !== "visible") return;
    send("app_opened", "onboarding");
    pageChanged();
  };
  visible();
  source.addEventListener("visibilitychange", visible);
  return { pageChanged, dispose: () => source.removeEventListener("visibilitychange", visible) };
}

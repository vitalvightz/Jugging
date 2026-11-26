import { useState } from "react";

/** Ask for a fresh signed rendition on every save, including after a viewer has sat open. */
export function SaveMediaButton({ kind, load, className }: {
  kind: "photo" | "video";
  load: () => Promise<string>;
  className: string;
}) {
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  return <>
    <button type="button" className={className} disabled={busy} onClick={async event => {
      const button = event.currentTarget;
      setBusy(true);
      setFailed(false);
      try {
        const url = new URL(await load());
        const bucket = kind === "photo" ? "attachment-ready" : "private-demo-ready";
        if (url.origin !== new URL(process.env.NEXT_PUBLIC_SUPABASE_URL!).origin
          || !url.pathname.startsWith(`/storage/v1/object/sign/${bucket}/`) || !url.searchParams.get("token")) {
          throw new Error("Invalid download link");
        }
        if (!button.isConnected) return;
        const name = kind === "photo" ? "UNLXCK-photo.jpg" : "UNLXCK-video.mp4";
        url.searchParams.set("download", name);
        const link = document.createElement("a");
        link.href = url.href;
        link.download = name;
        link.rel = "noreferrer";
        document.body.append(link);
        link.click();
        link.remove();
      } catch {
        setFailed(true);
      } finally {
        setBusy(false);
      }
    }}>{busy ? "Preparing…" : `Save ${kind}`}</button>
    {failed && <span role="alert">Couldn’t save. Try again.</span>}
  </>;
}

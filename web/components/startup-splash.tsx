"use client";

import { useEffect, useState } from "react";
import "./startup-splash.css";

export function StartupSplash() {
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const main = document.querySelector(".app-main");
    const finish = () => {
      observer.disconnect();
      window.clearTimeout(timeout);
      setReady(true);
    };
    const check = () => {
      const page = main?.querySelector(".page");
      if (page?.querySelector(":scope > :not(template, script, style)") &&
          !page.querySelector("[data-startup-pending]")) {
        finish();
      }
    };
    const observer = new MutationObserver(check);
    const timeout = window.setTimeout(finish, 900);
    if (main) observer.observe(main, { childList: true, subtree: true, attributes: true, attributeFilter: ["data-startup-pending"] });
    check();
    return () => {
      observer.disconnect();
      window.clearTimeout(timeout);
    };
  }, []);

  return (
    <div className="startup-splash" data-ready={ready} role="status" aria-hidden={ready}>
      <span className="sr-only">Loading UNLXCK</span>
      <span className="startup-splash-mark" aria-hidden="true" />
    </div>
  );
}

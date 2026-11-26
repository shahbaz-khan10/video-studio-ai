import { useEffect, useState } from "react";

export type Route =
  | { name: "dashboard" }
  | { name: "projects" }
  | { name: "project"; id: string }
  | { name: "studio"; id: string };

export function parseRoute(hash: string): Route {
  const clean = hash.replace(/^#\/?/, "").replace(/\/$/, "");
  if (clean.startsWith("projects/")) {
    const rest = decodeURIComponent(clean.slice("projects/".length));
    if (rest.endsWith("/studio")) {
      return { name: "studio", id: rest.slice(0, -"/studio".length) };
    }
    return { name: "project", id: rest };
  }
  if (clean === "projects") {
    return { name: "projects" };
  }
  return { name: "dashboard" };
}

export function navigate(path: string): void {
  window.location.hash = path.startsWith("/") ? path : `/${path}`;
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash));
  useEffect(() => {
    const update = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  return route;
}

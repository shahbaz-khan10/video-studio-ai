import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { installAuthFetch } from "./lib/auth";
import "./styles.css";

installAuthFetch(); // before any API request

const container = document.getElementById("root");
if (!container) {
  throw new Error("Root element not found");
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

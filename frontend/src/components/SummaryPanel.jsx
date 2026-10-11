import { useEffect, useRef, useState } from "react";
import { summarize } from "../api.js";
import ErrorAlert from "./ErrorAlert.jsx";
import Loading from "./Loading.jsx";
import Markdown from "./Markdown.jsx";

// Runs the summary when mounted. `target` is { paper_id } or { paper }.
export default function SummaryPanel({ target }) {
  const [state, setState] = useState({ loading: true });

  async function run() {
    setState({ loading: true });
    try {
      const { summary } = await summarize(target);
      setState({ summary });
    } catch (e) {
      setState({ error: e.message });
    }
  }

  const started = useRef(false); // StrictMode runs effects twice in development; call the API once
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    run();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="panel">
      <h3>Summary</h3>
      {state.loading && <Loading>Reading the paper and summarizing…</Loading>}
      {state.summary && (
        <div className="summary">
          <Markdown>{state.summary}</Markdown>
        </div>
      )}
      {state.error && <ErrorAlert>{state.error}</ErrorAlert>}
      {!state.loading && (
        <button className="btn sm" style={{ marginTop: 8 }} onClick={run}>
          Regenerate
        </button>
      )}
    </div>
  );
}

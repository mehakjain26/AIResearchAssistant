import { useState } from "react";
import { savePaper, searchPapers } from "../api.js";
import PaperCard from "../components/PaperCard.jsx";
import { usePanels } from "../components/PaperActions.jsx";
import ErrorAlert from "../components/ErrorAlert.jsx";
import Loading from "../components/Loading.jsx";

function ResultCard({ result }) {
  const [saved, setSaved] = useState(null); // saved Paper row once saved
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  // Use the saved id once saved; otherwise the PDF is fetched on demand from the search result.
  const target = saved ? { paper_id: saved.id } : { paper: result };
  const { buttons, panel } = usePanels(target);

  async function save() {
    setSaving(true);
    setError(null);
    try {
      setSaved(await savePaper(result));
    } catch (e) {
      setError(`Could not save: ${e.message}`);
    } finally {
      setSaving(false);
    }
  }

  return (
    <PaperCard paper={saved ? { ...result, id: saved.id } : result} error={error} panel={panel}>
      {saved ? (
        <span className="tag">Saved</span>
      ) : (
        <button className="btn primary sm" onClick={save} disabled={saving}>
          {saving ? "Fetching PDF and saving…" : "Save"}
        </button>
      )}
      {buttons}
    </PaperCard>
  );
}

export default function SearchPage() {
  const [query, setQuery] = useState("");
  const [state, setState] = useState({ status: "idle" });

  async function submit(e) {
    e.preventDefault();
    if (!query.trim()) return;
    setState({ status: "loading" });
    try {
      setState({ status: "done", results: await searchPapers(query.trim()) });
    } catch (err) {
      setState({ status: "error", message: err.message });
    }
  }

  return (
    <section>
      <form className="searchbox" onSubmit={submit}>
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search papers by keyword or topic"
          aria-label="Search papers"
        />
        <button className="btn primary">Search</button>
      </form>
      {state.status === "loading" && <Loading>Searching…</Loading>}
      {state.status === "error" && <ErrorAlert>{state.message}</ErrorAlert>}
      {state.status === "done" && state.results.length === 0 && <p>No papers found.</p>}
      {state.status === "done" &&
        state.results.map((r) => <ResultCard key={r.external_id || r.title} result={r} />)}
    </section>
  );
}

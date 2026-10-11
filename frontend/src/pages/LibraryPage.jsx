import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { listPapers } from "../api.js";
import PaperCard from "../components/PaperCard.jsx";
import { usePanels } from "../components/PaperActions.jsx";
import UploadButton from "../components/UploadButton.jsx";
import DeleteButton from "../components/DeleteButton.jsx";
import ErrorAlert from "../components/ErrorAlert.jsx";
import Loading from "../components/Loading.jsx";

function LibraryCard({ paper, onDeleted }) {
  const [error, setError] = useState(null);
  const { buttons, panel } = usePanels({ paper_id: paper.id });
  return (
    <PaperCard paper={paper} panel={panel} error={error}>
      <Link className="btn primary sm" to={`/papers/${paper.id}`}>Open</Link>
      {buttons}
      <DeleteButton paper={paper} onDeleted={onDeleted} onError={setError} />
    </PaperCard>
  );
}

export default function LibraryPage() {
  const [papers, setPapers] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    listPapers().then(setPapers).catch((e) => setError(e.message));
  }, []);

  return (
    <section>
      <h1>Library</h1>
      <UploadButton onUploaded={(p) => setPapers((list) => [p, ...(list || [])])} />
      {error && <ErrorAlert>{error}</ErrorAlert>}
      {papers === null && !error && <Loading>Loading…</Loading>}
      {papers && papers.length === 0 && <p>No papers yet. Save one from Search or upload a PDF.</p>}
      {papers &&
        papers.map((p) => (
          <LibraryCard key={p.id} paper={p} onDeleted={(id) => setPapers((list) => list.filter((x) => x.id !== id))} />
        ))}
    </section>
  );
}

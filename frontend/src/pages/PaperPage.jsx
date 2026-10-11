import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { getPaper } from "../api.js";
import PaperCard from "../components/PaperCard.jsx";
import SummaryPanel from "../components/SummaryPanel.jsx";
import ChatPanel from "../components/ChatPanel.jsx";
import ErrorAlert from "../components/ErrorAlert.jsx";
import Loading from "../components/Loading.jsx";
import DeleteButton from "../components/DeleteButton.jsx";

export default function PaperPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [deleteError, setDeleteError] = useState(null);
  const [paper, setPaper] = useState(null);
  const [error, setError] = useState(null);
  const [showSummary, setShowSummary] = useState(false);

  useEffect(() => {
    getPaper(id).then(setPaper).catch((e) => setError(e.message));
  }, [id]);

  if (error) return <ErrorAlert>{error}</ErrorAlert>;
  if (!paper) return <Loading>Loading…</Loading>;

  const target = { paper_id: paper.id };
  return (
    <section>
      <Link className="back" to="/library">← Back to library</Link>
      <PaperCard paper={paper} fullAbstract error={deleteError}>
        <DeleteButton paper={paper} onDeleted={() => navigate("/library")} onError={setDeleteError} />
      </PaperCard>
      <div className="two">
        <div className="card">
          {showSummary ? (
            <SummaryPanel target={target} />
          ) : (
            <button className="btn primary" onClick={() => setShowSummary(true)}>Summarize</button>
          )}
        </div>
        <div className="card">
          <ChatPanel target={target} />
        </div>
      </div>
    </section>
  );
}

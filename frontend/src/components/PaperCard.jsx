import { useState } from "react";
import ErrorAlert from "./ErrorAlert.jsx";

const LONG_ABSTRACT = 350; // characters; longer abstracts are shortened until expanded

export default function PaperCard({ paper, children, error, panel, fullAbstract = false }) {
  const [expanded, setExpanded] = useState(fullAbstract);
  const authors = paper.authors?.length ? paper.authors.join(", ") : "Unknown authors";
  const abstract = paper.abstract || "Abstract not available.";
  const long = abstract.length > LONG_ABSTRACT;
  // A saved paper (it has an id) opens its stored PDF; a search result that isn't saved yet links to the web.
  const link = paper.id ? `/api/papers/${paper.id}/pdf` : paper.url;
  return (
    <article className="card">
      <h2>{paper.title}</h2>
      <div className="meta">
        {authors} · {paper.year ?? "Year unknown"}
      </div>
      <p className={long && !expanded ? "abstract clamped" : "abstract"}>{abstract}</p>
      {long && !fullAbstract && (
        <button className="link" onClick={() => setExpanded((e) => !e)}>
          {expanded ? "Show less" : "Show full abstract"}
        </button>
      )}
      <div className="actions">
        {children}
        {link && (
          <a href={link} target="_blank" rel="noreferrer">
            View paper ↗
          </a>
        )}
      </div>
      {error && <ErrorAlert>{error}</ErrorAlert>}
      {panel}
    </article>
  );
}

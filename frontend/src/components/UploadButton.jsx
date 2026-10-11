import { useRef, useState } from "react";
import { uploadPaper } from "../api.js";
import ErrorAlert from "./ErrorAlert.jsx";

export default function UploadButton({ onUploaded }) {
  const input = useRef(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function handle(file) {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      onUploaded(await uploadPaper(file));
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
      if (input.current) input.current.value = "";
    }
  }

  return (
    <div
      className="drop"
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => {
        e.preventDefault();
        handle(e.dataTransfer.files[0]);
      }}
    >
      <input
        ref={input}
        type="file"
        accept="application/pdf,.pdf"
        hidden
        onChange={(e) => handle(e.target.files[0])}
      />
      <button className="btn primary" disabled={busy} onClick={() => input.current.click()}>
        {busy ? "Uploading…" : "Upload PDF"}
      </button>
      <div>
        {busy
          ? "Extracting title, authors, year and abstract…"
          : "or drop a PDF here. Title, authors, year and abstract are extracted automatically."}
      </div>
      {error && <ErrorAlert>{error}</ErrorAlert>}
    </div>
  );
}

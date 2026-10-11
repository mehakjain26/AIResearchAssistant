import { useState } from "react";
import { deletePaper } from "../api.js";

// Asks for confirmation, deletes the saved paper (and its stored PDF), then calls onDeleted.
export default function DeleteButton({ paper, onDeleted, onError }) {
  const [busy, setBusy] = useState(false);

  async function remove() {
    if (!window.confirm(`Delete "${paper.title}" and its stored PDF? This cannot be undone.`)) return;
    setBusy(true);
    try {
      await deletePaper(paper.id);
      onDeleted(paper.id);
    } catch (e) {
      setBusy(false);
      onError?.(e.message);
    }
  }

  return (
    <button className="btn sm danger" onClick={remove} disabled={busy}>
      {busy ? "Deleting…" : "Delete"}
    </button>
  );
}

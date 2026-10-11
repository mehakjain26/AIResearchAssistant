import { useState } from "react";
import { ask } from "../api.js";
import ErrorAlert from "./ErrorAlert.jsx";
import Loading from "./Loading.jsx";
import Markdown from "./Markdown.jsx";

// Each question is answered independently from the paper's full text.
// The conversation shown here lives only in the page.
export default function ChatPanel({ target }) {
  const [messages, setMessages] = useState([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    const q = question.trim();
    if (!q || busy) return;
    setQuestion("");
    setMessages((m) => [...m, { role: "user", text: q }]);
    setBusy(true);
    try {
      const { answer } = await ask(target, q);
      setMessages((m) => [...m, { role: "bot", text: answer }]);
    } catch (err) {
      setMessages((m) => [...m, { role: "error", text: err.message }]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel">
      <h3>Ask about this paper</h3>
      <div className="chat">
        {messages.map((m, i) => (
          m.role === "error" ? (
            <ErrorAlert key={i}>{m.text}</ErrorAlert>
          ) : (
            <div key={i} className={`msg ${m.role}`}>
              {m.role === "bot" ? <Markdown>{m.text}</Markdown> : m.text}
            </div>
          )
        ))}
        {busy && <Loading>Reading the paper…</Loading>}
      </div>
      <form className="ask" onSubmit={submit}>
        <input
          type="text"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Ask a question about this paper"
          aria-label="Question"
        />
        <button className="btn primary" disabled={busy || !question.trim()}>
          Ask
        </button>
      </form>
    </div>
  );
}

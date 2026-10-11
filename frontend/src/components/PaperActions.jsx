import { useState } from "react";
import SummaryPanel from "./SummaryPanel.jsx";
import ChatPanel from "./ChatPanel.jsx";

// Summarize / Ask buttons and their inline panels, shared by the search and library cards.
// `target` is { paper_id } or { paper }. A panel stays mounted once opened (it is only hidden),
// so closing and reopening it doesn't call the API again.
export function usePanels(target) {
  const [open, setOpen] = useState(null);
  const [opened, setOpened] = useState({});

  function toggle(name) {
    setOpen((current) => (current === name ? null : name));
    setOpened((o) => ({ ...o, [name]: true }));
  }

  const buttons = (
    <>
      <button className="btn sm" onClick={() => toggle("summary")}>Summarize</button>
      <button className="btn sm" onClick={() => toggle("ask")}>Ask</button>
    </>
  );
  const panel = (
    <>
      {opened.summary && (
        <div hidden={open !== "summary"}>
          <SummaryPanel target={target} />
        </div>
      )}
      {opened.ask && (
        <div hidden={open !== "ask"}>
          <ChatPanel target={target} />
        </div>
      )}
    </>
  );
  return { buttons, panel };
}

import ReactMarkdown from "react-markdown";

// Renders the model's Markdown (headings, bullets, bold) as formatted text. Raw HTML is not rendered.
export default function Markdown({ children }) {
  return (
    <div className="md">
      <ReactMarkdown>{children}</ReactMarkdown>
    </div>
  );
}

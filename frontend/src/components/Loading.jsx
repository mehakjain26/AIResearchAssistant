export default function Loading({ children }) {
  return (
    <div className="loading">
      <span className="spin" /> {children}
    </div>
  );
}

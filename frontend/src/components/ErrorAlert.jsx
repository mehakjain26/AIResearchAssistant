export default function ErrorAlert({ children }) {
  return (
    <div className="alert" role="alert">
      {children}
    </div>
  );
}

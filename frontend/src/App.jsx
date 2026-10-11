import { NavLink, Route, Routes } from "react-router-dom";
import SearchPage from "./pages/SearchPage.jsx";
import LibraryPage from "./pages/LibraryPage.jsx";
import PaperPage from "./pages/PaperPage.jsx";

export default function App() {
  return (
    <>
      <header className="app">
        <div className="bar">
          <span className="brand">Research Assistant</span>
          <nav>
            <NavLink to="/" end>Search</NavLink>
            <NavLink to="/library">Library</NavLink>
          </nav>
        </div>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<SearchPage />} />
          <Route path="/library" element={<LibraryPage />} />
          <Route path="/papers/:id" element={<PaperPage />} />
        </Routes>
      </main>
    </>
  );
}

import { useState } from "react";
import Header from "./components/Header";
import Home from "./pages/Home";
import History from "./pages/History";
import Architecture from "./pages/Architecture";

export default function App() {
  const [view, setView] = useState<"console" | "history" | "architecture">("console");
  return <div className="min-h-screen bg-base"><Header view={view} onChangeView={setView} />{view === "console" ? <Home /> : view === "history" ? <History /> : <Architecture />}</div>;
}

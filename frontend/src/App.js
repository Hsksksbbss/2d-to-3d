import "@/App.css";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import FloorPlanApp from "@/pages/FloorPlanApp";
import { Toaster } from "@/components/ui/sonner";

function App() {
  return (
    <div className="App min-h-screen bg-[color:var(--bg)]">
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<FloorPlanApp />} />
        </Routes>
      </BrowserRouter>
      <Toaster position="top-right" richColors />
    </div>
  );
}

export default App;

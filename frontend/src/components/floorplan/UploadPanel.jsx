import { useRef } from "react";
import { Button } from "@/components/ui/button";
import { UPLOAD } from "@/constants/testIds";
import { Upload, Loader2 } from "lucide-react";

export default function UploadPanel({ loading, onUpload }) {
  const inputRef = useRef(null);
  return (
    <div className="card p-5">
      <div className="mono text-xs uppercase tracking-[0.25em] text-[color:var(--muted)] mb-1">
        upload
      </div>
      <h3 className="text-lg font-semibold mb-3">Floor plan image</h3>
      <input
        ref={inputRef}
        type="file"
        accept="image/*"
        data-testid={UPLOAD.fileInput}
        className="block w-full text-sm text-[color:var(--muted)]
                   file:mr-3 file:pill-btn file:border-0
                   file:bg-[color:var(--ink)] file:text-white
                   file:cursor-pointer"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) onUpload(f);
        }}
      />
      <div className="mt-4 flex items-center gap-2">
        <Button
          className="pill-btn bg-[color:var(--accent)] hover:bg-[color:var(--accent)]/90 text-white"
          data-testid={UPLOAD.submitBtn}
          disabled={loading}
          onClick={() => inputRef.current?.click()}
        >
          {loading ? (
            <><Loader2 className="w-4 h-4 mr-2 animate-spin" /> Analyzing…</>
          ) : (
            <><Upload className="w-4 h-4 mr-2" /> Choose file</>
          )}
        </Button>
      </div>
      <p className="mt-3 text-xs text-[color:var(--muted)]">
        Analysis runs OpenCV + Tesseract server-side. Larger images take longer.
      </p>
    </div>
  );
}

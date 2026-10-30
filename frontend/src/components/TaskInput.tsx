import { useEffect, useRef, useState } from "react";
import { ArrowUp, Loader2, Mic, MicOff, Sparkles, Square, Eraser } from "lucide-react";
import ProviderSelector from "./ProviderSelector";
import type { ProviderSelection } from "../types";

const EXAMPLES = [
  "Open GitHub",
  "Open YouTube and search Python tutorials",
  "Open Wikipedia and search artificial intelligence",
  "Search GitHub for browser automation",
  "Compose an email to Rahul saying hello, but don't send it",
];

type RecognitionEventLike = Event & {
  resultIndex: number;
  results: {
    length: number;
    [index: number]: { isFinal: boolean; 0: { transcript: string } };
  };
};

type RecognitionLike = {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  start: () => void;
  stop: () => void;
  abort: () => void;
  onstart: (() => void) | null;
  onend: (() => void) | null;
  onerror: ((event: Event) => void) | null;
  onresult: ((event: RecognitionEventLike) => void) | null;
};

type RecognitionConstructor = new () => RecognitionLike;

declare global {
  interface Window {
    SpeechRecognition?: RecognitionConstructor;
    webkitSpeechRecognition?: RecognitionConstructor;
  }
}

interface Props {
  onSubmit: (instruction: string, provider: ProviderSelection) => void;
  busy: boolean;
  onStop?: () => void;
  onClear?: () => void;
}

export default function TaskInput({ onSubmit, busy, onStop, onClear }: Props) {
  const [value, setValue] = useState("");
  const [provider, setProvider] = useState<ProviderSelection>("auto");
  const [listening, setListening] = useState(false);
  const [speechSupported, setSpeechSupported] = useState(true);
  const recognitionRef = useRef<RecognitionLike | null>(null);
  const listeningRef = useRef(false);
  const finalTranscriptRef = useRef("");

  useEffect(() => {
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!Recognition) {
      setSpeechSupported(false);
      return;
    }

    const recognition = new Recognition();
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = "en-US";

    recognition.onstart = () => setListening(true);
    recognition.onresult = (event) => {
      let interim = "";
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const transcript = event.results[i][0].transcript;
        if (event.results[i].isFinal) {
          finalTranscriptRef.current += `${transcript} `;
        } else {
          interim += transcript;
        }
      }
      setValue(`${finalTranscriptRef.current}${interim}`.trimStart());
    };
    recognition.onerror = () => {
      // Chrome can emit no-speech/network errors during a short pause. We keep
      // the transcript and let onend restart while the user is still listening.
    };
    recognition.onend = () => {
      if (listeningRef.current) {
        window.setTimeout(() => {
          if (!listeningRef.current) return;
          try {
            recognition.start();
          } catch {
            // Already starting; the next end event will retry if necessary.
          }
        }, 180);
      } else {
        setListening(false);
      }
    };

    recognitionRef.current = recognition;
    return () => {
      listeningRef.current = false;
      try {
        recognition.abort();
      } catch {
        // no-op
      }
    };
  }, []);

  const toggleVoice = () => {
    const recognition = recognitionRef.current;
    if (!recognition || busy) return;

    if (listening) {
      listeningRef.current = false;
      recognition.stop();
      setListening(false);
      return;
    }

    finalTranscriptRef.current = value.trim() ? `${value.trim()} ` : "";
    listeningRef.current = true;
    try {
      recognition.start();
    } catch {
      // Chrome may already have a recognition session; keep it alive.
    }
  };

  const submit = () => {
    const trimmed = value.trim();
    if (!trimmed || busy) return;
    if (listening) {
      listeningRef.current = false;
      recognitionRef.current?.stop();
      setListening(false);
    }
    onSubmit(trimmed, provider);
  };

  return (
    <div className="space-y-4">
      <div className={`task-composer ${listening ? "task-composer-listening" : ""}`}>
        <div className="flex items-start gap-3">
          <Sparkles className="mt-1 h-5 w-5 shrink-0 text-signal" />
          <textarea
            value={value}
            onChange={(e) => {
              setValue(e.target.value);
              finalTranscriptRef.current = e.target.value ? `${e.target.value} ` : "";
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
            placeholder="Tell the browser what to do…"
            rows={3}
            className="min-h-[86px] flex-1 resize-none bg-transparent text-[17px] leading-7 text-text placeholder:text-muted outline-none"
            aria-label="Browser task"
          />
        </div>

        <div className="mt-3 flex items-center justify-between gap-3 border-t border-border/70 pt-3">
          <div className="flex items-center gap-2 text-xs text-muted">
            <button
              type="button"
              onClick={toggleVoice}
              disabled={busy || !speechSupported}
              className={`voice-button ${listening ? "voice-button-active" : ""}`}
              title={!speechSupported ? "Speech recognition is not available in this browser" : listening ? "Stop listening" : "Speak a task"}
              aria-label={listening ? "Stop voice input" : "Start voice input"}
            >
              {listening ? <MicOff className="h-4 w-4" /> : <Mic className="h-4 w-4" />}
            </button>
            <span>
              {listening ? "Listening — pause whenever you need; transcript stays active" : speechSupported ? "Type or speak your task" : "Voice input unavailable"}
            </span>
          </div>

          <div className="flex items-center gap-2">
            <ProviderSelector value={provider} onChange={setProvider} disabled={busy} />
            {!busy && (
              <button
                type="button"
                onClick={() => { setValue(""); finalTranscriptRef.current = ""; onClear?.(); }}
                className="clear-button"
                aria-label="Clear"
                title="Clear the input and the last result"
              >
                <Eraser className="h-4 w-4" />
              </button>
            )}
            {busy && onStop ? (
              <button type="button" onClick={onStop} className="cancel-button" aria-label="Stop task">
                <Square className="mr-1 inline h-3.5 w-3.5" />Stop
              </button>
            ) : (
              <button
                type="button"
                onClick={submit}
                disabled={busy || !value.trim()}
                className="run-button"
                aria-label="Run browser task"
              >
                {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <ArrowUp className="h-4 w-4" />}
                <span>{busy ? "Running" : "Run task"}</span>
              </button>
            )}
          </div>
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        {EXAMPLES.map((example, index) => (
          <button
            key={example}
            type="button"
            onClick={() => !busy && onSubmit(example, provider)}
            disabled={busy}
            className="example-chip"
            style={{ animationDelay: `${index * 70}ms` }}
          >
            {example}
          </button>
        ))}
      </div>
    </div>
  );
}

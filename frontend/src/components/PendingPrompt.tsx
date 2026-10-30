import { useState } from "react";
import { HelpCircle, ShieldAlert } from "lucide-react";
import type { PendingRequest } from "../types";

interface Props {
  pending: PendingRequest;
  onRespond: (reply: { approve?: boolean; answer?: string }) => void;
}

/** Shown while a task is paused: a confirmation (approve / decline) or a clarification question (options or free text). */
export default function PendingPrompt({ pending, onRespond }: Props) {
  const [text, setText] = useState("");
  const [sent, setSent] = useState(false);
  const send = (reply: { approve?: boolean; answer?: string }) => {
    if (sent) return;           // never submit the same decision twice
    setSent(true);
    onRespond(reply);
  };

  if (pending.type === "confirmation") {
    return (
      <div className="pending-card pending-confirm" role="alertdialog" aria-live="assertive">
        <ShieldAlert className="h-5 w-5 shrink-0" />
        <div className="min-w-0 flex-1">
          <p className="font-medium">Confirmation needed</p>
          <p className="mt-1 text-sm">{pending.question}</p>
          {pending.target && <p className="mt-1 text-xs opacity-70">Control: {pending.target}</p>}
          <div className="mt-3 flex gap-2">
            <button type="button" className="run-button" disabled={sent} onClick={() => send({ approve: true })}>Yes, go ahead</button>
            <button type="button" className="cancel-button" disabled={sent} onClick={() => send({ approve: false })}>No, don't</button>
          </div>
        </div>
      </div>
    );
  }

  const options = pending.options ?? [];
  return (
    <div className="pending-card pending-ask" role="alertdialog" aria-live="assertive">
      <HelpCircle className="h-5 w-5 shrink-0" />
      <div className="min-w-0 flex-1">
        <p className="font-medium">NEXA needs your input</p>
        <p className="mt-1 text-sm">{pending.question}</p>
        {options.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-2">
            {options.map((o) => (
              <button key={o} type="button" className="example-chip" disabled={sent} onClick={() => send({ answer: o })}>{o}</button>
            ))}
          </div>
        )}
        <form className="mt-3 flex gap-2" onSubmit={(e) => { e.preventDefault(); if (text.trim()) send({ answer: text.trim() }); }}>
          <input value={text} onChange={(e) => setText(e.target.value)} disabled={sent} placeholder="Type your answer…" aria-label="Your answer" className="pending-input" />
          <button type="submit" className="run-button" disabled={sent || !text.trim()}>Reply</button>
        </form>
      </div>
    </div>
  );
}

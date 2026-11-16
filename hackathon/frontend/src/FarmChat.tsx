import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { api, ApiError } from './api';
import type { ChatAction } from './api';

interface Msg {
  id: number;
  who: 'farmer' | 'bot';
  text: string;
  source?: 'rules' | 'llm';
  action?: ChatAction | null;
  actionState?: 'pending' | 'done' | 'declined' | 'failed';
}

const START: Msg = {
  id: 0,
  who: 'bot',
  text: 'Hi! Ask me about the farm in plain words, like "How are we doing?", "Can I spray today?" or "What is the sprayer doing?". '
    + 'You can also tell me what\'s happening, such as "the sprayer broke down", and I\'ll offer to update the plan.',
};
const DEFAULT_SUGGESTIONS = ['How are we doing?', 'Can I spray today?', "What's late?", 'What have you learned?'];

let nextId = 1;

export default function FarmChat({ onChanged }: { onChanged: () => void }) {
  const [open, setOpen] = useState(() => new URLSearchParams(window.location.search).get('chat') === '1');
  const [msgs, setMsgs] = useState<Msg[]>([START]);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [suggestions, setSuggestions] = useState(DEFAULT_SUGGESTIONS);
  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: 'smooth' });
  }, [msgs, open]);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  const ask = async (text: string) => {
    const q = text.trim();
    if (!q || sending) return;
    setInput('');
    setMsgs((m) => [...m, { id: nextId++, who: 'farmer', text: q }]);
    setSending(true);
    try {
      const r = await api.chat(q);
      setMsgs((m) => [...m, { id: nextId++, who: 'bot', text: r.reply, source: r.source, action: r.action,
        actionState: r.action ? 'pending' : undefined }]);
      if (r.suggestions?.length) setSuggestions(r.suggestions);
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : 'the farm service is unreachable';
      setMsgs((m) => [...m, { id: nextId++, who: 'bot', text: `Sorry — I couldn't reach the farm data (${msg}).` }]);
    } finally {
      setSending(false);
    }
  };

  const setActionState = (id: number, actionState: Msg['actionState']) =>
    setMsgs((m) => m.map((x) => (x.id === id ? { ...x, actionState } : x)));

  const apply = async (msg: Msg) => {
    if (!msg.action) return;
    setActionState(msg.id, 'done');
    try {
      const r = await api.event({ name: msg.action.name, ...msg.action.payload });
      if (!r.ok) {
        setActionState(msg.id, 'failed');
        setMsgs((m) => [...m, { id: nextId++, who: 'bot', text: `The simulator refused that: ${r.reason ?? 'unknown reason'}.` }]);
        return;
      }
      setMsgs((m) => [...m, { id: nextId++, who: 'bot',
        text: 'Done. The agent will replan on the next step. Step the simulation, then ask me "How are we doing?".' }]);
      onChanged();
    } catch (e) {
      setActionState(msg.id, 'failed');
      setMsgs((m) => [...m, { id: nextId++, who: 'bot', text: `That didn't work: ${e instanceof Error ? e.message : String(e)}` }]);
    }
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    ask(input);
  };

  return (
    <>
      {!open && (
        <button type="button" className="chat-fab" onClick={() => setOpen(true)} aria-label="Open farm assistant chat">
          <span aria-hidden="true">💬</span> Ask the farm
        </button>
      )}
      {open && (
        <section className="chat" role="dialog" aria-label="Farm assistant">
          <header className="chat-head">
            <div>
              <strong>Farm assistant</strong>
              <span className="chat-sub">Answers from the live simulator</span>
            </div>
            <button type="button" className="chat-x" onClick={() => setOpen(false)} aria-label="Close chat">×</button>
          </header>
          <div className="chat-list" ref={listRef} aria-live="polite">
            {msgs.map((m) => (
              <div key={m.id} className={`chat-msg ${m.who}`}>
                <div className="chat-bubble">
                  {m.text}
                  {m.action && (
                    <div className="chat-action">
                      {m.actionState === 'pending' ? (
                        <>
                          <button type="button" className="primary" onClick={() => apply(m)}>{m.action.label}</button>
                          <button type="button" onClick={() => setActionState(m.id, 'declined')}>No thanks</button>
                        </>
                      ) : (
                        <span className="chat-note">
                          {m.actionState === 'done' ? '✓ Applied' : m.actionState === 'declined' ? 'Not applied' : '✗ Failed'}
                        </span>
                      )}
                    </div>
                  )}
                </div>
                {m.source === 'llm' && <span className="chat-note">phrased by AI from live data</span>}
              </div>
            ))}
            {sending && <div className="chat-msg bot"><div className="chat-bubble typing">Checking the farm…</div></div>}
          </div>
          <div className="chat-chips">
            {suggestions.map((s) => (
              <button key={s} type="button" className="chip" onClick={() => ask(s)} disabled={sending}>{s}</button>
            ))}
          </div>
          <form className="chat-form" onSubmit={submit}>
            <input ref={inputRef} value={input} onChange={(e) => setInput(e.target.value)} maxLength={500}
              placeholder="Ask about machines, fields, weather…" aria-label="Message" />
            <button type="submit" className="primary" disabled={sending || !input.trim()}>Send</button>
          </form>
        </section>
      )}
    </>
  );
}

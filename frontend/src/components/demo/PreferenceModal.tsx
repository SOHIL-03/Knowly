import { useState } from 'react';
import { MAX_PREFERENCE_CHARS, type Preference } from '../../lib/demo';
import { CloseIcon, Modal } from './Modal';

/** "Anything you want Maya to ask about?" Yes opens the text box; No keeps the podcast general. */
export function PreferenceModal({
  section, initial, submitLabel = 'Create podcast', onCancel, onCreate,
}: { section: string; initial: Preference | null; submitLabel?: string; onCancel: () => void; onCreate: (p: Preference) => void }) {
  const [choice, setChoice] = useState<'yes' | 'no' | null>(initial?.choice ?? null);
  const [text, setText] = useState(initial?.text ?? '');
  const chars = text.trim().length;
  const over = chars > MAX_PREFERENCE_CHARS;
  const disabled = !choice || (choice === 'yes' && (chars === 0 || over));

  return (
    <Modal label="Listening preference" width={640} padding="24px 28px" onClose={onCancel}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12 }}>
        <div>
          <p style={{ fontSize: 13, fontWeight: 700, color: 'var(--kn-muted)' }}>{section}</p>
          <h2 style={{ margin: '6px 0 0', fontSize: 24, fontWeight: 800 }}>Anything you want Maya to ask about?</h2>
        </div>
        <button className="btn btn-ghost icon-btn" aria-label="Close" onClick={onCancel}><CloseIcon /></button>
      </div>
      <p style={{ margin: '10px 0 0', color: 'var(--text-2)', fontSize: 15, lineHeight: 1.55 }}>
        Maya asks Theo the questions you would. Tell her about your work or interests, or skip this for a general tech conversation.
      </p>
      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', marginTop: 20 }} role="radiogroup" aria-label="Add a preference?">
        <button role="radio" aria-checked={choice === 'yes'} className={choice === 'yes' ? 'choice on' : 'choice'} onClick={() => setChoice('yes')}>
          <span style={{ display: 'block', fontWeight: 700, fontSize: 16 }}>Yes, add my preference</span>
          <span style={{ display: 'block', marginTop: 4, fontSize: 14, color: 'var(--text-2)' }}>Maya’s questions follow what you write.</span>
        </button>
        <button role="radio" aria-checked={choice === 'no'} className={choice === 'no' ? 'choice on' : 'choice'} onClick={() => setChoice('no')}>
          <span style={{ display: 'block', fontWeight: 700, fontSize: 16 }}>No, keep it general</span>
          <span style={{ display: 'block', marginTop: 4, fontSize: 14, color: 'var(--text-2)' }}>A general tech conversation.</span>
        </button>
      </div>
      {choice === 'yes' && (
        <div style={{ marginTop: 20 }}>
          <label htmlFor="pref" style={{ display: 'block', fontWeight: 600, fontSize: 14, marginBottom: 6 }}>What do you want to hear about?</label>
          <textarea
            id="pref"
            data-autofocus
            className="field"
            rows={4}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="e.g. I’m a backend engineer building a RAG app. Focus on what open-weight models mean for self-hosting cost and privacy."
            style={{ resize: 'vertical', lineHeight: 1.5 }}
          />
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, marginTop: 6, flexWrap: 'wrap' }}>
            <p style={{ fontSize: 13, color: 'var(--kn-muted)' }}>Used for every section you play. You can change it anytime.</p>
            <p style={{ fontSize: 13, fontWeight: 600, color: over ? 'var(--notyet)' : 'var(--kn-muted)' }}>{chars} / {MAX_PREFERENCE_CHARS} characters</p>
          </div>
        </div>
      )}
      <div style={{ marginTop: 24, display: 'flex', gap: 10, justifyContent: 'flex-end', flexWrap: 'wrap' }}>
        <button className="btn btn-ghost" onClick={onCancel}>Cancel</button>
        <button className="btn btn-primary" disabled={disabled} onClick={() => choice && onCreate({ choice, text: choice === 'yes' ? text.trim() : '' })}>
          {submitLabel}
        </button>
      </div>
    </Modal>
  );
}

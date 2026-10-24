import { useState } from "react";
import { inr } from "../../lib/format.js";

/** "Reset account": wipes THIS paper account's trades back to its starting money. The server backs up the whole database first,
 *  and the user must type the account name, so one stray click can't erase anything. */
export default function ResetAccount({ accountId, startMoney, onReset }) {
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const close = () => { setOpen(false); setTyped(""); setErr(""); };
  const go = (e) => {
    e.preventDefault();
    setBusy(true); setErr("");
    onReset(accountId, typed)
      .then((r) => { setMsg(`Reset done: back to ${inr(r.capital)}. A backup of the old history was saved first (${String(r.backup).split("/").pop()}).`); close(); })
      .catch((x) => setErr(x.message))
      .finally(() => setBusy(false));
  };
  if (!open) {
    return (
      <div className="reset-acct">
        <button type="button" className="linkish" onClick={() => { setOpen(true); setMsg(""); }}>Reset this account</button>
        {msg && <div className="note" role="status">{msg}</div>}
      </div>
    );
  }
  return (
    <form className="reset-acct panel" onSubmit={go}>
      <div className="note">
        This deletes every trade of <b>{accountId}</b> and starts it again from {inr(startMoney ?? 0)}. The other account is not touched.
        The whole database is backed up first, so it can be restored. Type <b>{accountId}</b> to confirm.
      </div>
      <input type="text" value={typed} onChange={(e) => setTyped(e.target.value)} aria-label="Type the account name to confirm" autoFocus placeholder={accountId} />
      <div className="row">
        <button type="submit" className="danger" disabled={busy || typed.trim() !== accountId}>{busy ? "Resetting..." : "Reset account"}</button>
        <button type="button" onClick={close} disabled={busy}>Cancel</button>
      </div>
      {err && <div className="error" role="alert">{err}</div>}
    </form>
  );
}

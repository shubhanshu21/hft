import { useEffect, useState } from "react";
import Ticker from "./Ticker.jsx";
import { useOverview } from "../../hooks/useOverview.js";

const istNow = () => new Date().toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: "Asia/Kolkata" });

/** Ticker tape + a live IST clock + open/closed lights for the three markets. */
export default function TopBar() {
  const { data } = useOverview();
  const [clock, setClock] = useState(istNow);
  useEffect(() => { const id = setInterval(() => setClock(istNow()), 1000); return () => clearInterval(id); }, []);
  const mkt = (id) => data?.accounts?.find((a) => a.id === id);
  const lights = [["NSE", mkt("RESULTS_GAP")], ["MCX", mkt("MCX_TREND_CARRY")], ["Crypto", mkt("CRYPTO_DIP_REVERSAL")]];
  return (
    <div className="topbar">
      <Ticker />
      <div className="clocks">
        {lights.map(([name, a]) => <span key={name} className={`mk ${a?.market_open ? "open" : "closed"}`} title={a?.market_text}><i />{name}</span>)}
        <span className="ist">{clock} <small>IST</small></span>
      </div>
    </div>
  );
}

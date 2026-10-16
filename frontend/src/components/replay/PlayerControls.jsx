export default function PlayerControls({ player, idp = "" }) {
  const { playing, atEnd, toggle, seek, cur, speed, setSpeed, count, nextTrade, stopOnTrade, setStopOnTrade } = player;
  return (
    <div className="panel ctrl row-flex">
      <button className="main" type="button" onClick={toggle}>{playing ? "Pause" : atEnd ? "Play from start" : "Play"}</button>
      <button type="button" onClick={() => seek(cur - 1)}>&lsaquo; Day</button>
      <button type="button" onClick={() => seek(cur + 1)}>Day &rsaquo;</button>
      <button type="button" onClick={nextTrade}>Next trade &raquo;</button>
      <label htmlFor={`${idp}speed`} className="note">Speed</label>
      <select id={`${idp}speed`} value={speed} onChange={(e) => setSpeed(+e.target.value)}>
        <option value={1000}>Very slow (1 day per second)</option><option value={450}>Slow</option><option value={180}>Normal</option><option value={60}>Fast</option><option value={15}>Very fast</option>
      </select>
      <label className="note check" htmlFor={`${idp}stop`}><input id={`${idp}stop`} type="checkbox" checked={stopOnTrade} onChange={(e) => setStopOnTrade(e.target.checked)} /> Pause at each trade</label>
      <input type="range" min={0} max={count - 1} value={cur} aria-label="Day" onChange={(e) => seek(+e.target.value)} />
    </div>
  );
}

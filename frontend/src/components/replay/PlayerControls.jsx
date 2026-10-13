export default function PlayerControls({ player }) {
  const { playing, atEnd, toggle, seek, cur, speed, setSpeed, count } = player;
  return (
    <div className="panel ctrl row-flex">
      <button className="main" type="button" onClick={toggle}>{playing ? "Pause" : atEnd ? "Play from start" : "Play"}</button>
      <button type="button" onClick={() => seek(cur - 1)}>&lsaquo; Day</button>
      <button type="button" onClick={() => seek(cur + 1)}>Day &rsaquo;</button>
      <label htmlFor="speed" className="note">Speed</label>
      <select id="speed" value={speed} onChange={(e) => setSpeed(+e.target.value)}>
        <option value={250}>Slow</option><option value={110}>Normal</option><option value={40}>Fast</option><option value={12}>Very fast</option>
      </select>
      <input type="range" min={0} max={count - 1} value={cur} aria-label="Day" onChange={(e) => seek(+e.target.value)} />
    </div>
  );
}

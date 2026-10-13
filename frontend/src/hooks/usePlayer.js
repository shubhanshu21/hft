import { useEffect, useRef, useState } from "react";

/** Day-by-day playback over `count` days. Starts paused on the last day (the finished picture). */
export function usePlayer(count, events = []) {
  const [cur, setCur] = useState(count - 1);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(450);
  const [stopOnTrade, setStopOnTrade] = useState(false);
  const curRef = useRef(cur);
  curRef.current = cur;

  useEffect(() => {
    if (!playing) return undefined;
    const id = setInterval(() => {
      if (curRef.current >= count - 1) { setPlaying(false); return; }
      const next = curRef.current + 1;
      setCur(next);
      if (stopOnTrade && events[next]?.length) setPlaying(false);
    }, speed);
    return () => clearInterval(id);
  }, [playing, speed, count, stopOnTrade, events]);

  const seek = (i) => { setPlaying(false); setCur(Math.min(count - 1, Math.max(0, i))); };
  const toggle = () => {
    if (playing) { setPlaying(false); return; }
    if (cur >= count - 1) setCur(0);
    setPlaying(true);
  };
  const nextTrade = () => {
    for (let i = cur + 1; i < count; i++) if (events[i]?.length) { seek(i); return; }
  };
  return { cur, playing, speed, setSpeed, seek, toggle, nextTrade, stopOnTrade, setStopOnTrade, atEnd: cur >= count - 1 };
}

import { useEffect, useRef, useState } from "react";

/** Day-by-day playback over `count` days. Starts paused on the last day (the finished picture). */
export function usePlayer(count) {
  const [cur, setCur] = useState(count - 1);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(110);
  const curRef = useRef(cur);
  curRef.current = cur;

  useEffect(() => {
    if (!playing) return undefined;
    const id = setInterval(() => {
      if (curRef.current >= count - 1) { setPlaying(false); return; }
      setCur(curRef.current + 1);
    }, speed);
    return () => clearInterval(id);
  }, [playing, speed, count]);

  const seek = (i) => { setPlaying(false); setCur(Math.min(count - 1, Math.max(0, i))); };
  const toggle = () => {
    if (playing) { setPlaying(false); return; }
    if (cur >= count - 1) setCur(0);
    setPlaying(true);
  };
  return { cur, playing, speed, setSpeed, seek, toggle, atEnd: cur >= count - 1 };
}

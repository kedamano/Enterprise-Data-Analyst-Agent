"use client";
import {
  useScroll,
  useTransform,
  motion,
} from "motion/react";
import React, { useEffect, useRef, useState } from "react";

export type TimelineTone = "idle" | "active" | "success" | "warn" | "error";

interface TimelineEntry {
  title: string;
  content: React.ReactNode;
  /** 状态色调（控制圆点 + 滚动光束）。默认 idle（中性灰）。 */
  tone?: TimelineTone;
}

const TONE_DOT: Record<TimelineTone, string> = {
  idle:    "bg-slate-300 border-slate-300",
  active:  "bg-indigo-500 border-indigo-400 shadow-[0_0_10px_rgba(99,102,241,.45)]",
  success: "bg-emerald-500 border-emerald-400 shadow-[0_0_10px_rgba(16,185,129,.4)]",
  warn:    "bg-amber-500 border-amber-400 shadow-[0_0_10px_rgba(245,158,11,.4)]",
  error:   "bg-rose-500 border-rose-400 shadow-[0_0_10px_rgba(244,63,94,.4)]",
};

/** 标题配色：已完成用中性灰，仅「进行中 / 告警 / 失败」上色，避免整条轴过于花哨。 */
const TONE_TITLE: Record<TimelineTone, string> = {
  idle: "text-slate-400",
  active: "text-indigo-600 font-semibold",
  success: "text-slate-700",
  warn: "text-amber-600 font-semibold",
  error: "text-rose-600 font-semibold",
};

const TONE_BEAM = "from-indigo-400 via-violet-400 to-transparent";

export const Timeline = ({ data }: { data: TimelineEntry[] }) => {
  const ref = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [height, setHeight] = useState(0);

  useEffect(() => {
    if (ref.current) {
      const rect = ref.current.getBoundingClientRect();
      setHeight(rect.height);
    }
  }, [ref]);

  const { scrollYProgress } = useScroll({
    target: containerRef,
    offset: ["start 10%", "end 50%"],
  });

  const heightTransform = useTransform(scrollYProgress, [0, 1], [0, height]);
  const opacityTransform = useTransform(scrollYProgress, [0, 0.1], [0, 1]);

  return (
    <div
      className="w-full bg-white font-sans"
      ref={containerRef}
    >
      <div ref={ref} className="relative max-w-7xl mx-auto pb-2">
        {data.map((item, index) => {
          const tone: TimelineTone = item.tone ?? "idle";
          return (
            <div
              key={index}
              className="flex justify-start pt-2.5 md:pt-3 md:gap-3"
            >
              <div className="sticky flex flex-col md:flex-row z-40 items-center top-2 self-start max-w-[140px] lg:max-w-[160px] md:w-[140px] shrink-0">
                <div className="h-6 absolute left-2 md:left-2 w-6 rounded-full bg-white flex items-center justify-center">
                  <div
                    className={`h-2.5 w-2.5 rounded-full border-2 p-[2px] transition-colors ${TONE_DOT[tone]}`}
                  />
                </div>
                <h3
                  className={`hidden md:block text-small md:pl-10 leading-tight transition-colors ${TONE_TITLE[tone]}`}
                >
                  {item.title}
                </h3>
              </div>

              <div className="relative pl-10 pr-2 md:pl-1 w-full min-w-0">
                <h3
                  className={`md:hidden block text-body mb-1 text-left leading-tight ${TONE_TITLE[tone]}`}
                >
                  {item.title}
                </h3>
                {item.content}{" "}
              </div>
            </div>
          );
        })}
        <div
          style={{
            height: height + "px",
          }}
          className="absolute md:left-5 left-5 top-0 overflow-hidden w-[2px] bg-[linear-gradient(to_bottom,var(--tw-gradient-stops))] from-transparent from-[0%] via-slate-200 to-transparent to-[99%]  [mask-image:linear-gradient(to_bottom,transparent_0%,black_10%,black_90%,transparent_100%)] "
        >
          <motion.div
            style={{
              height: heightTransform,
              opacity: opacityTransform,
            }}
            className={`absolute inset-x-0 top-0  w-[2px] bg-gradient-to-t ${TONE_BEAM} from-[0%] via-[10%] rounded-full`}
          />
        </div>
      </div>
    </div>
  );
};

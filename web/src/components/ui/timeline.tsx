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
  idle:    "bg-rule border-rule-strong",
  active:  "bg-brand border-brand shadow-[0_0_10px_rgba(20,58,94,.35)]",
  success: "bg-verified border-verified shadow-[0_0_10px_rgba(27,127,90,.35)]",
  warn:    "bg-attention border-attention shadow-[0_0_10px_rgba(154,103,0,.35)]",
  error:   "bg-danger border-danger shadow-[0_0_10px_rgba(179,38,30,.35)]",
};

/** 标题配色：已完成用中性灰，仅「进行中 / 告警 / 失败」上色，避免整条轴过于花哨。 */
const TONE_TITLE: Record<TimelineTone, string> = {
  idle: "text-ink-3",
  active: "text-brand font-semibold",
  success: "text-ink-2",
  warn: "text-attention font-semibold",
  error: "text-danger font-semibold",
};

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
          className="absolute left-5 top-0 w-[2px] overflow-hidden rounded-full bg-rule [mask-image:linear-gradient(to_bottom,transparent_0%,black_10%,black_90%,transparent_100%)]"
        >
          <motion.div
            style={{
              height: heightTransform,
              opacity: opacityTransform,
            }}
            className="absolute inset-x-0 top-0 w-[2px] rounded-full bg-brand"
          />
        </div>
      </div>
    </div>
  );
};

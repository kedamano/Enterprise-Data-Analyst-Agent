import { useCallback, useEffect, useRef, useState } from "react";
import { useLocalStorage } from "@/lib/storage";

export function useMediaQuery(query: string): boolean {
  const [match, setMatch] = useState<boolean>(() =>
    typeof window !== "undefined" ? window.matchMedia(query).matches : false,
  );
  useEffect(() => {
    if (typeof window === "undefined") return;
    const mql = window.matchMedia(query);
    const onChange = () => setMatch(mql.matches);
    onChange();
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [query]);
  return match;
}

type Orientation = "horizontal" | "vertical";

/**
 * 指针拖拽尺寸（宽或高）—— 用于面板侧边 / 顶部把手的拖拽。
 *
 * - `orientation="horizontal"`：拖拽改变**宽度**（客户端点 X 位移）；
 * - `orientation="vertical"`：拖拽改变**高度**（客户端点 Y 位移）。
 *
 * 尺寸经 clamp 后写回 localStorage，刷新后生效。
 */
export function useResizablePanel(args: {
  storageKey: string;
  defaultSize: number;
  min: number;
  max: number;
  orientation: Orientation;
}): {
  size: number;
  /** 薄把手的绑定事件（onPointerDown 等）与样式 */
  handleProps: React.HTMLAttributes<HTMLDivElement>;
  reset: () => void;
} {
  const { storageKey, defaultSize, min, max, orientation } = args;
  const [size, setSize] = useLocalStorage<number>(storageKey, defaultSize);
  const start = useRef<{ size: number; pos: number } | null>(null);

  const onPointerDown = useCallback(
    (e: React.PointerEvent) => {
      e.preventDefault();
      e.currentTarget.setPointerCapture?.(e.pointerId);
      start.current = {
        size,
        pos: orientation === "horizontal" ? e.clientX : e.clientY,
      };
    },
    [size, orientation],
  );

  useEffect(() => {
    const onMove = (e: PointerEvent) => {
      if (!start.current) return;
      const pos = orientation === "horizontal" ? e.clientX : e.clientY;
      const next = Math.min(max, Math.max(min, start.current.size + (pos - start.current.pos)));
      setSize(next);
    };
    const onUp = () => {
      start.current = null;
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    };
  }, [min, max, orientation, setSize]);

  const reset = useCallback(() => setSize(defaultSize), [defaultSize, setSize]);

  return {
    size,
    reset,
    handleProps: {
      onPointerDown,
      style: {
        cursor: orientation === "horizontal" ? "col-resize" : "row-resize",
        touchAction: "none",
        userSelect: "none",
      },
    },
  };
}

"use client";
import { cn } from "@/lib/utils";
import { AnimatePresence, motion } from "motion/react";
import React, {
  type ReactNode,
  useEffect,
  useRef,
} from "react";

/**
 * 2026-09-15 重构：模态框改为「受属性驱动」(open / onClose)。
 *
 * 旧实现里 <Modal> 内部又包了一层 <ModalProvider>，导致每个弹窗的 open 状态
 * 是局部单例，而父组件通过 useModal().setOpen 改的是最外层 provider —— 两者
 * 永远不连通，于是导航点击「毫无反应」。现改为 props 直接驱动渲染，无单例陷阱。
 *
 * 保留 ModalProvider / useModal / ModalTrigger 仅为向后兼容（已成空实现）。
 */
export const ModalProvider = ({ children }: { children: ReactNode }) => <>{children}</>;
export const useModal = () => ({ open: false, setOpen: () => {} });
export const ModalTrigger = () => null;

const Overlay = ({ onClose }: { onClose?: () => void }) => (
  <motion.div
    initial={{ opacity: 0 }}
    animate={{ opacity: 1, backdropFilter: "blur(10px)" }}
    exit={{ opacity: 0, backdropFilter: "blur(0px)" }}
    onClick={onClose}
    className="fixed inset-0 h-full w-full bg-black bg-opacity-50 z-50"
  />
);

const CloseIcon = ({ onClose }: { onClose?: () => void }) => (
  <button
    type="button"
    onClick={onClose}
    aria-label="关闭"
    className="absolute top-4 right-4 z-10 grid h-8 w-8 place-items-center rounded-control text-slate-400 transition hover:bg-slate-100 hover:text-slate-700"
  >
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M18 6l-12 12" />
      <path d="M6 6l12 12" />
    </svg>
  </button>
);

export function Modal({
  open,
  onClose,
  children,
  className,
}: {
  open: boolean;
  onClose?: () => void;
  children: ReactNode;
  className?: string;
}) {
  // 打开时锁定背景滚动；关闭/卸载时还原
  useEffect(() => {
    if (open) document.body.style.overflow = "hidden";
    else document.body.style.overflow = "auto";
    return () => {
      document.body.style.overflow = "auto";
    };
  }, [open]);

  // Esc 关闭
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose?.();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const modalRef = useRef<HTMLDivElement>(null);

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1, backdropFilter: "blur(10px)" }}
          exit={{ opacity: 0, backdropFilter: "blur(0px)" }}
          className="fixed [perspective:800px] [transform-style:preserve-3d] inset-0 h-full w-full flex items-center justify-center z-50"
        >
          <Overlay onClose={onClose} />
          <motion.div
            ref={modalRef}
            onClick={(e) => e.stopPropagation()}
            className={cn(
              "min-h-[40%] max-h-[90%] w-full max-w-lg md:max-w-[42%] bg-white border border-slate-200 md:rounded-panel relative z-50 flex flex-col flex-1 overflow-hidden shadow-2xl shadow-slate-900/10",
              className ?? "",
            )}
            initial={{ opacity: 0, scale: 0.5, rotateX: 40, y: 40 }}
            animate={{ opacity: 1, scale: 1, rotateX: 0, y: 0 }}
            exit={{ opacity: 0, scale: 0.8, rotateX: 10 }}
            transition={{ type: "spring", stiffness: 260, damping: 15 }}
          >
            <CloseIcon onClose={onClose} />
            {children}
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

export const ModalBody = ({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) => (
  <div className={cn("flex flex-col flex-1 overflow-y-auto p-6 md:p-8", className)}>
    {children}
  </div>
);

export const ModalContent = ({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) => <div className={cn("flex flex-col flex-1", className)}>{children}</div>;

export const ModalFooter = ({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) => (
  <div className={cn("flex justify-end gap-2 border-t border-slate-100 bg-slate-50 px-6 py-3 md:px-8", className)}>
    {children}
  </div>
);

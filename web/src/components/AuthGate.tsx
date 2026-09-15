import { useState } from "react";
import {
  Modal,
  ModalBody,
  ModalContent,
} from "@/components/ui/animated-modal";
import { setApiKey } from "@/lib/auth";

/**
 * #1 前端登录桥接：后端 AUTH_ENABLED=true 时，所有 API 返回 401/503 会触发本弹窗。
 * 用户输入 X-API-Key → 存 localStorage → 回调让上层重试刚才的请求。
 */
export function AuthGate({
  open,
  onAuthed,
  onCancel,
}: {
  open: boolean;
  onAuthed: () => void;
  onCancel: () => void;
}) {
  const [key, setKey] = useState("");

  const submit = () => {
    const v = key.trim();
    if (!v) return;
    setApiKey(v); // 存同源 localStorage，后续请求自动带 X-API-Key
    onAuthed();
  };

  return (
    <Modal open={open} onClose={onCancel}>
      <ModalBody className="max-w-md">
        <ModalContent>
          <h2 className="text-lg font-semibold text-slate-900">需要 API Key</h2>
          <p className="mt-2 text-sm leading-relaxed text-slate-500">
            服务端已开启鉴权。请输入分配给你的 <code className="rounded bg-slate-100 px-1">X-API-Key</code>
            ，提交后将以该身份继续本次分析。
          </p>
          <input
            type="password"
            value={key}
            autoFocus
            onChange={(e) => setKey(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit()}
            placeholder="粘贴 API Key"
            aria-label="API Key"
            className="mt-4 w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-800 outline-none focus:border-indigo-400"
          />
          <div className="mt-4 flex justify-end gap-2">
            <button
              onClick={onCancel}
              className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-600 transition hover:bg-slate-50"
            >
              取消
            </button>
            <button
              onClick={submit}
              disabled={!key.trim()}
              className="rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-indigo-700 disabled:opacity-50"
            >
              确定
            </button>
          </div>
        </ModalContent>
      </ModalBody>
    </Modal>
  );
}

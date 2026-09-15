import { useState } from "react";
import {
  Modal,
  ModalBody,
  ModalContent,
} from "@/components/ui/animated-modal";
import { setApiKey } from "@/lib/auth";

/**
 * 访问凭证弹窗：服务端开启访问控制后，请求被拒（401/503）时弹出。
 *
 * 用户在弹窗里输入管理员分配的访问密钥 → 存同源 localStorage → 回调让上层重试刚才的请求。
 * 按钮与提示文案保持一致口径：主按钮说「继续」，成功后接着说「已继续」。
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
  const [submitting, setSubmitting] = useState(false);

  const submit = () => {
    const v = key.trim();
    if (!v || submitting) return;
    setSubmitting(true);
    setApiKey(v); // 存同源 localStorage，后续请求自动携带
    onAuthed();
  };

  return (
    <Modal open={open} onClose={onCancel}>
      <ModalBody className="max-w-md">
        <ModalContent>
          <h2 className="text-heading font-semibold text-ink">需要访问密钥</h2>
          <p className="mt-2 text-body leading-relaxed text-ink-3">
            这台服务开启了访问控制。请输入管理员分配给你的访问密钥，
            提交后会回到刚才的操作继续。不知道密钥是什么？向部署这套服务的同事索取。
          </p>
          <input
            type="password"
            value={key}
            autoFocus
            onChange={(e) => setKey(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit()}
            placeholder="粘贴访问密钥"
            aria-label="访问密钥"
            className="mt-4 w-full rounded-control border border-rule bg-white px-3 py-2 text-body text-ink outline-none focus:border-brand"
          />
          <div className="mt-4 flex justify-end gap-2">
            <button
              type="button"
              onClick={onCancel}
              className="rounded-control border border-rule bg-white px-3 py-1.5 text-small font-medium text-ink-2 transition-colors hover:bg-canvas"
            >
              取消
            </button>
            <button
              type="button"
              onClick={submit}
              disabled={!key.trim() || submitting}
              className="rounded-control bg-brand px-3 py-1.5 text-small font-medium text-white transition-colors hover:bg-brand-hover disabled:opacity-50"
            >
              {submitting ? "正在继续…" : "继续"}
            </button>
          </div>
        </ModalContent>
      </ModalBody>
    </Modal>
  );
}

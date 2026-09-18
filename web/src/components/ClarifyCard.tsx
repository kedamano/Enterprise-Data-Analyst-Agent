import { useState } from "react";
import { HelpCircle, Send } from "@/components/icons";
import type { Clarification } from "../lib/api";

/**
 * CLARIFY/01：澄清卡片。
 *
 * 后端在 status=CLARIFY 时给出结构化问题（最多 3 个）；用户作答后回到同一会话继续跑。
 * 这是"更懂分析师"最直接的一处——真实分析师遇到模糊请求会反问一句，
 * 而不是丢一句"错误"就结束。
 */
export function ClarifyCard({
  clarification,
  answered,
  onAnswer,
}: {
  clarification: Clarification;
  answered?: boolean;
  onAnswer?: (answer: string) => void;
}) {
  const [text, setText] = useState("");
  const questions = clarification.questions ?? [];

  const submit = () => {
    const value = text.trim();
    if (!value || !onAnswer) return;
    onAnswer(value);
    setText("");
  };

  return (
    <div className="mb-3 rounded-panel border border-attention bg-attention-soft p-4">
      <div className="mb-2 flex items-center gap-2">
        <HelpCircle className="h-4 w-4 text-attention" />
        <span className="text-small font-medium text-attention">
          {answered ? "已澄清" : "需要你确认"}
        </span>
      </div>

      {clarification.objective && (
        <p className="mb-2 text-small text-attention">
          目标：{clarification.objective}
        </p>
      )}

      <ol className="mb-3 list-decimal space-y-1 pl-5 text-body text-attention">
        {questions.map((q, i) => (
          <li key={i}>{q}</li>
        ))}
      </ol>

      {!answered && onAnswer && (
        <div className="flex items-end gap-2">
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
            rows={2}
            placeholder="直接回答上面的问题，例如：不含退款，对比去年同期"
            aria-label="澄清回答"
            className="flex-1 resize-none rounded-panel border border-attention bg-white px-3 py-2 text-body outline-none focus:border-attention"
          />
          <button
            onClick={submit}
            disabled={!text.trim()}
            aria-label="提交澄清回答"
            className="flex items-center gap-1 rounded-panel bg-attention px-3 py-2 text-body text-white disabled:opacity-40"
          >
            <Send className="h-4 w-4" />
            发送
          </button>
        </div>
      )}

      {(clarification.assumptions ?? []).length > 0 && (
        <p className="mt-2 text-small text-attention">
          已假设：{(clarification.assumptions ?? []).join("；")}
        </p>
      )}
    </div>
  );
}

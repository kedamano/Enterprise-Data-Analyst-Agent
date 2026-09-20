/**
 * FeedbackWidget — 分析报告底部的用户反馈组件。
 *
 * 两种反馈方式：
 *   1. 👍 / 👎 大按钮（对应 +1 / -1），点一个即提交
 *   2. 展开 "写几句..." 文本框 + 发送（文字随 rating 一起推）
 *
 * 三种状态：loading / success / error。
 *
 * 已提交过时显示 "已反馈" + "修改反馈" 按钮。
 */
import { useState } from "react";
import { fetchFeedback, submitFeedback, type FeedbackPayload } from "@/lib/api";

export function FeedbackWidget({ sessionId }: { sessionId: string }) {
  const [rating, setRating] = useState<1 | -1 | null>(null);
  const [comment, setComment] = useState("");
  const [showCommentBox, setShowCommentBox] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editingExisting, setEditingExisting] = useState(false);

  async function handleRate(value: 1 | -1) {
    setRating(value);
    setShowCommentBox(true);
    // 如果之前没提交过且没有文字框，可以直接只提交 rating
    // 但给用户体验：先展开文字框让他们可选写
    if (submitted) {
      // 修改已有反馈：立刻提交
      await doSubmit(value, comment);
    }
  }

  const doSubmit = async (rate: 1 | -1, cmt: string) => {
    setSubmitting(true);
    setError(null);
    try {
      const payload: FeedbackPayload = { rating: rate, comment: cmt };
      await submitFeedback(sessionId, payload);
      setSubmitted(true);
      setShowCommentBox(false);
      setEditingExisting(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "提交失败，请重试");
    } finally {
      setSubmitting(false);
    }
  }

  function handleSendComment() {
    if (rating === null) return;
    doSubmit(rating, comment);
  }

  function handleEdit() {
    setEditingExisting(true);
    setShowCommentBox(true);
    setSubmitted(false);
    // 尝试拉已有评论预填
    fetchFeedback(sessionId)
      .then((fb) => {
        if (fb) {
          setRating(fb.rating as 1 | -1);
          setComment(fb.comment ?? "");
        }
      })
      .catch(() => {});
  }

  // 已反馈 + 不在编辑模式：显示简洁的"已反馈"状态
  if (submitted && !editingExisting) {
    return (
      <div className="mt-4 rounded-panel border border-rule bg-canvas/40 px-4 py-3">
        <div className="flex items-center justify-between">
          <span className="text-micro text-verified font-medium">
            已感谢您的反馈 {rating === 1 ? "👍" : "👎"}
          </span>
          <button
            onClick={handleEdit}
            className="text-micro text-ink-3 hover:text-brand transition-colors"
          >
            修改反馈
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="mt-4 rounded-panel border border-rule bg-canvas/40 px-4 py-3">
      <div className="mb-2 text-micro font-medium text-ink-2">
        这个分析结果对你有帮助吗？
      </div>

      {/* 评分按钮行 */}
      <div className="flex items-center gap-2">
        <button
          onClick={() => handleRate(1)}
          disabled={submitting}
          className={`inline-flex items-center gap-1.5 rounded-control border px-4 py-2 text-body font-medium transition-all
            ${
              rating === 1
                ? "border-verified bg-verified/10 text-verified"
                : "border-rule bg-white text-ink-2 hover:border-verified/50 hover:text-verified"
            }
            disabled:opacity-50`}
        >
          👍 有帮助
        </button>
        <button
          onClick={() => handleRate(-1)}
          disabled={submitting}
          className={`inline-flex items-center gap-1.5 rounded-control border px-4 py-2 text-body font-medium transition-all
            ${
              rating === -1
                ? "border-danger bg-danger/10 text-danger"
                : "border-rule bg-white text-ink-2 hover:border-danger/50 hover:text-danger"
            }
            disabled:opacity-50`}
        >
          👎 没帮助
        </button>
        {submitting && (
          <div className="ml-2 h-4 w-4 animate-spin rounded-full border-2 border-brand border-t-transparent" />
        )}
      </div>

      {/* 展开的文字反馈区 */}
      {showCommentBox && rating !== null && !submitted && (
        <div className="mt-3">
          <textarea
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            maxLength={500}
            placeholder="说几句具体的（哪里好 / 哪里不准 / 缺什么）……"
            className="w-full resize-none rounded-control border border-rule bg-white px-3 py-2 text-body text-ink-1 placeholder:text-ink-3 focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand/30"
            rows={3}
          />
          <div className="mt-2 flex items-center justify-between">
            <span className="text-micro text-ink-3">
              {comment.length}/500
            </span>
            <button
              onClick={handleSendComment}
              disabled={submitting}
              className="inline-flex items-center gap-1.5 rounded-control border border-brand bg-brand px-4 py-1.5 text-body font-medium text-white transition-all hover:bg-brand/90 disabled:opacity-50"
            >
              发送反馈
            </button>
          </div>
        </div>
      )}

      {error && (
        <div className="mt-2 text-micro text-danger">{error}</div>
      )}
    </div>
  );
}

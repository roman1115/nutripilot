import { FormEvent, useState } from "react";
import {
  Check,
  LoaderCircle,
  RefreshCw,
  Star,
  ThumbsDown,
  ThumbsUp,
  X,
} from "lucide-react";
import { ErrorNotice } from "../../components/Common";
import { submitMealFeedback } from "../../lib/api";
import type { FeedbackType, MealFeedback, MealPlanItem } from "../../types";

const feedbackOptions: Array<{
  value: FeedbackType;
  label: string;
  icon: typeof ThumbsUp;
}> = [
  { value: "like", label: "喜欢", icon: ThumbsUp },
  { value: "dislike", label: "不喜欢", icon: ThumbsDown },
  { value: "replace", label: "想替换", icon: RefreshCw },
  { value: "rating", label: "评分", icon: Star },
];

export function FeedbackDialog({
  meal,
  onClose,
  onSubmitted,
}: {
  meal: MealPlanItem;
  onClose: () => void;
  onSubmitted: (feedback: MealFeedback) => void;
}) {
  const [feedbackType, setFeedbackType] = useState<FeedbackType>("like");
  const [rating, setRating] = useState(5);
  const [reason, setReason] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mealId = Number(meal.mealId);
  const hasValidMealId = Number.isInteger(mealId) && mealId > 0;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!hasValidMealId || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const result = await submitMealFeedback({
        mealId,
        feedbackType,
        rating: feedbackType === "rating" ? rating : undefined,
        reason,
      });
      onSubmitted(result);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "反馈提交失败");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={() => !submitting && onClose()}>
      <form
        className="modal feedback-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="feedback-title"
        onMouseDown={(event) => event.stopPropagation()}
        onSubmit={submit}
      >
        <div className="modal-heading">
          <div>
            <span className="eyebrow">Preference feedback</span>
            <h2 id="feedback-title">评价这份餐食</h2>
          </div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="关闭" disabled={submitting}>
            <X size={18} />
          </button>
        </div>

        <div className="feedback-meal-context">
          <span>{meal.mealType || "餐食"}</span>
          <strong>{meal.mealName}</strong>
          {meal.calories !== undefined ? <b>{Math.round(meal.calories)} kcal</b> : null}
        </div>

        {!hasValidMealId ? (
          <ErrorNotice message="服务端没有为这份餐食返回可用的 Meal ID，暂时无法记录反馈。" />
        ) : null}
        {error ? <ErrorNotice message={error} /> : null}

        <fieldset className="feedback-fieldset" disabled={submitting || !hasValidMealId}>
          <legend>反馈类型</legend>
          <div className="feedback-type-control">
            {feedbackOptions.map((option) => {
              const Icon = option.icon;
              return (
                <button
                  className={feedbackType === option.value ? "is-active" : ""}
                  key={option.value}
                  type="button"
                  onClick={() => setFeedbackType(option.value)}
                  aria-pressed={feedbackType === option.value}
                >
                  <Icon size={16} />
                  <span>{option.label}</span>
                </button>
              );
            })}
          </div>
        </fieldset>

        {feedbackType === "rating" ? (
          <fieldset className="feedback-fieldset" disabled={submitting || !hasValidMealId}>
            <legend>满意度</legend>
            <div className="star-rating" aria-label={`${rating} 星`}>
              {[1, 2, 3, 4, 5].map((value) => (
                <button
                  key={value}
                  type="button"
                  className={value <= rating ? "is-active" : ""}
                  onClick={() => setRating(value)}
                  aria-label={`${value} 星`}
                >
                  <Star size={22} fill={value <= rating ? "currentColor" : "none"} />
                </button>
              ))}
              <strong>{rating}.0</strong>
            </div>
          </fieldset>
        ) : null}

        <label className="field feedback-reason">
          <span>补充原因 <small>选填</small></span>
          <textarea
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            maxLength={500}
            rows={3}
            placeholder={feedbackType === "replace" ? "希望替换成什么，或哪些条件不合适？" : "记录口味、分量或场景感受"}
            disabled={submitting || !hasValidMealId}
          />
          <small>{reason.length}/500</small>
        </label>

        <div className="modal-actions">
          <button className="button button-secondary" type="button" onClick={onClose} disabled={submitting}>
            取消
          </button>
          <button className="button button-primary" type="submit" disabled={submitting || !hasValidMealId}>
            {submitting ? <LoaderCircle className="spin" size={16} /> : <Check size={16} />}
            提交反馈
          </button>
        </div>
      </form>
    </div>
  );
}

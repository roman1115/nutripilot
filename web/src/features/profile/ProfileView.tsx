import { useEffect, useMemo, useState } from "react";
import {
  Check,
  LoaderCircle,
  RefreshCw,
  Save,
  ShieldAlert,
  UserRound,
  X,
} from "lucide-react";
import { ErrorNotice, LoadingState, PageHeader, TagEditor } from "../../components/Common";
import { getProfile, updateProfile } from "../../lib/api";
import { formatDate } from "../../lib/format";
import type { ConnectionState, UserProfile } from "../../types";

const emptyProfile: UserProfile = {
  displayName: "",
  goals: [],
  tastes: [],
  cuisines: [],
  avoidances: [],
  allergens: [],
  dietaryRestrictions: [],
  location: "",
};

export function ProfileView({ connection }: { connection: ConnectionState }) {
  const [profile, setProfile] = useState<UserProfile | null>(null);
  const [baseline, setBaseline] = useState<UserProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [reviewOpen, setReviewOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const value = await getProfile();
      setProfile(value);
      setBaseline(value);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "无法读取用户画像");
      setProfile(null);
      setBaseline(null);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (connection !== "checking") void load();
  }, [connection]);

  const isDirty = useMemo(
    () => profile !== null && baseline !== null && JSON.stringify(profile) !== JSON.stringify(baseline),
    [baseline, profile],
  );

  const changedFields = useMemo(() => {
    if (!profile || !baseline) return [];
    const labels: Array<[keyof UserProfile, string]> = [
      ["displayName", "姓名"],
      ["goals", "健康目标"],
      ["tastes", "口味偏好"],
      ["cuisines", "菜系偏好"],
      ["avoidances", "不喜欢"],
      ["allergens", "过敏原"],
      ["dietaryRestrictions", "饮食限制"],
      ["dailyCalorieTarget", "每日热量目标"],
      ["dailyProteinTarget", "每日蛋白质目标"],
      ["location", "所在城市"],
    ];
    return labels.filter(([key]) => JSON.stringify(profile[key]) !== JSON.stringify(baseline[key])).map(([, label]) => label);
  }, [baseline, profile]);

  const submit = async () => {
    if (!profile) return;
    setSaving(true);
    setError(null);
    setSuccess(null);
    try {
      const saved = await updateProfile(profile);
      setProfile(saved);
      setBaseline(saved);
      setReviewOpen(false);
      setSuccess("饮食偏好已更新。");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "画像保存失败");
      setReviewOpen(false);
    } finally {
      setSaving(false);
    }
  };

  const update = <K extends keyof UserProfile>(key: K, value: UserProfile[K]) => {
    setProfile((current) => (current ? { ...current, [key]: value } : current));
    setSuccess(null);
  };

  return (
    <div className="page-container">
      <PageHeader
        eyebrow="Preference memory"
        title="饮食偏好"
        description=""
        action={
          <button className="button button-secondary" type="button" onClick={() => void load()} disabled={loading || connection === "checking"}>
            <RefreshCw size={16} className={loading ? "spin" : ""} />
            刷新
          </button>
        }
      />

      {error ? <ErrorNotice message={error} /> : null}
      {success ? (
        <div className="notice notice-success" role="status">
          <Check size={18} />
          <div><strong>保存成功</strong><p>{success}</p></div>
        </div>
      ) : null}

      {loading ? (
        <LoadingState label="正在读取用户画像" />
      ) : profile ? (
        <form className="profile-form" onSubmit={(event) => { event.preventDefault(); if (isDirty) setReviewOpen(true); }}>
          <section className="form-section identity-section">
            <div className="section-heading">
              <div className="section-icon"><UserRound size={18} /></div>
              <div><h2>日常偏好</h2></div>
            </div>
            <div className="form-grid form-grid-two">
              <label className="field field-block">
                <span>姓名</span>
                <input
                  value={profile.displayName}
                  onChange={(event) => update("displayName", event.target.value)}
                  placeholder="你的称呼"
                  maxLength={80}
                />
              </label>
              <label className="field field-block">
                <span>所在城市</span>
                <input
                  value={profile.location}
                  onChange={(event) => update("location", event.target.value)}
                  placeholder="如：上海"
                  maxLength={100}
                />
              </label>
              <label className="field field-block">
                <span>每日热量目标（kcal）</span>
                <input
                  type="number"
                  min={800}
                  max={10000}
                  step={50}
                  value={profile.dailyCalorieTarget ?? ""}
                  onChange={(event) =>
                    update(
                      "dailyCalorieTarget",
                      event.target.value ? Number(event.target.value) : undefined,
                    )
                  }
                  placeholder="如：2000"
                />
              </label>
              <label className="field field-block">
                <span>每日蛋白质目标（g）</span>
                <input
                  type="number"
                  min={0}
                  max={1000}
                  step={1}
                  value={profile.dailyProteinTarget ?? ""}
                  onChange={(event) =>
                    update("dailyProteinTarget", event.target.value ? Number(event.target.value) : undefined)
                  }
                />
              </label>
              <label className="field field-block">
                <span>健康目标</span>
                <TagEditor value={profile.goals} onChange={(value) => update("goals", value)} placeholder="如：控制体重" />
              </label>
              <label className="field field-block">
                <span>口味偏好</span>
                <TagEditor value={profile.tastes} onChange={(value) => update("tastes", value)} placeholder="如：清淡" />
              </label>
              <label className="field field-block">
                <span>菜系偏好</span>
                <TagEditor value={profile.cuisines} onChange={(value) => update("cuisines", value)} placeholder="如：中式" />
              </label>
              <label className="field field-block">
                <span>不喜欢的食物</span>
                <TagEditor value={profile.avoidances} onChange={(value) => update("avoidances", value)} placeholder="如：香菜" />
              </label>
            </div>
          </section>

          <section className="form-section safety-section">
            <div className="section-heading">
              <div className="section-icon"><ShieldAlert size={18} /></div>
              <div><h2>过敏与饮食限制</h2><p>请只填写已确认的信息。</p></div>
            </div>
            <div className="form-grid form-grid-two">
              <label className="field field-block">
                <span>过敏原</span>
                <TagEditor value={profile.allergens} onChange={(value) => update("allergens", value)} placeholder="如：花生" />
              </label>
              <label className="field field-block">
                <span>饮食限制</span>
                <TagEditor value={profile.dietaryRestrictions} onChange={(value) => update("dietaryRestrictions", value)} placeholder="如：素食" />
              </label>
            </div>
          </section>

          <div className="form-footer">
            <p>{profile.updatedAt ? `上次更新于 ${formatDate(profile.updatedAt)}` : "尚无更新记录"}</p>
            <div>
              <button className="button button-secondary" type="button" onClick={() => baseline && setProfile(baseline)} disabled={!isDirty}>
                <X size={16} />撤销修改
              </button>
              <button className="button button-primary" type="submit" disabled={!isDirty || saving || connection !== "online"}>
                <Save size={16} />保存修改
              </button>
            </div>
          </div>
        </form>
      ) : (
        <div className="empty-data-panel">
          <UserRound size={24} />
          <h2>未读取到用户画像</h2>
          <p>请确认 API 已启动且当前演示用户存在。页面不会自动创建或填充模拟画像。</p>
        </div>
      )}

      {reviewOpen && profile ? (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => !saving && setReviewOpen(false)}>
          <section className="modal" role="dialog" aria-modal="true" aria-labelledby="profile-review-title" onMouseDown={(event) => event.stopPropagation()}>
            <div className="modal-heading">
              <div><h2 id="profile-review-title">确认更新饮食偏好</h2></div>
              <button className="icon-button" type="button" onClick={() => setReviewOpen(false)} aria-label="关闭" disabled={saving}><X size={18} /></button>
            </div>
            <p className="modal-description">这些长期信息会影响后续推荐与安全过滤，请确认内容准确。</p>
            <div className="review-summary">
              <span>本次变更字段</span>
              <div className="tag-row">{changedFields.map((field) => <span key={field}>{field}</span>)}</div>
              {profile.allergens.length ? <p className="safety-copy"><ShieldAlert size={15} />过敏原：{profile.allergens.join("、")}</p> : null}
            </div>
            <div className="modal-actions">
              <button className="button button-secondary" type="button" onClick={() => setReviewOpen(false)} disabled={saving}>返回检查</button>
              <button className="button button-primary" type="button" onClick={() => void submit()} disabled={saving}>
                {saving ? <LoaderCircle className="spin" size={16} /> : <Check size={16} />}
                确认保存
              </button>
            </div>
          </section>
        </div>
      ) : null}
    </div>
  );
}

export { emptyProfile };

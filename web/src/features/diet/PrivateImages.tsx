import { useEffect, useState } from "react";
import { privateResponse } from "../../lib/api";

export function PrivateImages({ ids }: { ids: string[] }) {
  return <div className="diet-private-images">{ids.map((id, index) => <PrivateImage key={id} id={id} index={index} />)}</div>;
}

function PrivateImage({ id, index }: { id: string; index: number }) {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    let resource: string | null = null;
    void privateResponse(`/api/v1/diet/media/${encodeURIComponent(id)}/content`).then((response) => response.blob()).then((blob) => {
      if (!active) return;
      resource = URL.createObjectURL(blob); setUrl(resource);
    }).catch((caught) => { if (active) setError(caught instanceof Error ? caught.message : "照片读取失败"); });
    return () => { active = false; if (resource) URL.revokeObjectURL(resource); };
  }, [id]);
  return <figure>{url ? <img src={url} alt={`本次饮食记录的私有照片 ${index + 1}`} /> : <p role="status">{error || "正在读取照片"}</p>}<figcaption>照片 {index + 1}</figcaption></figure>;
}

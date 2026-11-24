import { useState, useEffect } from "react";
import type { Cam } from "../pages/CamerasPage";

interface Props {
  open: boolean;
  editCam: Cam | null;
  onClose: () => void;
  onSave: (name: string, rtspUrl: string) => Promise<string | null>;
}

export default function CameraFormModal({ open, editCam, onClose, onSave }: Props) {
  const [name, setName] = useState("");
  const [rtspUrl, setRtspUrl] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false); // POST/PUT 진행중 — 버튼 disable + '등록 중…' (#124)

  // 창을 열 때만 오류·진행 상태를 비운다.
  // editCam 변경 때 비우면, 수정 충돌로 최신 값을 다시 채울 때 충돌 안내 문구가 바로 지워진다.
  useEffect(() => {
    if (open) {
      setError("");
      setSubmitting(false);
    }
  }, [open]);

  // 창을 열 때와 수정 충돌로 최신 카메라 값을 받았을 때 입력란을 그 값으로 채운다.
  useEffect(() => {
    if (open) {
      setName(editCam?.name ?? "");
      setRtspUrl(editCam?.rtsp_url ?? "");
    }
  }, [open, editCam]);

  if (!open) return null;

  // onSave 는 부모(CamerasPage)에서 POST/PUT /api/ipcams 로 처리(성공 null / 실패 메시지).
  // await 동안 버튼 disable + '등록 중…' 표시(#124) — 등록이 몇 초 걸려도 진행중임이 보이게.
  async function handleSubmit() {
    if (!name.trim() || !rtspUrl.trim()) {
      setError("이름과 RTSP URL을 입력하세요.");
      return;
    }
    setError("");
    setSubmitting(true);
    const err = await onSave(name.trim(), rtspUrl.trim());
    setSubmitting(false);
    if (err) setError(err); // 실패 — 모달 유지 + 버튼 원복(재시도 가능)
    else onClose(); // 성공 — 닫고 목록 갱신(부모 fetchCams)
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>{editCam ? "카메라 수정" : "카메라 등록"}</h2>

        <div className="field">
          <label>카메라 이름</label>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="예: 정문 입구 카메라"
            maxLength={100} // 백엔드 상한(DB 컬럼 길이)과 같다
          />
        </div>

        <div className="field">
          <label>RTSP URL</label>
          <input
            value={rtspUrl}
            onChange={(e) => setRtspUrl(e.target.value)}
            placeholder="rtsp://192.168.0.100:554/stream1"
            maxLength={500} // 백엔드 상한(DB 컬럼 길이)과 같다
          />
          <p className="hint">
            형식 — rtsp://[user:pass@]IP:PORT/PATH · 지원 코덱 H.264 / H.265
          </p>
          {editCam && rtspUrl.includes(":***@") && (
            <p className="hint">
              ⚠️ <code>***</code> = 기존 비밀번호 유지 — RTSP 주소를 바꾸지 않고 이름만 수정할 때만 유효합니다.
              주소·포트·경로 또는 비밀번호를 변경하려면 <code>***</code> 를 지우고 실제 비밀번호를 다시 입력하세요.
            </p>
          )}
        </div>

        {error && <p className="form-error">{error}</p>}

        <div className="modal-actions">
          <button onClick={onClose} disabled={submitting}>취소</button>
          <button className="primary" onClick={handleSubmit} disabled={submitting}>
            {submitting ? (editCam ? "저장 중…" : "등록 중…") : editCam ? "저장" : "등록"}
          </button>
        </div>
      </div>
    </div>
  );
}

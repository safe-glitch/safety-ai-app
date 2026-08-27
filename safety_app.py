import base64
import hashlib
import io
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
from anthropic import Anthropic
from PIL import Image, ImageDraw

try:
    from streamlit_cropper import st_cropper

    HAS_CROPPER = True
except Exception:
    HAS_CROPPER = False

ROLES = ["근로자", "관리감독자", "안전관리자", "보건관리자", "사업주/경영진", "기타"]
DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "inspections.db"

st.set_page_config(page_title="산업안전 AI 점검", layout="wide", page_icon="🦺")


def get_conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS inspections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            name TEXT NOT NULL,
            department TEXT NOT NULL,
            role TEXT NOT NULL,
            site TEXT NOT NULL,
            work_type TEXT,
            risk_level TEXT,
            analysis TEXT NOT NULL,
            focus_used INTEGER DEFAULT 0
        )
        """
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(inspections)")}
    if "focus_used" not in cols:
        conn.execute("ALTER TABLE inspections ADD COLUMN focus_used INTEGER DEFAULT 0")
    conn.commit()
    return conn


def load_pil(photo) -> Image.Image:
    raw = photo.getvalue() if hasattr(photo, "getvalue") else photo.read()
    return Image.open(io.BytesIO(raw)).convert("RGB")


def photo_key(photo) -> str:
    raw = photo.getvalue() if hasattr(photo, "getvalue") else photo.read()
    return hashlib.md5(raw).hexdigest()[:12]


def image_to_jpeg_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode()


def is_meaningful_focus(full: Image.Image, focus: Image.Image | None) -> bool:
    if focus is None:
        return False
    fw, fh = full.size
    cw, ch = focus.size
    if cw < 16 or ch < 16:
        return False
    area_ratio = (cw * ch) / max(1, fw * fh)
    return area_ratio <= 0.92


def draw_focus_overlay(full: Image.Image, box: dict) -> Image.Image:
    overlay = full.copy()
    draw = ImageDraw.Draw(overlay)
    left = int(box["left"])
    top = int(box["top"])
    right = left + int(box["width"])
    bottom = top + int(box["height"])
    for i in range(3):
        draw.rectangle([left - i, top - i, right + i, bottom + i], outline="#E53935")
    return overlay


def focus_by_cropper(full_img: Image.Image, key: str) -> tuple[Image.Image | None, dict | None]:
    result = st_cropper(
        full_img,
        realtime_update=True,
        box_color="#E53935",
        aspect_ratio=None,
        return_type="both",
        stroke_width=3,
        key=f"cropper_{key}",
    )
    if isinstance(result, tuple) and len(result) == 2:
        cropped, box = result
        return cropped.convert("RGB"), box
    if isinstance(result, Image.Image):
        return result.convert("RGB"), None
    return None, None


def focus_by_sliders(full_img: Image.Image, key: str) -> tuple[Image.Image | None, dict | None]:
    st.caption("네모 드래그가 안 될 때 쓰는 보조 방식입니다. 슬라이더로 구간을 지정하세요.")
    w, h = full_img.size
    c1, c2 = st.columns(2)
    with c1:
        x1_pct = st.slider("왼쪽 (%)", 0, 95, 20, key=f"x1_{key}")
        y1_pct = st.slider("위 (%)", 0, 95, 20, key=f"y1_{key}")
    with c2:
        x2_pct = st.slider("오른쪽 (%)", 5, 100, 80, key=f"x2_{key}")
        y2_pct = st.slider("아래 (%)", 5, 100, 80, key=f"y2_{key}")

    if x2_pct <= x1_pct + 2 or y2_pct <= y1_pct + 2:
        st.warning("오른쪽/아래 값이 왼쪽/위보다 커야 합니다.")
        return None, None

    left = int(w * x1_pct / 100)
    top = int(h * y1_pct / 100)
    right = int(w * x2_pct / 100)
    bottom = int(h * y2_pct / 100)
    box = {"left": left, "top": top, "width": right - left, "height": bottom - top}
    cropped = full_img.crop((left, top, right, bottom))
    return cropped, box


def load_records(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT
            id AS 번호,
            created_at AS 점검일시,
            name AS 성명,
            department AS 소속,
            role AS 직책,
            site AS 사업장_위치,
            work_type AS 작업내용,
            risk_level AS 위험등급,
            CASE focus_used WHEN 1 THEN 'Y' ELSE 'N' END AS 포커스사용,
            analysis AS 분석결과
        FROM inspections
        ORDER BY id DESC
        """,
        conn,
    )


def to_excel_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="안전점검기록")
    return buf.getvalue()


def analyze_image(
    client: Anthropic,
    full_img: Image.Image,
    user: dict,
    focus_img: Image.Image | None = None,
    focus_note: str = "",
) -> str:
    content = [
        {"type": "text", "text": "아래는 현장 전체 사진입니다."},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": image_to_jpeg_b64(full_img),
            },
        },
    ]

    if focus_img is not None:
        content.extend(
            [
                {
                    "type": "text",
                    "text": (
                        "아래는 점검자가 위험요인으로 지정한 포커스(네모) 영역입니다. "
                        "이 영역을 최우선으로 분석하고, 전체 사진 맥락은 보조로만 사용하세요."
                    ),
                },
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": image_to_jpeg_b64(focus_img),
                    },
                },
            ]
        )

    focus_line = focus_note.strip() or "없음"
    prompt = f"""당신은 대한민국 산업안전보건법 전문가입니다.
회사 현장 안전점검을 위해 사진을 분석하세요.

점검자 정보:
- 성명: {user['name']}
- 소속: {user['department']}
- 직책: {user['role']}
- 사업장/위치: {user['site']}
- 작업내용: {user['work_type'] or '미입력'}
- 포커스 영역 사용: {"예" if focus_img is not None else "아니오"}
- 점검자 메모(포커스 설명): {focus_line}

분석 규칙:
- 포커스 이미지가 있으면 그 구간의 위험요인을 중심으로 구체적으로 적으세요.
- 포커스 밖이라도 명백히 위험한 것은 추가로 짧게 언급하세요.
- 추측은 줄이고, 사진에서 보이는 근거를 먼저 적으세요.

아래 형식으로 한국어로 작성하세요.

1) 종합 위험등급: 상 / 중 / 하 중 하나만
2) 표: | 위험요소 | 관찰 근거(사진) | 관련 법령(조항) | 즉시조치 | 개선대책 | 담당 권장 직책 |
3) 근로자가 바로 할 수 있는 행동 3가지
4) 관리감독자가 확인해야 할 사항 3가지
"""
    content.append({"type": "text", "text": prompt})

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1500,
        messages=[{"role": "user", "content": content}],
    )
    return response.content[0].text


def extract_risk_level(text: str) -> str:
    for level in ("상", "중", "하"):
        if f"위험등급: {level}" in text or f"위험등급 : {level}" in text:
            return level
        if f"종합 위험등급: {level}" in text or f"종합 위험등급 : {level}" in text:
            return level
    if "위험등급" in text:
        idx = text.find("위험등급")
        snippet = text[idx : idx + 20]
        for level in ("상", "중", "하"):
            if level in snippet:
                return level
    return "미분류"


st.sidebar.header("회사 사용자 정보")
api_key = st.sidebar.text_input("Anthropic API Key", type="password")
name = st.sidebar.text_input("성명 *")
department = st.sidebar.text_input("소속(부서/팀) *")
role = st.sidebar.selectbox("직책/역할 *", ROLES)
site = st.sidebar.text_input("사업장/위치 *", placeholder="예: 1공장 용접구역")
work_type = st.sidebar.text_input("작업내용", placeholder="예: 고소작업, 지게차 하역")

st.sidebar.markdown("---")
st.sidebar.caption("기록이 DB에 저장되고, 엑셀로 내려받을 수 있습니다.")

if not api_key:
    st.warning("왼쪽 사이드바에 Anthropic API Key를 입력하세요.")
    st.stop()

client = Anthropic(api_key=api_key)
conn = get_conn()

st.title("🦺 산업안전 AI 점검 시스템")
st.caption("위험 구간에 네모 포커스를 지정하면, AI가 그 부분을 중심으로 분석합니다.")

tab_inspect, tab_records, tab_excel = st.tabs(["① 현장 점검", "② 점검 기록", "③ 엑셀 다운로드"])

with tab_inspect:
    missing = [
        label
        for label, val in [("성명", name), ("소속", department), ("사업장/위치", site)]
        if not val.strip()
    ]
    if missing:
        st.info(f"점검을 시작하려면 사이드바에 다음을 입력하세요: {', '.join(missing)}")

    source = st.radio("사진 입력 방식", ["카메라 촬영", "파일 업로드"], horizontal=True)
    if source == "카메라 촬영":
        photo = st.camera_input("현장 사진 촬영")
    else:
        photo = st.file_uploader("현장 사진 업로드", type=["jpg", "jpeg", "png", "webp"])

    focus_img = None
    focus_note = ""

    if photo:
        full_img = load_pil(photo)
        pkey = photo_key(photo)

        st.subheader("위험 구간 포커스")
        use_focus = st.checkbox("포커스(네모)로 위험 구간 지정", value=True, key=f"use_focus_{pkey}")

        focus_mode = "cropper"
        if use_focus:
            if HAS_CROPPER:
                mode = st.radio(
                    "포커스 방식",
                    ["드래그 네모 (권장)", "슬라이더 (대체)"],
                    horizontal=True,
                    key=f"focus_mode_{pkey}",
                )
                focus_mode = "cropper" if mode.startswith("드래그") else "slider"
            else:
                st.warning("드래그 네모 패키지가 없어 슬라이더 방식으로 동작합니다. requirements에 streamlit-cropper를 넣어 재배포하세요.")
                focus_mode = "slider"

            if focus_mode == "cropper":
                st.caption("사진 위 네모 모서리를 드래그해서 위험 구간만 남기세요.")
                cropped, box = focus_by_cropper(full_img, pkey)
            else:
                cropped, box = focus_by_sliders(full_img, pkey)

            if is_meaningful_focus(full_img, cropped):
                focus_img = cropped
            else:
                focus_img = None
                if use_focus and cropped is not None:
                    st.info("선택 영역이 거의 전체와 같습니다. 더 작게 줄이면 포커스 분석이 켜집니다.")

            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**전체 + 포커스 표시**")
                if box:
                    st.image(draw_focus_overlay(full_img, box), use_container_width=True)
                else:
                    st.image(full_img, use_container_width=True)
            with c2:
                st.markdown("**잘린 포커스 영역**")
                if focus_img is not None:
                    st.image(focus_img, use_container_width=True)
                    st.success("포커스 적용됨 → 이 구간을 우선 분석합니다")
                else:
                    st.image(full_img, use_container_width=True)
                    st.caption("포커스 미적용 · 전체 사진 기준으로 분석합니다")

            focus_note = st.text_input(
                "포커스 설명(선택)",
                placeholder="예: 안전난간 미설치 / 전선 피복 손상 / PPE 미착용",
                key=f"focus_note_{pkey}",
            )
        else:
            st.image(full_img, use_container_width=True)

        can_run = not missing
        if st.button("위험요소 분석 및 저장", type="primary", disabled=not can_run):
            with st.spinner("분석 중..."):
                user = {
                    "name": name.strip(),
                    "department": department.strip(),
                    "role": role,
                    "site": site.strip(),
                    "work_type": work_type.strip(),
                }
                analysis = analyze_image(
                    client,
                    full_img,
                    user,
                    focus_img=focus_img,
                    focus_note=focus_note,
                )
                risk_level = extract_risk_level(analysis)
                created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    """
                    INSERT INTO inspections
                    (created_at, name, department, role, site, work_type, risk_level, analysis, focus_used)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        created_at,
                        user["name"],
                        user["department"],
                        user["role"],
                        user["site"],
                        user["work_type"],
                        risk_level,
                        analysis,
                        1 if focus_img is not None else 0,
                    ),
                )
                conn.commit()
                st.session_state["last_analysis"] = analysis
                st.session_state["last_risk"] = risk_level
                st.success(
                    f"저장 완료 · 위험등급: {risk_level} · 포커스: {'사용' if focus_img is not None else '미사용'}"
                )

    if st.session_state.get("last_analysis"):
        st.subheader("최근 분석 결과")
        st.markdown(st.session_state["last_analysis"])

with tab_records:
    df = load_records(conn)
    st.metric("누적 점검 건수", len(df))

    c1, c2, c3 = st.columns(3)
    with c1:
        role_filter = st.multiselect("직책 필터", ROLES)
    with c2:
        risk_filter = st.multiselect("위험등급 필터", ["상", "중", "하", "미분류"])
    with c3:
        keyword = st.text_input("검색(성명/소속/위치)")

    view = df.copy()
    if role_filter:
        view = view[view["직책"].isin(role_filter)]
    if risk_filter:
        view = view[view["위험등급"].isin(risk_filter)]
    if keyword.strip():
        q = keyword.strip()
        mask = (
            view["성명"].astype(str).str.contains(q, na=False)
            | view["소속"].astype(str).str.contains(q, na=False)
            | view["사업장_위치"].astype(str).str.contains(q, na=False)
        )
        view = view[mask]

    if view.empty:
        st.info("아직 저장된 점검 기록이 없습니다. ① 현장 점검 탭에서 분석을 저장하세요.")
    else:
        show_cols = ["번호", "점검일시", "성명", "소속", "직책", "사업장_위치", "작업내용", "위험등급", "포커스사용"]
        st.dataframe(view[show_cols], use_container_width=True, hide_index=True)
        selected = st.selectbox(
            "상세 보기 (번호 선택)",
            options=view["번호"].tolist(),
            format_func=lambda i: (
                f"#{i} · {view.loc[view['번호'] == i, '점검일시'].values[0]} · "
                f"{view.loc[view['번호'] == i, '성명'].values[0]}"
            ),
        )
        detail = view[view["번호"] == selected].iloc[0]
        st.markdown(
            f"**{detail['성명']}** ({detail['직책']}) · {detail['소속']} · "
            f"{detail['사업장_위치']} · 포커스 {detail['포커스사용']}"
        )
        st.markdown(detail["분석결과"])

with tab_excel:
    df = load_records(conn)
    st.write("회사 안전점검 데이터를 엑셀로 내려받아 공유·보관할 수 있습니다.")

    if df.empty:
        st.info("내려받을 데이터가 없습니다.")
    else:
        show_cols = ["번호", "점검일시", "성명", "소속", "직책", "사업장_위치", "작업내용", "위험등급", "포커스사용"]
        st.dataframe(df[show_cols], use_container_width=True, hide_index=True)
        excel_bytes = to_excel_bytes(df)
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        st.download_button(
            label="📥 전체 점검기록 엑셀 다운로드",
            data=excel_bytes,
            file_name=f"안전점검기록_{stamp}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
        )
        st.caption("엑셀 시트명: 안전점검기록 · 분석결과 전문 포함")

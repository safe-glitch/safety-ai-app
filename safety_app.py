import base64
import hashlib
import io
import json
import re
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
PROMPT_VERSION = "1.1"
MODEL = "claude-opus-4-5"

DISCLAIMER = (
    "본 분석은 AI 참고용이며, 최종 판단·조치는 자격을 갖춘 안전관리자가 수행합니다. "
    "법령·규정 관련 내용은 **미검증 추정**이며, 반드시 원문·전문가 확인이 필요합니다."
)

st.set_page_config(page_title="산업안전 AI 점검", layout="wide", page_icon="🦺")


def get_api_key() -> str | None:
    try:
        key = st.secrets["ANTHROPIC_API_KEY"]
        return key.strip() if key else None
    except (KeyError, FileNotFoundError, AttributeError):
        return None


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
            focus_used INTEGER DEFAULT 0,
            prompt_version TEXT
        )
        """
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(inspections)")}
    if "focus_used" not in cols:
        conn.execute("ALTER TABLE inspections ADD COLUMN focus_used INTEGER DEFAULT 0")
    if "prompt_version" not in cols:
        conn.execute("ALTER TABLE inspections ADD COLUMN prompt_version TEXT")
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
            prompt_version AS 분석버전,
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


def parse_json_response(text: str) -> dict | None:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def validate_analysis(data: dict) -> bool:
    if data.get("risk_level") not in ("상", "중", "하"):
        return False
    if not isinstance(data.get("hazards"), list) or not data["hazards"]:
        return False
    return True


def normalize_risk_level(value: str | None) -> str:
    if value in ("상", "중", "하"):
        return value
    return "미분류"


def format_analysis_markdown(data: dict) -> str:
    risk = normalize_risk_level(data.get("risk_level"))
    lines = [
        f"### 종합 위험등급: **{risk}**",
        "",
        data.get("summary", "").strip(),
        "",
        "| 위험요소 | 관찰 근거 | 관련 법령 분야(미검증) | 즉시조치 | 개선대책 | 담당 권장 |",
        "|---|---|---|---|---|---|",
    ]
    for h in data.get("hazards", []):
        lines.append(
            "| {hazard} | {evidence} | {law_area} | {immediate} | {improve} | {role} |".format(
                hazard=h.get("hazard", ""),
                evidence=h.get("evidence", ""),
                law_area=h.get("law_area", ""),
                immediate=h.get("immediate_action", ""),
                improve=h.get("improvement", ""),
                role=h.get("responsible_role", ""),
            )
        )

    lines.extend(["", "#### 근로자 즉시 행동"])
    for i, action in enumerate(data.get("worker_actions", []), 1):
        lines.append(f"{i}. {action}")

    lines.extend(["", "#### 관리감독자 확인 사항"])
    for i, check in enumerate(data.get("supervisor_checks", []), 1):
        lines.append(f"{i}. {check}")

    lines.extend(
        [
            "",
            "---",
            "*법령 분야는 AI 추정이며 조항 번호를 인용하지 않았습니다. 안전관리자가 원문을 확인하세요.*",
        ]
    )
    return "\n".join(lines)


def build_analysis_prompt(user: dict, focus_img: Image.Image | None, focus_note: str) -> str:
    focus_line = focus_note.strip() or "없음"
    return f"""당신은 대한민국 산업안전 현장 점검 보조 AI입니다.
반드시 아래 JSON 형식만 출력하세요. JSON 앞뒤에 다른 문장을 쓰지 마세요.

점검자 정보:
- 성명: {user['name']}
- 소속: {user['department']}
- 직책: {user['role']}
- 사업장/위치: {user['site']}
- 작업내용: {user['work_type'] or '미입력'}
- 포커스 영역 사용: {"예" if focus_img is not None else "아니오"}
- 점검자 메모: {focus_line}

분석 규칙:
- 포커스 이미지가 있으면 그 구간을 최우선 분석하세요.
- 사진에서 보이는 근거만 적고 추측은 최소화하세요.
- **법령 조항 번호(제00조 등)는 절대 쓰지 마세요.**
- law_area에는 "보호구 착용", "고소작업 안전", "전기안전"처럼 **분야명만** 적고 (미검증)임을 전제로 하세요.

JSON 스키마:
{{
  "risk_level": "상 또는 중 또는 하",
  "summary": "2~3문장 요약",
  "hazards": [
    {{
      "hazard": "위험요소",
      "evidence": "사진에서 본 근거",
      "law_area": "관련 법령 분야(미검증, 조항번호 금지)",
      "immediate_action": "즉시조치",
      "improvement": "개선대책",
      "responsible_role": "담당 권장 직책"
    }}
  ],
  "worker_actions": ["행동1", "행동2", "행동3"],
  "supervisor_checks": ["확인1", "확인2", "확인3"]
}}"""


def analyze_image(
    client: Anthropic,
    full_img: Image.Image,
    user: dict,
    focus_img: Image.Image | None = None,
    focus_note: str = "",
) -> dict:
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
                        "아래는 점검자가 지정한 포커스(위험) 영역입니다. "
                        "이 영역을 최우선으로 분석하세요."
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

    prompt = build_analysis_prompt(user, focus_img, focus_note)
    content.append({"type": "text", "text": prompt})

    last_raw = ""
    for attempt in range(2):
        response = client.messages.create(
            model=MODEL,
            max_tokens=1800,
            messages=[{"role": "user", "content": content}],
        )
        last_raw = response.content[0].text
        parsed = parse_json_response(last_raw)
        if parsed and validate_analysis(parsed):
            risk_level = normalize_risk_level(parsed.get("risk_level"))
            return {
                "risk_level": risk_level,
                "analysis": format_analysis_markdown(parsed),
                "parse_ok": True,
            }
        content.append(
            {
                "type": "text",
                "text": (
                    "이전 응답이 JSON 스키마를 따르지 않았습니다. "
                    "risk_level은 상/중/하 중 하나, hazards는 1개 이상. "
                    "JSON만 다시 출력하세요."
                ),
            }
        )

    return {
        "risk_level": "미분류",
        "analysis": (
            "### 분석 결과 (형식 오류 — 수동 확인 필요)\n\n"
            f"{last_raw}\n\n"
            "---\n*AI 응답을 구조화하지 못했습니다. 안전관리자가 직접 검토하세요.*"
        ),
        "parse_ok": False,
    }


# ----- API Key (Secrets only) -----
api_key = get_api_key()
if not api_key:
    st.error("Anthropic API Key가 설정되지 않았습니다.")
    st.markdown(
        """
**Streamlit Cloud** → 앱 **Settings** → **Secrets**에 아래를 추가하세요:

```toml
ANTHROPIC_API_KEY = "sk-ant-..."
```

로컬 실행 시: `.streamlit/secrets.toml` 파일에 동일하게 넣으세요.
        """
    )
    st.stop()

client = Anthropic(api_key=api_key)
conn = get_conn()

# ----- Sidebar -----
st.sidebar.header("점검자 정보")
name = st.sidebar.text_input("성명 *")
department = st.sidebar.text_input("소속(부서/팀) *")
role = st.sidebar.selectbox("직책/역할 *", ROLES)
site = st.sidebar.text_input("사업장/위치 *", placeholder="예: 1공장 용접구역")
work_type = st.sidebar.text_input("작업내용", placeholder="예: 고소작업, 지게차 하역")

st.sidebar.markdown("---")
st.sidebar.caption("기록 저장 · 엑셀 다운로드 · 분석버전 " + PROMPT_VERSION)

st.title("🦺 산업안전 AI 점검 시스템")
st.caption("위험 구간 포커스 → AI 참고 분석 → 기록 저장 (1인 개발 프로토타입)")

st.info(DISCLAIMER)

tab_inspect, tab_records, tab_excel = st.tabs(["① 현장 점검", "② 점검 기록", "③ 엑셀 다운로드"])

# ----- Tab 1 -----
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
                st.warning("streamlit-cropper 미설치 → 슬라이더 방식 사용")
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
                if cropped is not None:
                    st.info("선택 영역이 거의 전체입니다. 더 작게 줄이면 포커스 분석이 켜집니다.")

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
                    st.success("포커스 적용됨")
                else:
                    st.image(full_img, use_container_width=True)
                    st.caption("포커스 미적용")

            focus_note = st.text_input(
                "포커스 설명(선택)",
                placeholder="예: 안전난간 미설치 / 전선 피복 손상",
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
                result = analyze_image(
                    client,
                    full_img,
                    user,
                    focus_img=focus_img,
                    focus_note=focus_note,
                )
                risk_level = result["risk_level"]
                analysis = result["analysis"]
                created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    """
                    INSERT INTO inspections
                    (created_at, name, department, role, site, work_type, risk_level, analysis, focus_used, prompt_version)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        PROMPT_VERSION,
                    ),
                )
                conn.commit()
                st.session_state["last_analysis"] = analysis
                st.session_state["last_risk"] = risk_level
                msg = f"저장 완료 · 위험등급: {risk_level} · 포커스: {'사용' if focus_img else '미사용'}"
                if not result["parse_ok"]:
                    msg += " · ⚠️ 형식 오류(수동 확인)"
                st.success(msg)

    if st.session_state.get("last_analysis"):
        st.subheader("최근 분석 결과")
        st.markdown(st.session_state["last_analysis"])

# ----- Tab 2 -----
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
        st.info("아직 저장된 점검 기록이 없습니다.")
    else:
        show_cols = [
            "번호", "점검일시", "성명", "소속", "직책",
            "사업장_위치", "작업내용", "위험등급", "포커스사용", "분석버전",
        ]
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
            f"{detail['사업장_위치']} · 위험 {detail['위험등급']}"
        )
        st.markdown(detail["분석결과"])

# ----- Tab 3 -----
with tab_excel:
    df = load_records(conn)
    st.write("점검 데이터를 엑셀로 내려받을 수 있습니다.")
    st.caption(DISCLAIMER)

    if df.empty:
        st.info("내려받을 데이터가 없습니다.")
    else:
        show_cols = [
            "번호", "점검일시", "성명", "소속", "직책",
            "사업장_위치", "작업내용", "위험등급", "포커스사용", "분석버전",
        ]
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

import streamlit as st
from anthropic import Anthropic
import base64

st.set_page_config(page_title="안전점검 AI", layout="centered")
st.title("🚨 산업안전 AI 점검기")

api_key = st.sidebar.text_input("Anthropic API Key", type="password")
if not api_key:
    st.warning("왼쪽 사이드바에 API 키를 입력하세요.")
    st.stop()

client = Anthropic(api_key=api_key)

photo = st.camera_input("현장 사진 촬영")

if photo:
    st.image(photo, use_container_width=True)
    if st.button("위험요소 분석"):
        with st.spinner("분석 중..."):
            img_b64 = base64.b64encode(photo.getvalue()).decode()
            response = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=1024,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                        {"type": "text", "text": "대한민국 산업안전보건법 전문가로서 이 사진의 위험요소, 관련 법령 조항, 개선대책을 표 형식으로 분석해줘."}
                    ]
                }]
            )
            st.markdown(response.content[0].text)

import os
import json
import streamlit as st
import streamlit.components.v1 as components
import requests

BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000")

st.set_page_config(
    page_title="Wedding Photo Finder",
    page_icon="📸",
    layout="wide",
)


st.title("📸 Find Your Wedding Photos")
st.write("Take a selfie and we'll find every photo you appear in from the gallery.")

# Session state: hold selfie + results across reruns so camera can be hidden
if "selfie" not in st.session_state:
    st.session_state.selfie = None
if "results" not in st.session_state:
    st.session_state.results = None

# Show camera only until a selfie is captured
if st.session_state.selfie is None:
    st.info("🔒 Your selfie is processed instantly and is never stored on our servers.")
    img_file = st.camera_input("Smile for the camera!")
    if img_file:
        st.session_state.selfie = img_file
        st.rerun()
    st.stop()

# --- Selfie captured — camera is now hidden ---
img_file = st.session_state.selfie

# Run search once, cache result in session state
if st.session_state.results is None:
    with st.spinner("Scanning the gallery — this usually takes 10–20 seconds…"):
        files = {"file": ("selfie.jpg", img_file.getvalue(), "image/jpeg")}
        try:
            r = requests.post(f"{BACKEND_URL}/find-me", files=files, timeout=60)
        except requests.exceptions.ConnectionError:
            st.error("Could not reach the server. Please try again in a moment.")
            if st.button("📷 Try Again"):
                st.session_state.selfie = None
                st.session_state.results = None
                st.rerun()
            st.stop()
        except requests.exceptions.Timeout:
            st.error("The scan timed out. Please try again.")
            if st.button("📷 Try Again"):
                st.session_state.selfie = None
                st.session_state.results = None
                st.rerun()
            st.stop()

        if r.status_code != 200:
            st.error("Something went wrong on our end. Please try again in a moment.")
            if st.button("📷 Try Again"):
                st.session_state.selfie = None
                st.session_state.results = None
                st.rerun()
            st.stop()

        st.session_state.results = r.json()

data = st.session_state.results
drive_ids = data.get("drive_ids", [])

if not drive_ids:
    st.warning("No matches found. Try again with better lighting or a clearer angle.")
    if st.button("📷 Try Again"):
        st.session_state.selfie = None
        st.session_state.results = None
        st.rerun()
else:
    PER_PAGE = 24
    total = len(drive_ids)
    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)

    if "page" not in st.session_state:
        st.session_state.page = 0

    st.success(f"🎉 Found {total} photos of you!")

    col1, col2 = st.columns([2, 1])
    with col1:
        if st.button("📷 Try Again"):
            st.session_state.selfie = None
            st.session_state.results = None
            st.session_state.page = 0
            st.rerun()

    # Download All button (needs JS — small iframe, no images inside)
    download_urls = [f"https://drive.google.com/uc?export=download&id={did}" for did in drive_ids]
    urls_json = json.dumps(download_urls)
    components.html(f"""
        <!DOCTYPE html><html><head>
        <style>
          body {{ margin:0; padding:4px 0; font-family:sans-serif; background:transparent; }}
          #dl-all {{ background:#1d4ed8; color:white; border:none;
            padding:10px 20px; border-radius:7px; font-size:14px;
            font-weight:600; cursor:pointer; }}
          #dl-all:hover {{ background:#1e40af; }}
          #dl-note {{ color:#6b7280; font-size:12px; margin-top:5px; }}
        </style></head><body>
        <button id="dl-all" onclick="downloadAll()">⬇ Download All ({total} photos)</button>
        <p id="dl-note">Allow multiple downloads if browser asks.</p>
        <script>
        function downloadAll() {{
          var urls = {urls_json};
          urls.forEach(function(url, i) {{
            setTimeout(function() {{
              var a = document.createElement('a');
              a.href = url; a.download = 'photo_'+(i+1)+'.jpg';
              a.target = '_blank';
              document.body.appendChild(a); a.click(); document.body.removeChild(a);
            }}, i * 400);
          }});
        }}
        </script></body></html>
    """, height=80)

    # Pagination controls
    page = st.session_state.page
    start = page * PER_PAGE
    end = min(start + PER_PAGE, total)
    page_ids = drive_ids[start:end]

    st.caption(f"Page {page + 1} of {total_pages} · photos {start + 1}–{end} of {total}")

    # Photo grid via st.markdown — images load correctly here (not sandboxed)
    st.markdown("""
    <style>
    .photo-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
        gap: 14px; margin: 10px 0 20px 0;
    }
    .photo-card {
        border-radius: 10px; overflow: hidden;
        background: #111827; box-shadow: 0 2px 10px rgba(0,0,0,0.25);
    }
    .photo-card img { width:100%; height:190px; object-fit:cover; display:block; }
    .dl-btn {
        display:block; text-align:center; padding:9px;
        background:#1d4ed8; color:white; text-decoration:none;
        font-size:13px; font-weight:600;
    }
    .dl-btn:hover { background:#1e40af; }
    </style>
    """, unsafe_allow_html=True)

    cards_html = ""
    for did in page_ids:
        thumb = f"https://drive.google.com/thumbnail?id={did}&sz=w400"
        download = f"https://drive.google.com/uc?export=download&id={did}"
        cards_html += f"""
        <div class="photo-card">
            <a href="{download}" target="_blank">
                <img src="{thumb}" loading="lazy" alt="photo">
            </a>
            <a class="dl-btn" href="{download}" target="_blank">⬇ Download</a>
        </div>"""

    st.markdown(f'<div class="photo-grid">{cards_html}</div>', unsafe_allow_html=True)

    # Prev / Next
    c1, c2, c3 = st.columns([1, 2, 1])
    with c1:
        if st.button("← Prev", disabled=(page == 0)):
            st.session_state.page -= 1
            st.rerun()
    with c3:
        if st.button("Next →", disabled=(page >= total_pages - 1)):
            st.session_state.page += 1
            st.rerun()

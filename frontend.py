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

st.markdown("""
<style>
/* Responsive photo grid */
.photo-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 14px;
    margin-top: 20px;
}
.photo-card {
    border-radius: 10px;
    overflow: hidden;
    background: #111827;
    box-shadow: 0 2px 10px rgba(0,0,0,0.25);
    transition: transform 0.15s;
}
.photo-card:hover { transform: scale(1.02); }
.photo-card img {
    width: 100%;
    height: 200px;
    object-fit: cover;
    display: block;
}
.photo-card a.dl-btn {
    display: block;
    text-align: center;
    padding: 9px;
    background: #1d4ed8;
    color: white;
    text-decoration: none;
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.02em;
}
.photo-card a.dl-btn:hover { background: #1e40af; }
</style>
""", unsafe_allow_html=True)

st.title("📸 Find Your Wedding Photos")
st.write("Take a selfie and we'll find every photo you appear in from the gallery.")

# Privacy notice BEFORE the camera activates
st.info("🔒 Your selfie is processed instantly and is never stored on our servers.")

img_file = st.camera_input("Smile for the camera!")

if img_file:
    with st.spinner("Scanning the gallery — this usually takes 10–20 seconds…"):
        files = {"file": ("selfie.jpg", img_file.getvalue(), "image/jpeg")}
        try:
            r = requests.post(f"{BACKEND_URL}/find-me", files=files, timeout=60)
        except requests.exceptions.ConnectionError:
            st.error("Could not reach the server. Please try again in a moment.")
            st.stop()
        except requests.exceptions.Timeout:
            st.error("The scan timed out. Please try again.")
            st.stop()

    if r.status_code != 200:
        st.error("Something went wrong on our end. Please try again in a moment.")
        st.stop()

    data = r.json()
    drive_ids = data.get("drive_ids", [])

    if not drive_ids:
        st.warning("No matches found. Try again with better lighting or a clearer angle.")
        if st.button("📷 Try Again"):
            st.rerun()
    else:
        st.success(f"🎉 Found {len(drive_ids)} photos of you!")

        # --- Download All (JavaScript, staggered to avoid popup blockers) ---
        download_urls = [
            f"https://drive.google.com/uc?export=download&id={did}"
            for did in drive_ids
        ]
        urls_json = json.dumps(download_urls)
        components.html(f"""
            <style>
              #dl-all {{
                background: #1d4ed8; color: white; border: none;
                padding: 11px 22px; border-radius: 7px; font-size: 14px;
                font-weight: 600; cursor: pointer; font-family: sans-serif;
              }}
              #dl-all:hover {{ background: #1e40af; }}
              #dl-note {{ color: #6b7280; font-size: 12px; margin-top: 6px; font-family: sans-serif; }}
            </style>
            <button id="dl-all" onclick="downloadAll()">
              ⬇ Download All ({len(drive_ids)} photos)
            </button>
            <p id="dl-note">
              If your browser asks to allow multiple downloads, click Allow.
            </p>
            <script>
            function downloadAll() {{
              var urls = {urls_json};
              urls.forEach(function(url, i) {{
                setTimeout(function() {{
                  var a = document.createElement('a');
                  a.href = url;
                  a.download = 'photo_' + (i + 1) + '.jpg';
                  a.target = '_blank';
                  document.body.appendChild(a);
                  a.click();
                  document.body.removeChild(a);
                }}, i * 400);
              }});
            }}
            </script>
        """, height=80)

        # --- Thumbnail grid — all photos, lazy-loaded ---
        # Thumbnails: Google Drive thumbnail API (requires folder to be publicly viewable)
        # Download: direct file download URL
        cards_html = ""
        for did in drive_ids:
            thumb = f"https://drive.google.com/thumbnail?id={did}&sz=w400"
            download = f"https://drive.google.com/uc?export=download&id={did}"
            cards_html += f"""
            <div class="photo-card">
                <a href="{download}" target="_blank">
                    <img src="{thumb}" loading="lazy" alt="Your photo">
                </a>
                <a class="dl-btn" href="{download}" target="_blank">⬇ Download</a>
            </div>
            """

        st.markdown(
            f'<div class="photo-grid">{cards_html}</div>',
            unsafe_allow_html=True,
        )

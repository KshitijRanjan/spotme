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

        # Build cards
        cards_html = ""
        download_urls = []
        for did in drive_ids:
            thumb = f"https://drive.google.com/thumbnail?id={did}&sz=w400"
            download = f"https://drive.google.com/uc?export=download&id={did}"
            download_urls.append(download)
            cards_html += f"""
            <div class="photo-card">
                <a href="{download}" target="_blank">
                    <img src="{thumb}" loading="lazy" alt="Your photo">
                </a>
                <a class="dl-btn" href="{download}" target="_blank">⬇ Download</a>
            </div>
            """

        urls_json = json.dumps(download_urls)
        PER_PAGE = 24

        components.html(f"""
            <!DOCTYPE html>
            <html>
            <head>
            <style>
              body {{ margin: 0; padding: 0; font-family: sans-serif; background: transparent; }}
              #dl-wrap {{ margin-bottom: 16px; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
              #dl-all {{
                background: #1d4ed8; color: white; border: none;
                padding: 11px 22px; border-radius: 7px; font-size: 14px;
                font-weight: 600; cursor: pointer;
              }}
              #dl-all:hover {{ background: #1e40af; }}
              #dl-note {{ color: #6b7280; font-size: 12px; }}
              .photo-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
                gap: 14px;
                margin-bottom: 20px;
              }}
              .photo-card {{
                border-radius: 10px; overflow: hidden;
                background: #111827;
                box-shadow: 0 2px 10px rgba(0,0,0,0.25);
              }}
              .photo-card img {{
                width: 100%; height: 190px;
                object-fit: cover; display: block;
              }}
              .dl-btn {{
                display: block; text-align: center; padding: 9px;
                background: #1d4ed8; color: white;
                text-decoration: none; font-size: 13px; font-weight: 600;
              }}
              .dl-btn:hover {{ background: #1e40af; }}
              #pagination {{
                display: flex; align-items: center; gap: 10px;
                justify-content: center; padding: 10px 0;
              }}
              .pg-btn {{
                background: #1d4ed8; color: white; border: none;
                padding: 8px 18px; border-radius: 6px; font-size: 13px;
                font-weight: 600; cursor: pointer;
              }}
              .pg-btn:disabled {{ background: #374151; cursor: default; }}
              .pg-btn:not(:disabled):hover {{ background: #1e40af; }}
              #pg-info {{ color: #6b7280; font-size: 13px; }}
            </style>
            </head>
            <body>
              <div id="dl-wrap">
                <button id="dl-all" onclick="downloadAll()">
                  ⬇ Download All ({len(drive_ids)} photos)
                </button>
                <span id="dl-note">Allow multiple downloads if browser asks.</span>
              </div>
              <div class="photo-grid" id="grid"></div>
              <div id="pagination">
                <button class="pg-btn" id="prev-btn" onclick="changePage(-1)" disabled>← Prev</button>
                <span id="pg-info"></span>
                <button class="pg-btn" id="next-btn" onclick="changePage(1)">Next →</button>
              </div>
              <script>
              var photos = {cards_html!r};
              var allUrls = {urls_json};
              var perPage = {PER_PAGE};
              var total = {len(drive_ids)};
              var totalPages = Math.ceil(total / perPage);
              var page = 0;

              // Pre-build card HTML array from drive ids
              var thumbs = {json.dumps([f"https://drive.google.com/thumbnail?id={{did}}&sz=w400" for did in drive_ids])};
              var downloads = {json.dumps([f"https://drive.google.com/uc?export=download&id={{did}}" for did in drive_ids])};

              function render() {{
                var start = page * perPage;
                var end = Math.min(start + perPage, total);
                var html = '';
                for (var i = start; i < end; i++) {{
                  html += '<div class="photo-card">'
                    + '<a href="' + downloads[i] + '" target="_blank">'
                    + '<img src="' + thumbs[i] + '" loading="lazy" alt="photo">'
                    + '</a>'
                    + '<a class="dl-btn" href="' + downloads[i] + '" target="_blank">⬇ Download</a>'
                    + '</div>';
                }}
                document.getElementById('grid').innerHTML = html;
                document.getElementById('pg-info').textContent =
                  'Page ' + (page+1) + ' of ' + totalPages + ' · showing ' + start + '–' + (end-1);
                document.getElementById('prev-btn').disabled = page === 0;
                document.getElementById('next-btn').disabled = page === totalPages - 1;
              }}

              function changePage(dir) {{
                page = Math.max(0, Math.min(totalPages - 1, page + dir));
                render();
                window.scrollTo(0, 0);
              }}

              function downloadAll() {{
                allUrls.forEach(function(url, i) {{
                  setTimeout(function() {{
                    var a = document.createElement('a');
                    a.href = url; a.download = 'photo_' + (i+1) + '.jpg';
                    a.target = '_blank';
                    document.body.appendChild(a); a.click();
                    document.body.removeChild(a);
                  }}, i * 400);
                }});
              }}

              render();
              </script>
            </body>
            </html>
        """, height=1400, scrolling=False)

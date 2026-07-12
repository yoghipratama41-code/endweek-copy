import time
import random
import re
import io

import streamlit as st
import google.generativeai as genai
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from PIL import Image

# ============== KONFIGURASI ==============
st.set_page_config(page_title="GNS Slide Automation", page_icon="📊", layout="centered")

TEMPLATE_PRESENTATION_ID = st.secrets["TEMPLATE_PRESENTATION_ID"]
GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
GOOGLE_CLIENT_ID = st.secrets["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = st.secrets["GOOGLE_CLIENT_SECRET"]
GOOGLE_REFRESH_TOKEN = st.secrets["GOOGLE_REFRESH_TOKEN"]

# ID Template Endweek yang baru saja Anda berikan
ENDWEEK_TEMPLATE_ID = "1PvaGfcS1dBMcW-48HQWLEXT3irKPFi9Ptm5eqloX9QA"

SCOPES = (
    "https://www.googleapis.com/auth/drive.file "
    "https://www.googleapis.com/auth/drive.readonly "
    "https://www.googleapis.com/auth/presentations"
)

TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

MODEL_PRIORITY = [
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-3-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
]

MAX_RETRY_PER_MODEL = 3


# ============== HELPER: MODEL FALLBACK & AI ==============
def get_model_fallback_list():
    available = [m.name for m in genai.list_models() if "generateContent" in m.supported_generation_methods]
    ordered = []
    for key in MODEL_PRIORITY:
        match = next((m for m in available if key in m), None)
        if match and match not in ordered:
            ordered.append(match)
    return ordered

def analisis_dengan_model_fallback(model_names, prompt, gambar_list, status_box, max_retry_per_model=MAX_RETRY_PER_MODEL):
    last_err = None
    for model_name in model_names:
        model = genai.GenerativeModel(model_name)
        delay = 10
        nama_model_pendek = model_name.split("/")[-1]

        for attempt in range(max_retry_per_model):
            try:
                time.sleep(2)
                respon = model.generate_content([prompt] + gambar_list)
                return respon.text, nama_model_pendek
            except Exception as e:
                err_msg = str(e)
                last_err = e
                if "429" in err_msg or "503" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                    wait_time = delay + random.uniform(0, 5)
                    status_box.warning(
                        f"⚠️ Model **{nama_model_pendek}** kena limit/sibuk "
                        f"(percobaan {attempt + 1}/{max_retry_per_model}). Menunggu {wait_time:.1f}s..."
                    )
                    time.sleep(wait_time)
                    delay *= 2
                else:
                    status_box.warning(f"⚠️ Model **{nama_model_pendek}** error: {err_msg[:150]}")
                    break
        status_box.info(f"➡️ Pindah dari model **{nama_model_pendek}** ke model berikutnya...")
    raise Exception(f"Semua model gagal dicoba. Error terakhir: {last_err}")


# ============== HELPER: TEMPLATE DETECTION ==============
def _get_slide_text(slide):
    texts = []
    for el in slide.get("pageElements", []):
        shape = el.get("shape")
        if shape and "text" in shape:
            for te in shape["text"].get("textElements", []):
                run = te.get("textRun")
                if run:
                    texts.append(run.get("content", ""))
    return "".join(texts)

def cari_template_slide(presentation):
    id_main = None
    id_comment = None
    for slide in presentation.get("slides", []):
        txt = _get_slide_text(slide)
        if "{{IMG}}" in txt and id_main is None:
            id_main = slide["objectId"]
        if "{{CMT}}" in txt and id_comment is None:
            id_comment = slide["objectId"]
    return id_main, id_comment

def cari_template_slide_endweek(presentation):
    """
    Mendeteksi slide format 1 (Facebook Group) dan format 2 (Promotion)
    berdasarkan teks yang ada di dalamnya.
    """
    id_fb_main, id_fb_comment, id_promo_main = None, None, None
    for slide in presentation.get("slides", []):
        txt = _get_slide_text(slide).lower()
        if "{{img}}" in txt and "facebook group" in txt and id_fb_main is None:
            id_fb_main = slide["objectId"]
        if "{{cmt}}" in txt and "facebook group" in txt and id_fb_comment is None:
            id_fb_comment = slide["objectId"]
        if "{{img}}" in txt and "promotion" in txt and id_promo_main is None:
            id_promo_main = slide["objectId"]
    return id_fb_main, id_fb_comment, id_promo_main


# ============== HELPER: UPLOAD IMAGE KE DRIVE ==============
def upload_gambar_ke_drive(drive_service, uploaded_file):
    uploaded_file.seek(0)
    media = MediaIoBaseUpload(io.BytesIO(uploaded_file.read()), mimetype="image/jpeg")
    file_dr = drive_service.files().create(
        body={"name": uploaded_file.name}, media_body=media, fields="id, webContentLink"
    ).execute()
    drive_service.permissions().create(
        fileId=file_dr["id"], body={"type": "anyone", "role": "reader"}
    ).execute()
    return file_dr["webContentLink"]


# ============== AUTOMATION 1: MIDWEEK (AI GENERATION) ==============
def jalankan_otomatisasi_midweek(creds, news_items, progress_bar, status_box):
    drive_service = build("drive", "v3", credentials=creds)
    slides_service = build("slides", "v1", credentials=creds)
    genai.configure(api_key=GEMINI_API_KEY)

    model_fallback_list = get_model_fallback_list()
    
    nama_slide_baru = f"Final Midweek - {time.strftime('%Y-%m-%d %H:%M:%S')}"
    copy = drive_service.files().copy(
        fileId=TEMPLATE_PRESENTATION_ID, body={"name": nama_slide_baru}
    ).execute()
    id_slide_baru = copy.get("id")
    link_presentasi = f"https://docs.google.com/presentation/d/{id_slide_baru}/edit"

    presentation = slides_service.presentations().get(presentationId=id_slide_baru).execute()
    id_templat_main, id_templat_comment = cari_template_slide(presentation)

    slide_count = len(presentation.get("slides", []))
    jumlah = len(news_items)
    
    # Simpan hasil olahan AI untuk dipakai lagi di Endweek
    processed_data = [] 

    for index, item in enumerate(news_items):
        main_file = item["main"]
        comment_file = item.get("comment")

        try:
            status_box.info(f"[{index+1}/{jumlah}] Memproses Midweek: {main_file.name}...")

            main_file.seek(0)
            gambar_list = [Image.open(main_file)]
            if comment_file:
                comment_file.seek(0)
                gambar_list.append(Image.open(comment_file))

            prompt_ai = """
            Analyze this image (and comments if any) for a professional research slide.
            Write the analysis in one single cohesive paragraph in English.
            Strict Rules:
            1. Refer to both users and drivers ONLY as "rider".
            2. Do NOT mention any social media account names, usernames, or the image filename.
            3. The paragraph must consist of at least 3-4 sentences.
            Output Format:
            [TITLE] Write a short title (max 5 words).
            [CONTENT] Write the full paragraph here.
            """

            teks_raw, model_dipakai = analisis_dengan_model_fallback(
                model_fallback_list, prompt_ai, gambar_list, status_box
            )
            judul = teks_raw.split("[TITLE]")[1].split("[CONTENT]")[0].strip()
            full_para = teks_raw.split("[CONTENT]")[1].strip()
            sentences = re.split(r"(?<=[.!?]) +", full_para)

            # Ekstrak context dan list insight
            if len(sentences) > 1:
                konteks = sentences[0]
                insight_list = [s for s in sentences[1:] if s.strip()]
                insight_midweek = "\n".join(insight_list)
            else:
                konteks = full_para
                insight_list = []
                insight_midweek = "-"

            link_gambar_main = upload_gambar_ke_drive(drive_service, main_file)
            link_gambar_comment = upload_gambar_ke_drive(drive_service, comment_file) if comment_file else None

            # Simpan data ke list
            processed_data.append({
                "filename": main_file.name,
                "title": judul,
                "context": konteks,
                "insight_list": insight_list,
                "img_main": link_gambar_main,
                "img_cmt": link_gambar_comment
            })

            # --- BUAT SLIDE MIDWEEK MAIN ---
            res_dup = slides_service.presentations().batchUpdate(
                presentationId=id_slide_baru,
                body={"requests": [{"duplicateObject": {"objectId": id_templat_main}}]},
            ).execute()
            id_baru_main = res_dup["replies"][0]["duplicateObject"]["objectId"]

            req_main = [
                {"updateSlidesPosition": {"slideObjectIds": [id_baru_main], "insertionIndex": slide_count}},
                {"replaceAllText": {"containsText": {"text": "{{TITLE}}"}, "replaceText": judul, "pageObjectIds": [id_baru_main]}},
                {"replaceAllText": {"containsText": {"text": "{{CONTEXT}}"}, "replaceText": konteks, "pageObjectIds": [id_baru_main]}},
                {"replaceAllText": {"containsText": {"text": "{{INSIGHT}}"}, "replaceText": insight_midweek, "pageObjectIds": [id_baru_main]}},
                {"replaceAllShapesWithImage": {"imageUrl": link_gambar_main, "replaceMethod": "CENTER_INSIDE", "containsText": {"text": "{{IMG}}", "matchCase": True}, "pageObjectIds": [id_baru_main]}}
            ]
            slides_service.presentations().batchUpdate(presentationId=id_slide_baru, body={"requests": req_main}).execute()
            slide_count += 1

            # --- BUAT SLIDE MIDWEEK COMMENT ---
            if comment_file:
                res_dup_c = slides_service.presentations().batchUpdate(
                    presentationId=id_slide_baru,
                    body={"requests": [{"duplicateObject": {"objectId": id_templat_comment}}]},
                ).execute()
                id_baru_comment = res_dup_c["replies"][0]["duplicateObject"]["objectId"]

                req_cmt = [
                    {"updateSlidesPosition": {"slideObjectIds": [id_baru_comment], "insertionIndex": slide_count}},
                    {"replaceAllText": {"containsText": {"text": "{{TITLE}}"}, "replaceText": judul, "pageObjectIds": [id_baru_comment]}},
                    {"replaceAllShapesWithImage": {"imageUrl": link_gambar_comment, "replaceMethod": "CENTER_INSIDE", "containsText": {"text": "{{CMT}}", "matchCase": True}, "pageObjectIds": [id_baru_comment]}}
                ]
                slides_service.presentations().batchUpdate(presentationId=id_slide_baru, body={"requests": req_cmt}).execute()
                slide_count += 1

            if index < jumlah - 1:
                time.sleep(15)

        except Exception as e:
            status_box.error(f"❌ {main_file.name} dilewati: {e}")

        finally:
            progress_bar.progress((index + 1) / jumlah)

    return link_presentasi, processed_data


# ============== AUTOMATION 2: ENDWEEK (NO AI, MERGE INSIGHT) ==============
def jalankan_otomatisasi_endweek(creds, processed_data, selections, status_box):
    drive_service = build("drive", "v3", credentials=creds)
    slides_service = build("slides", "v1", credentials=creds)

    nama_slide_baru = f"Final Endweek - {time.strftime('%Y-%m-%d %H:%M:%S')}"
    copy = drive_service.files().copy(
        fileId=ENDWEEK_TEMPLATE_ID, body={"name": nama_slide_baru}
    ).execute()
    id_slide_baru = copy.get("id")
    link_presentasi = f"https://docs.google.com/presentation/d/{id_slide_baru}/edit"

    presentation = slides_service.presentations().get(presentationId=id_slide_baru).execute()
    id_fb_main, id_fb_comment, id_promo_main = cari_template_slide_endweek(presentation)

    if not id_fb_main or not id_promo_main:
        raise Exception("Template Endweek tidak lengkap! Pastikan ada slide ber-teks 'Facebook Group' & 'Promotion'.")

    slide_count = len(presentation.get("slides", []))

    for item in processed_data:
        fname = item["filename"]
        format_pilihan = selections[fname]
        
        status_box.info(f"Memproses Endweek: {fname} sebagai {format_pilihan}...")

        judul = item["title"]
        konteks = item["context"]
        img_main = item["img_main"]
        img_cmt = item["img_cmt"]
        
        # PERUBAHAN ENDWEEK: Insight disatukan dengan spasi menjadi paragraf
        insight_endweek = " ".join(item["insight_list"])

        if "Format 1" in format_pilihan:
            # COPY SLIDE FB GROUP MAIN
            res_dup = slides_service.presentations().batchUpdate(
                presentationId=id_slide_baru, body={"requests": [{"duplicateObject": {"objectId": id_fb_main}}]}
            ).execute()
            id_baru_main = res_dup["replies"][0]["duplicateObject"]["objectId"]

            req_main = [
                {"updateSlidesPosition": {"slideObjectIds": [id_baru_main], "insertionIndex": slide_count}},
                {"replaceAllText": {"containsText": {"text": "{{TITLE}}"}, "replaceText": judul, "pageObjectIds": [id_baru_main]}},
                {"replaceAllText": {"containsText": {"text": "{{CONTEXT}}"}, "replaceText": konteks, "pageObjectIds": [id_baru_main]}},
                {"replaceAllText": {"containsText": {"text": "{{INSIGHT}}"}, "replaceText": insight_endweek, "pageObjectIds": [id_baru_main]}},
                {"replaceAllShapesWithImage": {"imageUrl": img_main, "replaceMethod": "CENTER_INSIDE", "containsText": {"text": "{{IMG}}", "matchCase": True}, "pageObjectIds": [id_baru_main]}}
            ]
            slides_service.presentations().batchUpdate(presentationId=id_slide_baru, body={"requests": req_main}).execute()
            slide_count += 1

            # COPY SLIDE FB GROUP COMMENT (Kalau Ada)
            if img_cmt and id_fb_comment:
                res_dup_c = slides_service.presentations().batchUpdate(
                    presentationId=id_slide_baru, body={"requests": [{"duplicateObject": {"objectId": id_fb_comment}}]}
                ).execute()
                id_baru_comment = res_dup_c["replies"][0]["duplicateObject"]["objectId"]

                req_cmt = [
                    {"updateSlidesPosition": {"slideObjectIds": [id_baru_comment], "insertionIndex": slide_count}},
                    {"replaceAllText": {"containsText": {"text": "{{TITLE}}"}, "replaceText": judul, "pageObjectIds": [id_baru_comment]}},
                    {"replaceAllShapesWithImage": {"imageUrl": img_cmt, "replaceMethod": "CENTER_INSIDE", "containsText": {"text": "{{CMT}}", "matchCase": True}, "pageObjectIds": [id_baru_comment]}}
                ]
                slides_service.presentations().batchUpdate(presentationId=id_slide_baru, body={"requests": req_cmt}).execute()
                slide_count += 1

        else:
            # COPY SLIDE PROMOTION
            res_dup = slides_service.presentations().batchUpdate(
                presentationId=id_slide_baru, body={"requests": [{"duplicateObject": {"objectId": id_promo_main}}]}
            ).execute()
            id_baru_main = res_dup["replies"][0]["duplicateObject"]["objectId"]

            req_main = [
                {"updateSlidesPosition": {"slideObjectIds": [id_baru_main], "insertionIndex": slide_count}},
                {"replaceAllText": {"containsText": {"text": "{{TITLE}}"}, "replaceText": judul, "pageObjectIds": [id_baru_main]}},
                {"replaceAllText": {"containsText": {"text": "{{CONTEXT}}"}, "replaceText": konteks, "pageObjectIds": [id_baru_main]}},
                {"replaceAllText": {"containsText": {"text": "{{INSIGHT}}"}, "replaceText": insight_endweek, "pageObjectIds": [id_baru_main]}},
                {"replaceAllShapesWithImage": {"imageUrl": img_main, "replaceMethod": "CENTER_INSIDE", "containsText": {"text": "{{IMG}}", "matchCase": True}, "pageObjectIds": [id_baru_main]}}
            ]
            slides_service.presentations().batchUpdate(presentationId=id_slide_baru, body={"requests": req_main}).execute()
            slide_count += 1

    return link_presentasi


# ============== AUTH ==============
@st.cache_resource(ttl=1800)
def get_creds():
    creds = Credentials(
        token=None,
        refresh_token=GOOGLE_REFRESH_TOKEN,
        token_uri=TOKEN_ENDPOINT,
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES.split(),
    )
    creds.refresh(GoogleAuthRequest())
    return creds


# ============== UI ==============
st.title("📊 GNS Slide Automation (Midweek & Endweek)")
st.caption("Upload gambar → AI Generates Midweek → Pilih Kategori → Auto Generate Endweek.")

try:
    creds = get_creds()
except Exception as e:
    st.error(f"Gagal autentikasi ke Google: {e}")
    st.stop()

# Inisialisasi Session State
if "midweek_done" not in st.session_state:
    st.session_state.midweek_done = False
    st.session_state.processed_data = []
    st.session_state.midweek_link = ""

st.divider()

# ------------- TAHAP 1: UPLOAD & MIDWEEK -------------
st.subheader("1️⃣ Upload Gambar (Untuk Midweek & Endweek)")
main_files = st.file_uploader("Upload gambar utama", type=["jpg", "jpeg", "png"], accept_multiple_files=True, key="main_uploader")
comment_files = st.file_uploader("Upload gambar comment (opsional)", type=["jpg", "jpeg", "png"], accept_multiple_files=True, key="comment_uploader")

news_items = []
if main_files:
    comment_by_name = {f.name: f for f in (comment_files or [])}
    for mf in main_files:
        news_items.append({"main": mf, "comment": comment_by_name.get(mf.name)})

if news_items and st.button("🚀 1. Jalankan Midweek", type="primary"):
    progress_bar = st.progress(0)
    status_box = st.empty()
    with st.spinner("Memproses Midweek (AI Analysis)..."):
        try:
            link_midweek, data_tersimpan = jalankan_otomatisasi_midweek(creds, news_items, progress_bar, status_box)
            # Simpan hasil ke session_state
            st.session_state.processed_data = data_tersimpan
            st.session_state.midweek_link = link_midweek
            st.session_state.midweek_done = True
            st.success("🎉 Slide Midweek Selesai!")
        except Exception as e:
            st.error(f"Kesalahan Midweek: {e}")

# ------------- TAHAP 2 & 3: KATEGORI & ENDWEEK -------------
if st.session_state.midweek_done and len(st.session_state.processed_data) > 0:
    st.divider()
    st.markdown(f"✅ **[Buka Presentasi Midweek di sini]({st.session_state.midweek_link})**")
    
    st.subheader("2️⃣ Kategori Slide untuk Endweek")
    st.info("Pilih format presentasi untuk masing-masing slide di laporan Endweek. AI tidak akan dijalankan ulang, melainkan menggunakan hasil Midweek (Insight akan digabung menjadi 1 paragraf).")

    # Form untuk pemetaan format
    selections = {}
    for item in st.session_state.processed_data:
        selections[item["filename"]] = st.radio(
            f"Format untuk gambar: **{item['filename']}**",
            options=["Format 1 (Facebook Group)", "Format 2 (Promotion)"],
            key=f"format_{item['filename']}",
            horizontal=True
        )

    if st.button("🚀 2. Buat Slide Endweek", type="secondary"):
        status_box_endweek = st.empty()
        with st.spinner("Menyusun Slide Endweek..."):
            try:
                link_endweek = jalankan_otomatisasi_endweek(
                    creds, 
                    st.session_state.processed_data, 
                    selections, 
                    status_box_endweek
                )
                st.balloons()
                st.success("🎉 Slide Endweek Selesai!")
                st.markdown(f"**[Buka Presentasi Endweek]({link_endweek})**")
            except Exception as e:
                st.error(f"Kesalahan saat menyusun Endweek: {e}")